from __future__ import annotations

import datetime
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any

import click
import typer
from rich.table import Table

from . import capsules
from .cloudinit import build_network_config, build_user_data
from .config import BASE_IMAGES, get_config, load_latita_template
from .hardening import SecurityProfile, apply_hardening_to_args
from .libvirt import (
    autostart_network,
    define_network,
    ensure_network_active,
    ensure_network_exists,
    get_vm_ip_addresses,
    get_vm_interfaces,
    get_vm_state,
    get_vm_wan_ip,
    grant_qemu_path_access,
    iface_exists,
    iface_is_wireless,
    network_exists,
    network_is_active,
    random_mac,
    start_network,
    stop_vm_libvirt,
    start_vm_libvirt,
    resume_vm_libvirt,
    undefine_vm_libvirt,
    vm_exists,
    virt_install,
    write_mgmt_network_xml,
    create_network_xml,
)
from . import metadata
from .metadata import (
    append_applied_capsule,
    read_applied_capsules,
    read_instance_env,
    read_instance_recipe,
    read_instance_spec,
    write_instance_env,
    write_instance_recipe,
    write_instance_spec,
)
from .ui import console
from .utils import (
    create_lab_key,
    default_host_pubkey,
    hash_password_interactive,
    host_key_exists,
    lab_key_exists,
    need_cmd,
    read_text,
    run,
    shred_file,
    validate_ip,
    validate_name,
)


# ---------------------------------------------------------------------------
# OS info mapping
# ---------------------------------------------------------------------------

def _osinfo_for_recipe(recipe: dict[str, Any]) -> str:
    os_family = recipe.get("os_family", "fedora")
    base_image = recipe.get("base_image", "")
    if "ubuntu" in os_family or "ubuntu" in base_image.lower():
        return "detect=on,name=ubuntu24.04,require=off"
    if "debian" in os_family or "debian" in base_image.lower():
        return "detect=on,name=debian12,require=off"
    if "fedora" in os_family:
        version = "43"
        # try to extract version from base_image filename
        import re
        m = re.search(r'fedora(\d+)', base_image.lower())
        if m:
            version = m.group(1)
        return f"detect=on,name=fedora{version},require=off"
    return "detect=on,name=linux2024,require=off"


def _package_manager_for_recipe(recipe: dict[str, Any]) -> str:
    os_family = recipe.get("os_family", "fedora")
    if os_family in ("ubuntu", "debian"):
        return "apt"
    if os_family in ("alpine",):
        return "apk"
    return "dnf"


def _check_libvirt_connectivity(cfg) -> None:
    """Verify libvirt is reachable; emit helpful hint if not."""
    if cfg.is_session:
        return
    import subprocess
    try:
        cp = subprocess.run(
            ["virsh", "-c", cfg.libvirt_uri, "list"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if cp.returncode == 0:
            return
    except (subprocess.TimeoutExpired, OSError):
        pass
    raise typer.BadParameter(
        f"Cannot connect to libvirt at {cfg.libvirt_uri}. "
        "Set LIBVIRT_DEFAULT_URI=qemu:///session to use user-level libvirt without sudo, "
        "or ensure the system libvirtd daemon is running and sudo is configured."
    )

def _find_free_port(start: int = 2222, end: int = 9999) -> int:
    """Find a bindable TCP port on localhost.

    Uses bind() instead of connect_ex() because QEMU needs to *listen*
    on the port for hostfwd. A port may reject connections (e.g. firewall)
    yet still be occupied for binding, or vice versa.
    """
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free TCP port found in range {start}-{end}")


_VIDEO_MODEL_CACHE: dict[str, Any] | None = None


def _detect_video_models() -> dict[str, Any]:
    """Probe QEMU and return ranked video models for desktop VMs.

    Returns a dict with ``best`` (str) and ``available`` (dict[str, bool]).
    Preference order: qxl > virtio > vga.
    """
    global _VIDEO_MODEL_CACHE
    if _VIDEO_MODEL_CACHE is not None:
        return _VIDEO_MODEL_CACHE

    cfg = get_config()
    qemu_bin = cfg.qemu_binary or "qemu-system-x86_64"

    devices: set[str] = set()
    try:
        out = subprocess.run(
            [qemu_bin, "-device", "help"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        devices = {m.group(1).lower() for m in re.finditer(r'name "([^"]+)"', out)}
    except Exception:
        pass

    # Note: virt-install --video virtio maps to virtio-gpu-pci (PCI model).
    # virtio-gpu-device (virtio-bus) does NOT work with --video virtio.
    available: dict[str, bool] = {
        "qxl": any(d in devices for d in ("qxl", "qxl-vga", "qxl-pci")),
        "virtio": "virtio-gpu-pci" in devices,
        "vga": any(d in devices for d in ("VGA", "cirrus-vga", "vmware-svga")),
    }

    for model in ("qxl", "virtio", "vga"):
        if available[model]:
            best = model
            break
    else:
        best = "vga"  # universal fallback

    result = {"best": best, "available": available}
    _VIDEO_MODEL_CACHE = result
    return result


def _detect_video_model() -> str:
    """Convenience: return the best detected video model string."""
    return _detect_video_models()["best"]


def _pick_video_model_cli(available: dict[str, bool], default: str) -> str:
    """Ranked CLI prompt for picking a video model.

    Prints available models with the default starred, then reads input.
    """
    labels = {
        "qxl": "qxl   — best SPICE performance",
        "virtio": "virtio — good performance",
        "vga": "vga    — universal fallback",
    }
    print("\nDetected video models (ranked):")
    for model in ("qxl", "virtio", "vga"):
        if not available.get(model):
            continue
        marker = " [default]" if model == default else ""
        print(f"  {labels[model]}{marker}")
    print(f"\nDefault: {default}")
    choice = input("Video model (Enter for default): ").strip().lower()
    if choice in available and available[choice]:
        return choice
    return default


def _build_nocloud_iso(
    ud_path: Path,
    nc_path: Path | None,
    iso_path: Path,
    instance_id: str = "latita",
    hostname: str = "latita-vm",
) -> None:
    """Create a NoCloud ISO from user-data, meta-data, and optional network-config.

    The ISO is labelled ``cidata`` so cloud-init's nocloud datasource
    discovers it automatically.  This is more reliable than ``virt-install
    --cloud-init`` in ``qemu:///session`` mode, where the temporary ISO
    created by virt-install is sometimes missing by the time the guest
    boots.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp_dir = Path(td)
        (tmp_dir / "user-data").write_text(ud_path.read_text())
        (tmp_dir / "meta-data").write_text(
            f"instance-id: {instance_id}\nlocal-hostname: {hostname}\n"
        )
        files = [str(tmp_dir / "user-data"), str(tmp_dir / "meta-data")]
        if nc_path is not None and nc_path.exists():
            (tmp_dir / "network-config").write_text(nc_path.read_text())
            files.append(str(tmp_dir / "network-config"))
        run([
            "xorriso", "-as", "mkisofs",
            "-o", str(iso_path),
            "-V", "cidata",
            "-J", "-R",
            *files,
        ])


# ---------------------------------------------------------------------------
# Template normalization
# ---------------------------------------------------------------------------

def normalize_template(data: dict[str, Any]) -> dict[str, Any]:
    """Take a raw .latita template and fill defaults."""
    d = dict(data)
    profile = str(d.get("profile", "headless")).lower()
    if profile not in ("headless", "desktop"):
        raise typer.BadParameter(f"template profile must be headless or desktop, got {profile}")

    net = d.get("network") or {}
    ephemeral = d.get("ephemeral") or {}
    security = d.get("security") or {}
    provision = d.get("provision") or {}

    return {
        "profile": profile,
        "os_family": str(d.get("os_family", "fedora")).lower(),
        "description": str(d.get("description", "")),
        "base_image": str(d.get("base_image", get_config().default_base_name)),
        "cpus": int(d.get("cpus", 2)),
        "memory": int(d.get("memory", 4096)),
        "disk_size": str(d.get("disk_size", "20G")),
        "guest_user": str(d.get("guest_user", "dev")),
        "passwordless_sudo": bool(d.get("passwordless_sudo", True)),
        "network": {
            "mode": str(net.get("mode", "nat")).lower(),
            "nat_network": str(net.get("nat_network", "default")),
            "uplink": str(net.get("uplink", "")).strip() or None,
            "mgmt_ip": str(net.get("mgmt_ip", "10.31.0.10")),
            "mgmt_prefix": str(net.get("mgmt_prefix", "24")),
        },
        "ephemeral": {
            "transient": bool(ephemeral.get("transient", profile == "headless")),
            "destroy_on_stop": bool(ephemeral.get("destroy_on_stop", False)),
            "max_runs": int(ephemeral["max_runs"]) if ephemeral.get("max_runs") is not None else None,
            "expires_after_hours": int(ephemeral["expires_after_hours"]) if ephemeral.get("expires_after_hours") is not None else None,
        },
        "security": {
            "selinux": bool(security.get("selinux", True)),
            "no_guest_agent": bool(security.get("no_guest_agent", True)),
            "restrict_network": bool(security.get("restrict_network", False)),
            "allow_hosts": list(security.get("allow_hosts", [])),
            "readonly_root": bool(security.get("readonly_root", False)),
        },
        "capsules": list(d.get("capsules", [])),
        "provision": {
            "packages": list(provision.get("packages", [])),
            "write_files": list(provision.get("write_files", [])),
            "root_commands": list(provision.get("root_commands", [])),
            "user_commands": list(provision.get("user_commands", [])),
        },
    }


# ---------------------------------------------------------------------------
# Recipe helpers
# ---------------------------------------------------------------------------

def _default_keys() -> dict[str, str]:
    cfg = get_config()
    host = default_host_pubkey()
    lab = cfg.keys_dir / "lab1_ed25519.pub"
    return {
        "host_pubkey_path": str(host) if host else "",
        "lab_pubkey_path": str(lab) if lab.exists() else "",
        "lab_privkey_path": str(cfg.keys_dir / "lab1_ed25519"),
    }


def build_recipe(
    template_name: str,
    overrides: dict[str, Any] | None = None,
    capsule_names: list[str] | None = None,
) -> dict[str, Any]:
    template = normalize_template(load_latita_template(template_name))
    recipe = deepcopy(template)
    recipe["template_name"] = template_name
    if overrides:
        _deep_update(recipe, overrides)

    # Resolve capsules (validate compatibility)
    requested = list(capsule_names or recipe.get("capsules", []))
    if requested:
        resolved = capsules.resolve_capsules(
            requested,
            profile=recipe["profile"],
            os_family=recipe["os_family"],
        )
        recipe["_resolved_capsules"] = [data for _, data in resolved]
        recipe["capsules"] = requested
    else:
        recipe["_resolved_capsules"] = []

    # Keys
    keys = _default_keys()
    if not keys["host_pubkey_path"]:
        if host_key_exists():
            keys["host_pubkey_path"] = str(default_host_pubkey())
        else:
            keys["host_pubkey_path"] = str(create_lab_key("lab1").with_suffix(".pub"))
    if not keys["lab_pubkey_path"]:
        keys["lab_pubkey_path"] = str(create_lab_key("lab1").with_suffix(".pub"))
        keys["lab_privkey_path"] = str(get_config().keys_dir / "lab1_ed25519")

    recipe["_keys"] = keys
    return recipe


def _deep_update(base: dict[str, Any], override: dict[str, Any]) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v


# ---------------------------------------------------------------------------
# Host bootstrap
# ---------------------------------------------------------------------------

def bootstrap_host() -> None:
    cfg = get_config()
    console.print("Bootstrapping latita...\n")
    need_cmd("virsh", "ssh-keygen", "qemu-img")
    if not cfg.is_session:
        need_cmd("setfacl")
    cfg.ensure_dirs()

    console.print(f"  [green]✓[/green] Root: {cfg.root_dir}")
    console.print(f"  [green]✓[/green] Keys: {cfg.keys_dir}")
    console.print(f"  [green]✓[/green] VMs: {cfg.inst_dir}")

    # SSH keys
    if not host_key_exists():
        create_host_key()
    console.print(f"  [green]✓[/green] Host key: {default_host_pubkey()}")

    lab_key = cfg.keys_dir / "lab1_ed25519"
    if not lab_key.exists():
        create_lab_key("lab1")
    console.print(f"  [green]✓[/green] Lab key: {lab_key}")

    if not cfg.is_session:
        # Management network
        xml_path = write_mgmt_network_xml(cfg)
        if not network_exists(cfg.net_name):
            run(["virsh", "-c", cfg.libvirt_uri, "net-define", str(xml_path)], sudo=True)
            console.print(f"  [green]✓[/green] Defined network: {cfg.net_name}")
        if not network_is_active(cfg.net_name):
            run(["virsh", "-c", cfg.libvirt_uri, "net-start", cfg.net_name], sudo=True)
            console.print(f"  [green]✓[/green] Started network: {cfg.net_name}")
        run(["virsh", "-c", cfg.libvirt_uri, "net-autostart", cfg.net_name], sudo=True)
        console.print(f"  [green]✓[/green] Network autostart enabled")

        # Default NAT network
        if not network_exists("default"):
            xml = create_network_xml(
                name="default",
                mode="nat",
                ip_address="192.168.122.1",
                netmask="255.255.255.0",
                dhcp_start="192.168.122.100",
                dhcp_end="192.168.122.200",
            )
            p = cfg.net_dir / "default.xml"
            p.write_text(xml)
            define_network(p)
            start_network("default")
            autostart_network("default")
            console.print("  [green]✓[/green] Created NAT network: default")
        else:
            if not network_is_active("default"):
                start_network("default")
                autostart_network("default")
                console.print("  [green]✓[/green] Started NAT network: default")
            else:
                console.print("  [green]✓[/green] NAT network: default")
    else:
        console.print("  [dim]Session mode: skipped network setup[/dim]")

    # Base image
    base_img = cfg.base_dir / cfg.default_base_name
    if not base_img.exists():
        console.print(f"\n[yellow]Base image not found: {base_img}[/yellow]")
        # Look up the default base image in BASE_IMAGES catalog
        base_info = None
        for data in BASE_IMAGES.values():
            if data["filename"] == cfg.default_base_name:
                base_info = data
                break
        if base_info and typer.confirm(f"Download {cfg.default_base_name} now?", default=True):
            init_base(base_info["filename"], base_info["url"], base_info.get("discover", False))
            console.print(f"  [green]✓[/green] Downloaded: {cfg.default_base_name}")
        else:
            console.print("  Skipped. Run 'latita init-base' later.", style="yellow")
    else:
        console.print(f"\n  [green]✓[/green] Base image: {base_img}")

    console.print("\n[green]Bootstrap complete![/green]")


# ---------------------------------------------------------------------------
# Base image download
# ---------------------------------------------------------------------------

def init_base(
    name: str | None = None,
    url: str | None = None,
    discover: bool = False,
) -> None:
    need_cmd("curl", "qemu-img")
    get_config().ensure_dirs()
    if name and url:
        _download_base(name, url, discover=discover)
        return
    choices = list(BASE_IMAGES.keys()) + ["cancel"]
    choice = typer.prompt(
        "Choose base image",
        type=click.Choice(choices),
        default=choices[0],
    )
    if choice == "cancel":
        return
    info = BASE_IMAGES[choice]
    _download_base(info["filename"], info["url"], discover=info.get("discover", False))


def _discover_latest_fedora_url(url: str) -> str | None:
    """Scrape a Fedora Cloud image directory listing for the latest Generic qcow2.

    Accepts either a directory URL (ending in /) or a file URL.
    """
    if url.endswith("/"):
        dir_url = url
    else:
        dir_url = url.rsplit("/", 1)[0] + "/"
    try:
        with urllib.request.urlopen(dir_url, timeout=20) as resp:  # noqa: S310
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return None
    # Extract filenames like Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2
    matches = re.findall(
        r'href="(Fedora-Cloud-Base-Generic-\d+(?:\.\d+)*-[\d.]+\.x86_64\.qcow2)"',
        html,
    )
    if not matches:
        return None
    # Sort by version string and pick the latest
    matches.sort(key=lambda s: [int(x) for x in re.findall(r"\d+", s)])
    return dir_url + matches[-1]


def _maybe_download_base(base_image_name: str) -> bool:
    """Download a base image if it's in the catalog. No interactive prompts.

    Returns True if the image is now available, False if not in catalog or download failed.
    """
    cfg = get_config()
    base_img = cfg.base_dir / base_image_name
    if base_img.exists():
        return True

    info = None
    for label, data in BASE_IMAGES.items():
        if data["filename"] == base_image_name:
            info = data
            break
    if not info:
        return False

    try:
        _download_base(info["filename"], info["url"], discover=info.get("discover", False))
        return (cfg.base_dir / base_image_name).exists()
    except Exception as exc:
        console.print(f"[red]Download failed: {exc}[/red]")
        return False


def _download_base(name: str, url: str, discover: bool = False) -> None:
    cfg = get_config()
    dst = cfg.base_dir / name
    if dst.exists():
        console.print(f"Base image already exists: {dst}", style="green")
        return

    # Build list of URLs to try: primary then any mirrors from BASE_IMAGES
    urls_to_try = [url]
    for data in BASE_IMAGES.values():
        if data.get("filename") == name:
            for mirror in data.get("mirror_urls", []):
                urls_to_try.append(mirror)
            break

    last_exc: Exception | None = None
    for attempt_url in urls_to_try:
        download_url = attempt_url
        if discover:
            discovered = _discover_latest_fedora_url(attempt_url)
            if discovered:
                download_url = discovered
                console.print(f"[dim]Discovered latest image:[/dim] {discovered}")
            else:
                console.print(f"[yellow]Could not discover image from {attempt_url}; skipping.[/yellow]")
                continue

        try:
            # curl: follow redirects, retry transient errors, 30s connect timeout, 10min max
            run([
                "curl", "-L", "--fail",
                "--retry", "3", "--retry-delay", "5",
                "--connect-timeout", "30", "--max-time", "600",
                "-o", str(dst), download_url,
            ])
            dst.chmod(0o444)
            run(["qemu-img", "info", str(dst)])
            console.print(f"Downloaded: {dst}", style="green")
            return
        except subprocess.CalledProcessError as exc:
            last_exc = exc
            console.print(
                f"[yellow]Download from {download_url} failed (exit {exc.returncode});"
                f" trying next source...[/yellow]"
            )

    raise RuntimeError(
        f"Failed to download '{name}' from all sources. "
        f"Last error: {last_exc}"
    ) from last_exc


# ---------------------------------------------------------------------------
# Host network setup helpers
# ---------------------------------------------------------------------------

def _ensure_host_networks(
    cfg,
    net_mode: str,
    nat_network: str = None,
) -> None:
    if cfg.is_session:
        return

    # Verify libvirt is reachable before attempting any sudo operations
    _check_libvirt_connectivity(cfg)

    grant_qemu_path_access()

    if net_mode not in ("isolated", "none"):
        xml_path = write_mgmt_network_xml(cfg)
        if not network_exists(cfg.net_name):
            run(["virsh", "-c", cfg.libvirt_uri, "net-define", str(xml_path)], sudo=True)
            console.print(f"  [green]\u2713[/green] Defined network: {cfg.net_name}")
        if not network_is_active(cfg.net_name):
            run(["virsh", "-c", cfg.libvirt_uri, "net-start", cfg.net_name], sudo=True)
            console.print(f"  [green]\u2713[/green] Started network: {cfg.net_name}")
        run(["virsh", "-c", cfg.libvirt_uri, "net-autostart", cfg.net_name], sudo=True)
        console.print(f"  [green]\u2713[/green] Network autostart enabled")

    if net_mode == "nat" and nat_network:
        ensure_network_active(nat_network)



# ---------------------------------------------------------------------------
# Instance creation
# ---------------------------------------------------------------------------

def create_instance(
    template_name: str,
    name: str | None = None,
    capsule_names: list[str] | None = None,
    overrides: dict[str, Any] | None = None,
    *,
    wait: bool = True,
) -> None:
    cfg = get_config()
    validate_name(name or "")
    recipe = build_recipe(template_name, overrides=overrides, capsule_names=capsule_names)
    if name:
        recipe["name"] = name
    else:
        recipe["name"] = _suggest_name(recipe["profile"])

    validate_name(recipe["name"])
    validate_ip(recipe["network"]["mgmt_ip"])

    inst = cfg.inst_dir / recipe["name"]
    pending_marker = inst / ".downloading"
    creating_marker = inst / ".creating"
    if inst.exists() and not pending_marker.exists() and not creating_marker.exists():
        raise typer.BadParameter(f"instance already exists: {inst}")

    base_img = cfg.base_dir / recipe["base_image"]
    if not base_img.exists():
        raise typer.BadParameter(f"base image not found: {base_img}")

    need_cmd("qemu-img", "virt-install", "virsh")
    cfg.ensure_dirs()
    _check_libvirt_connectivity(cfg)

    # Networking
    net = recipe["network"]
    net_mode = net["mode"]
    nat_network = net["nat_network"]
    uplink = net.get("uplink")

    if cfg.is_session:
        net_mode = "user"
    elif net_mode == "auto":
        from .libvirt import detect_default_uplink
        uplink = uplink or detect_default_uplink()
        if not uplink:
            raise typer.BadParameter("could not detect default uplink")
        if iface_is_wireless(uplink):
            net_mode = "nat"
            if not nat_network:
                raise typer.BadParameter("wifi detected; pass nat_network")
        else:
            net_mode = "direct"


    _ensure_host_networks(cfg, net_mode, nat_network)
    if net_mode in ("isolated", "none"):
        pass  # no host-side setup needed
    elif net_mode == "direct":
        if not uplink or not iface_exists(uplink):
            raise typer.BadParameter(f"uplink does not exist: {uplink}")
        if iface_is_wireless(uplink):
            raise typer.BadParameter(f"uplink {uplink} is wireless; use nat mode")
    elif net_mode == "nat":
        if not nat_network:
            raise typer.BadParameter("nat_network required for nat mode")
        ensure_network_active(nat_network)
    elif net_mode == "user":
        pass  # no host-side setup needed
    else:
        raise typer.BadParameter("network mode must be isolated, nat, direct, auto, or user")

    inst.mkdir(parents=True, exist_ok=True)
    overlay = inst / f"{recipe['name']}.qcow2"

    # Pre-flight checks
    for key_path in (recipe["_keys"]["host_pubkey_path"], recipe["_keys"]["lab_pubkey_path"]):
        if key_path and not Path(key_path).exists():
            console.print(f"[yellow]Warning: key not found: {key_path}[/yellow]")

    try:
        _run_create(recipe, inst, overlay, net_mode, nat_network, uplink)
    except Exception as exc:
        console.print(f"\n[red]Creation failed: {exc}[/red]")
        console.print("[yellow]Rolling back instance directory...[/yellow]")
        _rollback_create(inst)
        raise

    if wait:
        wait_for_vm_ready(recipe["name"])
    else:
        console.print(f"\n[dim]VM created. Use `latita wait {recipe['name']}` or `latita status {recipe['name']}` to track initialization.[/dim]")


def _rollback_create(inst: Path) -> None:
    if inst.exists():
        for f in inst.iterdir():
            if f.is_file():
                shred_file(f)
        shutil.rmtree(inst)


def _run_create(
    recipe: dict[str, Any],
    inst: Path,
    overlay: Path,
    net_mode: str,
    nat_network: str,
    uplink: str | None,
) -> None:
    cfg = get_config()
    base_img = cfg.base_dir / recipe["base_image"]
    net = recipe["network"]

    # Build cloud-init
    keys = recipe["_keys"]
    host_pubkey = read_text(Path(keys["host_pubkey_path"]))
    lab_pubkey = read_text(Path(keys["lab_pubkey_path"])) if keys["lab_pubkey_path"] else ""

    capsule_provisions = [
        capsules.capsule_provision_fragment(c)
        for c in recipe.get("_resolved_capsules", [])
    ]

    # Desktop needs login hash unless it's desktop-minimal (autologin, no password needed)
    login_hash = recipe.get("login_hash", "")
    if not login_hash and recipe["profile"] == "desktop" and recipe.get("template_name") != "desktop-minimal":
        login_hash = hash_password_interactive(guest_user=recipe["guest_user"])

    pkg_mgr = _package_manager_for_recipe(recipe)
    osinfo = _osinfo_for_recipe(recipe)

    user_data = build_user_data(
        profile=recipe["profile"],
        guest_user=recipe["guest_user"],
        host_pubkey=host_pubkey,
        lab_pubkey=lab_pubkey,
        lab_privkey=Path(keys["lab_privkey_path"]) if keys.get("lab_privkey_path") else None,
        login_hash=login_hash,
        provision=recipe["provision"],
        capsule_provisions=capsule_provisions,
        passwordless_sudo=recipe["passwordless_sudo"],
        package_manager=pkg_mgr,
        os_family=recipe.get("os_family", "fedora"),
    )

    wan_mac = random_mac()
    mgmt_mac = random_mac()

    ud_path = inst / "user-data.yaml"
    # Skip network-config in user mode (session SLIRP) — the VM only has one
    # interface and cloud-init's v2 config with two ethernets causes service
    # failures. In user mode the guest OS defaults to DHCP on the single NIC.
    if net_mode not in ("isolated", "none", "user"):
        net_cfg = build_network_config(
            wan_mac, mgmt_mac, net["mgmt_ip"], net["mgmt_prefix"]
        )
        nc_path = inst / "network-config.yaml"
        ud_path.write_text(user_data)
        nc_path.write_text(net_cfg)
    else:
        ud_path.write_text(user_data)
        nc_path = None

    # Build a persistent NoCloud ISO — more reliable than virt-install's
    # temporary --cloud-init ISO, especially in qemu:///session mode.
    iso_path = inst / "nocloud.iso"
    _build_nocloud_iso(ud_path, nc_path, iso_path, instance_id=recipe["name"])

    run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(base_img), str(overlay)])
    run(["qemu-img", "resize", str(overlay), recipe["disk_size"]])

    # Virt-install args
    args = [
        "--name", recipe["name"],
        "--memory", str(recipe["memory"]),
        "--vcpus", str(recipe["cpus"]),
        "--cpu", "host-passthrough",
        "--import",
        "--osinfo", osinfo,
        "--disk", f"path={overlay},format=qcow2,bus=virtio,discard=unmap",
        "--disk", f"path={iso_path},device=cdrom,readonly=on",
        "--rng", "/dev/urandom",
        "--noautoconsole",
    ]

    if recipe["ephemeral"]["transient"]:
        args.append("--transient")

    if recipe["profile"] == "desktop":
        video_model = recipe.get("video_model")
        if not video_model:
            video_model = _detect_video_model()
        args.extend(["--graphics", "spice,listen=127.0.0.1", "--video", video_model])
        if not cfg.is_session:
            args.extend(["--channel", "spicevmc"])
    else:
        args.extend(["--graphics", "none"])
        # Serial console so virt-viewer can connect even without a display
        args.extend(["--serial", "pty"])
        video_model = ""

    # Networking
    ssh_port = None
    if net_mode == "isolated" or net_mode == "none":
        args.extend(["--network", "none"])
    elif net_mode == "direct":
        args.extend(["--network", f"type=direct,source={uplink},source_mode=private,model=virtio,mac={wan_mac}"])
    elif net_mode == "user":
        if cfg.is_session:
            ssh_port = _find_free_port()
            args.extend(["--network", "none"])
            args.append(f"--qemu-commandline=-netdev user,id=net0,hostfwd=tcp::{ssh_port}-:22 -device virtio-net-pci,netdev=net0,mac={wan_mac},addr=0x10")
        else:
            args.extend(["--network", f"type=user,model=virtio,mac={wan_mac}"])
    else:
        args.extend(["--network", f"network={nat_network},model=virtio,mac={wan_mac}"])
    if not cfg.is_session and net_mode not in ("isolated", "none"):
        args.extend(["--network", f"network={cfg.net_name},model=virtio,mac={mgmt_mac}"])

    # Security hardening
    sec = recipe["security"]
    profile = SecurityProfile.from_dict(sec)
    args = apply_hardening_to_args(profile, args, vm_name=recipe["name"])

    # Try virt-install; on video-model failure offer a ranked picker and retry.
    try:
        virt_install(args)
    except Exception as exc:
        err_msg = str(exc)
        if recipe["profile"] == "desktop" and "is not a valid device model name" in err_msg:
            console.print(f"\n[red]Video model '{video_model}' failed:[/red] {err_msg}")
            models = _detect_video_models()
            picked = _pick_video_model_cli(models["available"], models["best"])
            # Replace the --video argument
            for i, arg in enumerate(args):
                if arg == "--video":
                    args[i + 1] = picked
                    break
            video_model = picked
            console.print(f"[green]Retrying with --video {picked}...[/green]\n")
            virt_install(args)
        else:
            raise

    # Spec & metadata
    now = datetime.datetime.now(datetime.timezone.utc)
    expire_at = None
    hours = recipe["ephemeral"].get("expires_after_hours")
    if hours:
        expire_at = (now + datetime.timedelta(hours=hours)).isoformat()

    spec = {
        "role": recipe["profile"],
        "template_name": recipe["template_name"],
        "overlay": str(overlay),
        "wan_mac": wan_mac,
        "mgmt_mac": mgmt_mac,
        "net_mode": net_mode,
        "nat_network": nat_network,
        "uplink": uplink,
        "graphics": "spice" if recipe["profile"] == "desktop" else "none",
        "video_model": video_model,
        "transient": recipe["ephemeral"]["transient"],
        "destroy_on_stop": recipe["ephemeral"]["destroy_on_stop"],
        "max_runs": recipe["ephemeral"]["max_runs"],
        "expire_at": expire_at,
        "run_count": 0,
        "created_at": now.isoformat(),
        "base_image": recipe["base_image"],
        "osinfo": osinfo,
    }
    spec["applied_capsules"] = recipe.get("capsules", [])
    write_instance_recipe(recipe["name"], recipe)
    write_instance_spec(recipe["name"], spec)
    write_instance_env(recipe["name"], {
        "NAME": recipe["name"],
        "TEMPLATE": recipe["template_name"],
        "PROFILE": recipe["profile"],
        "MGMT_IP": net["mgmt_ip"],
        "GUEST_USER": recipe["guest_user"],
        "TRANSIENT": "yes" if spec["transient"] else "no",
        "DESTROY_ON_STOP": "yes" if spec["destroy_on_stop"] else "no",
        "MAX_RUNS": str(spec["max_runs"] or ""),
        "EXPIRE_AT": str(spec["expire_at"] or ""),
        "FORWARDED_SSH_PORT": str(ssh_port) if ssh_port else "",
        "GRAPHICS": spec["graphics"],
        "LIBVIRT_URI": cfg.libvirt_uri,
    })

    console.print(f"\n[green]Created {recipe['name']} from template '{recipe['template_name']}'[/green]")
    console.print(f"  Profile : {recipe['profile']}")
    console.print(f"  IP      : {net['mgmt_ip']}")
    console.print(f"  Overlay : {overlay}")
    if recipe["ephemeral"]["transient"]:
        console.print(f"  Mode    : transient (libvirt)")
    if recipe["ephemeral"]["destroy_on_stop"]:
        console.print(f"  Mode    : ephemeral (destroy on stop)")
    if hours:
        console.print(f"  Expires : {expire_at}")
    if recipe["ephemeral"]["max_runs"]:
        console.print(f"  Max runs: {recipe['ephemeral']['max_runs']}")
    console.print(f"\nSSH: latita ssh {recipe['name']}")


# ---------------------------------------------------------------------------
# Instance lifecycle
# ---------------------------------------------------------------------------

def _check_ephemeral_constraints(name: str) -> None:
    spec = read_instance_spec(name)
    if not spec:
        return

    # Expiration
    expire_at = spec.get("expire_at")
    if expire_at:
        dt = datetime.datetime.fromisoformat(expire_at)
        if datetime.datetime.now(datetime.timezone.utc) > dt:
            raise typer.BadParameter(
                f"VM '{name}' has expired ({expire_at}). Destroy it with 'latita destroy {name}'"
            )

    # Max runs
    max_runs = spec.get("max_runs")
    if max_runs is not None:
        count = metadata.get_run_count(name)
        if count >= max_runs:
            raise typer.BadParameter(
                f"VM '{name}' reached max runs ({max_runs}). Destroy it with 'latita destroy {name}'"
            )


def start_instance(name: str, *, wait: bool = True) -> None:
    validate_name(name)
    if not vm_exists(name):
        raise typer.BadParameter(f"VM not found in libvirt: {name}")
    _check_ephemeral_constraints(name)
    state = get_vm_state(name)
    if state == "running":
        console.print(f"{name} is already running", style="yellow")
        return
    if state == "paused":
        resume_vm_libvirt(name)
    else:
        start_vm_libvirt(name)
    metadata.increment_run_count(name)
    console.print(f"Started {name}", style="green")
    if wait:
        wait_for_vm_ready(name)


def pause_instance(name: str) -> None:
    validate_name(name)
    if not vm_exists(name):
        raise typer.BadParameter(f"VM not found in libvirt: {name}")
    state = get_vm_state(name)
    if state == "paused":
        console.print(f"{name} is already paused", style="yellow")
        return
    if state != "running":
        console.print(f"Cannot pause {name}: state is {state}", style="yellow")
        return
    from .libvirt import suspend_vm_libvirt
    suspend_vm_libvirt(name)
    console.print(f"Paused {name}", style="green")


def resume_instance(name: str) -> None:
    validate_name(name)
    if not vm_exists(name):
        raise typer.BadParameter(f"VM not found in libvirt: {name}")
    state = get_vm_state(name)
    if state == "running":
        console.print(f"{name} is already running", style="yellow")
        return
    if state != "paused":
        console.print(f"Cannot resume {name}: state is {state}", style="yellow")
        return
    resume_vm_libvirt(name)
    console.print(f"Resumed {name}", style="green")


def stop_instance(name: str) -> None:
    validate_name(name)
    spec = read_instance_spec(name)
    if spec and spec.get("destroy_on_stop"):
        console.print(f"VM '{name}' is ephemeral (destroy_on_stop). Destroying...", style="yellow")
        destroy_instance(name)
        return

    if not vm_exists(name):
        raise typer.BadParameter(f"VM not found in libvirt: {name}")
    state = get_vm_state(name)
    if state == "shut off":
        console.print(f"{name} is already stopped", style="yellow")
        return
    stop_vm_libvirt(name)
    console.print(f"Stopped {name}", style="green")


def destroy_instance(name: str) -> None:
    validate_name(name)
    cfg = get_config()
    inst = cfg.inst_dir / name

    if vm_exists(name):
        stop_vm_libvirt(name, capture=True)
        undefine_vm_libvirt(name, capture=True)
        if vm_exists(name):
            raise typer.BadParameter(
                f"failed to undefine VM '{name}' in libvirt. "
                "It may have snapshots, checkpoints, or a managed save image."
            )

    if inst.exists():
        overlay = inst / f"{name}.qcow2"
        shred_file(overlay)
        try:
            shutil.rmtree(inst)
        except PermissionError:
            console.print(
                f"[yellow]Could not remove {inst} (permission denied).[/yellow]\n"
                f"Run: sudo rm -rf {inst}",
            )

    console.print(f"Destroyed {name}", style="green")


# ---------------------------------------------------------------------------
# One-shot ephemeral runner (like smolvm machine run)
# ---------------------------------------------------------------------------

def run_instance(
    template_name: str,
    command: list[str] | None = None,
    overrides: dict[str, Any] | None = None,
    capsule_names: list[str] | None = None,
) -> None:
    """Create a transient, one-shot VM with no persistent state."""
    cfg = get_config()
    recipe = build_recipe(template_name, overrides=overrides, capsule_names=capsule_names)

    recipe["ephemeral"]["transient"] = True
    recipe["ephemeral"]["destroy_on_stop"] = False
    if cfg.is_session:
        recipe["network"]["mode"] = "user"

    name = recipe.get("name") or _suggest_name(recipe["profile"])
    validate_name(name)
    recipe["name"] = name

    base_img = cfg.base_dir / recipe["base_image"]
    if not base_img.exists():
        raise typer.BadParameter(f"base image not found: {base_img}")

    need_cmd("qemu-img", "virt-install", "virsh")
    cfg.ensure_dirs()
    _check_libvirt_connectivity(cfg)
    net = recipe["network"]
    _ensure_host_networks(cfg, net["mode"], net.get("nat_network"))

    import tempfile
    td = tempfile.mkdtemp(prefix="latita-run-")
    td_path = Path(td)

    def _cleanup() -> None:
        if vm_exists(name):
            stop_vm_libvirt(name, capture=True)
            undefine_vm_libvirt(name, capture=True)
        shutil.rmtree(td_path, ignore_errors=True)

    try:
        overlay = td_path / f"{name}.qcow2"
        run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(base_img), str(overlay)])
        run(["qemu-img", "resize", str(overlay), recipe["disk_size"]])

        keys = recipe["_keys"]
        host_pubkey = read_text(Path(keys["host_pubkey_path"]))
        lab_pubkey = read_text(Path(keys["lab_pubkey_path"])) if keys["lab_pubkey_path"] else ""

        capsule_provisions = [
            capsules.capsule_provision_fragment(c)
            for c in recipe.get("_resolved_capsules", [])
        ]

        pkg_mgr = _package_manager_for_recipe(recipe)
        osinfo = _osinfo_for_recipe(recipe)

        user_data = build_user_data(
            profile=recipe["profile"],
            guest_user=recipe["guest_user"],
            host_pubkey=host_pubkey,
            lab_pubkey=lab_pubkey,
            lab_privkey=Path(keys["lab_privkey_path"]) if keys.get("lab_privkey_path") else None,
            login_hash=recipe.get("login_hash", ""),
            provision=recipe["provision"],
            capsule_provisions=capsule_provisions,
            passwordless_sudo=recipe["passwordless_sudo"],
            package_manager=pkg_mgr,
            os_family=recipe.get("os_family", "fedora"),
        )

        wan_mac = random_mac()
        net = recipe["network"]

        ud_path = td_path / "user-data.yaml"
        if net["mode"] not in ("isolated", "none", "user"):
            net_cfg = build_network_config(wan_mac, random_mac(), net["mgmt_ip"], net["mgmt_prefix"])
            nc_path = td_path / "network-config.yaml"
            ud_path.write_text(user_data)
            nc_path.write_text(net_cfg)
        else:
            ud_path.write_text(user_data)
            nc_path = None

        iso_path = td_path / "nocloud.iso"
        _build_nocloud_iso(ud_path, nc_path, iso_path, instance_id=name)

        args = [
            "--name", name,
            "--memory", str(recipe["memory"]),
            "--vcpus", str(recipe["cpus"]),
            "--cpu", "host-passthrough",
            "--import",
            "--osinfo", osinfo,
            "--disk", f"path={overlay},format=qcow2,bus=virtio,discard=unmap",
            "--disk", f"path={iso_path},device=cdrom,readonly=on",
            "--rng", "/dev/urandom",
            "--noautoconsole",
            "--transient",
        ]

        if recipe["profile"] == "desktop":
            video_model = _detect_video_model()
            args.extend([
                "--graphics", "spice,listen=127.0.0.1",
                "--video", video_model,
            ])
            if not cfg.is_session:
                args.extend(["--channel", "spicevmc"])
        else:
            args.extend(["--graphics", "none"])
            args.extend(["--serial", "pty"])

        net_mode = net["mode"]
        if net_mode in ("isolated", "none"):
            args.extend(["--network", "none"])
        elif net_mode == "user":
            if cfg.is_session:
                ssh_port = _find_free_port()
                args.extend(["--network", "none"])
                args.append(f"--qemu-commandline=-netdev user,id=net0,hostfwd=tcp::{ssh_port}-:22 -device virtio-net-pci,netdev=net0,mac={wan_mac},addr=0x10")
            else:
                args.extend(["--network", f"type=user,model=virtio,mac={wan_mac}"])
        elif net_mode == "direct" and net.get("uplink"):
            args.extend(["--network", f"type=direct,source={net['uplink']},source_mode=private,model=virtio,mac={wan_mac}"])
        else:
            nat_network = net.get("nat_network", "default")
            args.extend(["--network", f"network={nat_network},model=virtio,mac={wan_mac}"])
        if not cfg.is_session and net_mode not in ("isolated", "none"):
            args.extend(["--network", f"network={cfg.net_name},model=virtio,mac={random_mac()}"])

        sec = recipe["security"]
        profile = SecurityProfile.from_dict(sec)
        args = apply_hardening_to_args(profile, args, vm_name=name)

        console.print(f"Running transient {name}...", style="cyan")
        virt_install(args)

        if command:
            console.print(f"Transient VM started. Waiting for shutdown...", style="dim")
            import time
            while vm_exists(name):
                time.sleep(1)
        else:
            console.print(f"Transient VM {name} is running. Connect with: latita ssh {name}", style="green")
            console.print(f"It will disappear on shutdown.", style="dim")
    except Exception:
        _cleanup()
        raise
    else:
        _cleanup()


# ---------------------------------------------------------------------------
# Revive
# ---------------------------------------------------------------------------

def revive_instance(name: str) -> None:
    validate_name(name)
    cfg = get_config()
    if vm_exists(name):
        state = get_vm_state(name)
        if state == "running":
            console.print(f"{name} is already running", style="yellow")
            return
        start_instance(name)
        return

    recipe = read_instance_recipe(name)
    spec = read_instance_spec(name)
    if not recipe or not spec:
        raise typer.BadParameter(f"no saved recipe/spec for {name}; cannot revive")

    overlay = Path(spec["overlay"])
    if not overlay.exists():
        raise typer.BadParameter(f"overlay missing: {overlay}")

    if not cfg.is_session:
        ensure_network_exists(cfg.net_name)
        if spec.get("nat_network"):
            ensure_network_active(spec["nat_network"])

    net_mode = spec.get("net_mode", "nat")
    nat_network = spec.get("nat_network", "default")
    uplink = spec.get("uplink")

    if cfg.is_session:
        net_mode = "user"

    wan_mac = spec.get("wan_mac", random_mac())
    mgmt_mac = spec.get("mgmt_mac", random_mac())
    osinfo = spec.get("osinfo", "detect=on,name=fedora43,require=off")

    args = [
        "--name", name,
        "--memory", str(recipe.get("memory", 4096)),
        "--vcpus", str(recipe.get("cpus", 2)),
        "--cpu", "host-passthrough",
        "--import",
        "--osinfo", osinfo,
        "--disk", f"path={overlay},format=qcow2,bus=virtio,discard=unmap",
        "--rng", "/dev/urandom",
        "--noautoconsole",
    ]
    if spec.get("transient"):
        args.append("--transient")

    if spec.get("graphics") == "spice":
        video_model = spec.get("video_model")
        if not video_model:
            video_model = _detect_video_model()
        args.extend(["--graphics", "spice,listen=127.0.0.1", "--video", video_model])
        if not cfg.is_session:
            args.extend(["--channel", "spicevmc"])
    else:
        args.extend(["--graphics", "none"])
        args.extend(["--serial", "pty"])

    ssh_port = None
    if net_mode in ("isolated", "none"):
        args.extend(["--network", "none"])
    elif net_mode == "direct" and uplink and iface_exists(uplink):
        args.extend(["--network", f"type=direct,source={uplink},source_mode=private,model=virtio,mac={wan_mac}"])
    elif net_mode == "user":
        if cfg.is_session:
            existing_env = read_instance_env(name)
            existing_port = existing_env.get("FORWARDED_SSH_PORT")
            ssh_port = int(existing_port) if existing_port else _find_free_port()
            args.extend(["--network", "none"])
            args.append(f"--qemu-commandline=-netdev user,id=net0,hostfwd=tcp::{ssh_port}-:22 -device virtio-net-pci,netdev=net0,mac={wan_mac},addr=0x10")
        else:
            args.extend(["--network", f"type=user,model=virtio,mac={wan_mac}"])
    else:
        args.extend(["--network", f"network={nat_network},model=virtio,mac={wan_mac}"])
    if not cfg.is_session and net_mode not in ("isolated", "none"):
        args.extend(["--network", f"network={cfg.net_name},model=virtio,mac={mgmt_mac}"])

    sec_dict = recipe.get("security", {})
    profile = SecurityProfile.from_dict(sec_dict)
    args = apply_hardening_to_args(profile, args, vm_name=name)

    iso_path = cfg.inst_dir / name / "nocloud.iso"
    if iso_path.exists():
        args.extend(["--disk", f"path={iso_path},device=cdrom,readonly=on"])

    virt_install(args)
    metadata.increment_run_count(name)

    # Update forwarded port in env if it changed during revive
    if ssh_port:
        existing_env = read_instance_env(name)
        if existing_env.get("FORWARDED_SSH_PORT") != str(ssh_port):
            existing_env["FORWARDED_SSH_PORT"] = str(ssh_port)
            write_instance_env(name, existing_env)

    console.print(f"Revived {name}", style="green")


# ---------------------------------------------------------------------------
# SSH / Connect / Live capsule apply
# ---------------------------------------------------------------------------

def get_vm_ip(name: str) -> str | None:
    # Prefer dynamic discovery (guest agent, DHCP lease, ARP) over static config
    addresses = get_vm_ip_addresses(name)
    if addresses:
        return addresses[0]["ip"]
    env = read_instance_env(name)
    mgmt_ip = env.get("MGMT_IP")
    if mgmt_ip:
        return mgmt_ip
    recipe = read_instance_recipe(name)
    if recipe:
        net = recipe.get("network", {})
        return net.get("mgmt_ip")
    return None


def _discover_session_port(name: str) -> str | None:
    cfg = get_config()
    cp = run(
        ['virsh', '-c', cfg.libvirt_uri, 'dumpxml', name],
        capture=True, check=False,
    )
    if cp.returncode != 0:
        return None
    import re
    m = re.search(r'hostfwd=tcp::([0-9]+)-:22', cp.stdout)
    if m:
        return m.group(1)
    return None


def ssh_instance(name: str, command: str | None = None) -> None:
    validate_name(name)
    env = read_instance_env(name)
    forwarded_port = env.get("FORWARDED_SSH_PORT")
    user = env.get("GUEST_USER", "dev")

    if forwarded_port:
        ip = "localhost"
        ssh_port = forwarded_port
    else:
        cfg = get_config()
        if cfg.is_session:
            discovered = _discover_session_port(name)
            if discovered:
                ip = "localhost"
                ssh_port = discovered
            else:
                raise typer.BadParameter(
                    f"Could not find SSH port for {name}. "
                    f"Run: virsh -c qemu:///session dumpxml {name} | grep hostfwd"
                )
        else:
            ip = get_vm_ip(name)
            ssh_port = None
    if not ip:
        raise typer.BadParameter(f"cannot resolve IP for {name}")

    recipe = read_instance_recipe(name)
    key = None
    lab_key_path = None
    if recipe:
        keys = recipe.get("_keys", {})
        lab_priv = keys.get("lab_privkey_path")
        host_priv = keys.get("host_pubkey_path", "").replace(".pub", "")
        for candidate in (lab_priv, host_priv):
            if candidate and Path(candidate).exists():
                key = candidate
                break
    if not key:
        for kname in ("id_ed25519", "id_ecdsa", "id_rsa"):
            kp = Path.home() / ".ssh" / kname
            if kp.exists():
                key = str(kp)
                break
    if not key:
        raise typer.BadParameter("no SSH private key found")

    cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-i", key]
    if ssh_port:
        cmd.extend(["-p", str(ssh_port)])
    cmd.append(f"{user}@{ip}")
    if command:
        cmd.append(command)
    subprocess.run(cmd)


def connect_instance(name: str) -> None:
    validate_name(name)
    env = read_instance_env(name)
    uri = env.get("LIBVIRT_URI") or get_config().libvirt_uri

    if not shutil.which("virt-viewer"):
        console.print("[yellow]virt-viewer is not installed[/yellow]")
        ssh_instance(name)
        return

    # Verify domain exists at this URI
    check = subprocess.run(
        ["virsh", "-c", uri, "domstate", name],
        capture_output=True, text=True, timeout=5,
    )
    if check.returncode != 0:
        console.print(f"[red]Cannot find '{name}' at {uri}.[/red]")
        return

    subprocess.run(["virt-viewer", "--connect", uri, "--wait", name])



def _wait_for_ssh_ready(ip: str, user: str, key: str, port: str | None, max_wait: float = 60.0) -> bool:
    """Poll SSH until key authentication succeeds or timeout.

    This handles the race condition where cloud-init has not yet created
    the guest user and injected authorized_keys. Retries every 2 seconds.
    """
    import time

    test_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=3",
        "-i", key,
    ]
    if port:
        test_cmd.extend(["-p", str(port)])
    test_cmd.extend([f"{user}@{ip}", "true"])

    start = time.monotonic()
    attempt = 0
    while time.monotonic() - start < max_wait:
        attempt += 1
        result = subprocess.run(test_cmd, capture_output=True, text=True)
        if result.returncode == 0:
            if attempt > 1:
                console.print(f"  [green]SSH ready after {attempt} attempts[/green]")
            return True
        stderr = result.stderr.lower()
        if "permission denied" in stderr or "connection refused" in stderr or "connection timed out" in stderr:
            time.sleep(2.0)
            continue
        # Any other error is unexpected — bail immediately
        console.print(f"[yellow]SSH probe failed: {result.stderr.strip()}[/yellow]")
        return False

    console.print(f"[yellow]SSH not ready after {max_wait}s. Capsule apply may fail.[/yellow]")
    return False


def apply_capsule_live(name: str, capsule_name: str) -> None:
    validate_name(name)
    recipe = read_instance_recipe(name)
    spec = read_instance_spec(name)
    if not recipe:
        recipe = {
            "profile": "unknown",
            "os_family": "unknown",
            "guest_user": "dev",
            "_keys": {},
        }

    # Resolve dependencies so they are applied in order before the requested capsule
    try:
        resolved = capsules.resolve_capsules(
            [capsule_name],
            profile=recipe.get("profile", ""),
            os_family=recipe.get("os_family", ""),
        )
    except Exception as exc:
        console.print(f"[yellow]Failed to resolve capsule dependencies: {exc}[/yellow]")
        return

    # Check network mode and warn if isolated
    net_mode = ""
    if recipe:
        net_mode = recipe.get("network", {}).get("mode", "")
    elif spec:
        net_mode = spec.get("net_mode", "")
    if net_mode in ("isolated", "none", ""):
        console.print(
            f"[yellow]Warning: VM '{name}' has no internet access (network: {net_mode or 'unknown'}). "
            f"Capsules that download packages or images will fail.[/yellow]"
        )

    env = read_instance_env(name)
    forwarded_port = env.get("FORWARDED_SSH_PORT")
    if forwarded_port:
        ip = "localhost"
        ssh_port = forwarded_port
    else:
        ip = get_vm_ip(name)
        ssh_port = None
    if not ip:
        console.print(f"[yellow]Cannot resolve IP for {name}. Is the VM running?[/yellow]")
        console.print("[yellow]Try: latita start {name}[/yellow]")
        return

    keys = recipe.get("_keys", {})
    key = None
    for candidate in (keys.get("lab_privkey_path"), keys.get("host_pubkey_path", "").replace(".pub", "")):
        if candidate and Path(candidate).exists():
            key = candidate
            break
    if not key:
        for kname in ("id_ed25519", "id_ecdsa", "id_rsa"):
            kp = Path.home() / ".ssh" / kname
            if kp.exists():
                key = str(kp)
                break
    if not key:
        console.print("[yellow]No SSH private key found. Cannot apply capsule remotely.[/yellow]")
        return

    applied = read_applied_capsules(name)

    for cap_name, capsule in resolved:
        ok, reason = capsules.check_capsule_compatibility(
            capsule,
            profile=recipe.get("profile", ""),
            os_family=recipe.get("os_family", ""),
        )
        if not ok:
            console.print(f"[yellow]Capsule '{cap_name}' incompatible: {reason}[/yellow]")
            continue

        # Re-apply guard
        if cap_name in applied:
            if not typer.confirm(
                f"Capsule '{cap_name}' was already applied to '{name}'. Re-apply (replace)?",
                default=False,
            ):
                console.print(f"Skipped re-applying '{cap_name}'.", style="yellow")
                continue

        user = capsules.capsule_live_user(capsule, recipe.get("guest_user", "dev"))
        cmds = capsules.format_live_commands(capsule, recipe.get("guest_user", "dev"))
        if not cmds:
            console.print(f"Capsule '{cap_name}' has no live commands", style="yellow")
            continue

        # Wait for SSH key auth to be ready (cloud-init race condition)
        _wait_for_ssh_ready(ip, user, key, ssh_port)

        script = "\n".join(cmds)
        ssh_cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=5",
            "-i", key,
        ]
        if ssh_port:
            ssh_cmd.extend(["-p", str(ssh_port)])
        ssh_cmd.extend([f"{user}@{ip}", f"bash -lc {shlex.quote(script)}"])

        console.print(f"Applying capsule '{cap_name}' to {name} via SSH...", style="cyan")
        success = _stream_ssh(ssh_cmd)
        if not success:
            console.print(f"[yellow]Capsule apply failed. Try debugging manually: latita ssh {name}[/yellow]")
            return

        console.print(f"[green]Capsule '{cap_name}' applied successfully[/green]")
        append_applied_capsule(name, cap_name)

        # Run verify command if defined
        verify_cmd = capsules.capsule_verify_command(capsule)
        if verify_cmd:
            console.print(f"Verifying capsule '{cap_name}'...", style="dim")
            formatted_verify = capsules.format_verify_command(capsule, recipe.get("guest_user", "dev"))
            v_ssh = ssh_cmd[:-1] + [f"bash -lc {shlex.quote(formatted_verify)}"]
            _stream_ssh(v_ssh)


def _stream_ssh(cmd: list[str]) -> bool:
    """Run an SSH command and stream stdout/stderr in real time.
    Returns True if returncode == 0."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            print(line, end="")
    finally:
        proc.wait()
    return proc.returncode == 0


def _build_ssh_base(name: str) -> tuple[list[str], str, str] | None:
    """Build the common SSH command prefix for a VM.

    Returns (ssh_cmd_base, user, target) or None if the VM has no
    reachable endpoint or no usable key.
    """
    env = read_instance_env(name)
    recipe = read_instance_recipe(name)
    forwarded_port = env.get("FORWARDED_SSH_PORT")
    if forwarded_port:
        target = "localhost"
        ssh_cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes",
            "-p", str(forwarded_port),
        ]
    else:
        target = get_vm_ip(name)
        if not target:
            target = env.get("MGMT_IP", "")
        if not target:
            return None
        ssh_cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes",
        ]

    user = env.get("GUEST_USER", "dev")
    if recipe:
        user = recipe.get("guest_user", user)

    keys = recipe.get("_keys", {}) if recipe else {}
    key = None
    for candidate in (
        keys.get("lab_privkey_path"),
        keys.get("host_pubkey_path", "").replace(".pub", ""),
    ):
        if candidate and Path(candidate).exists():
            key = candidate
            break
    if not key:
        for kname in ("id_ed25519", "id_ecdsa", "id_rsa"):
            kp = Path.home() / ".ssh" / kname
            if kp.exists():
                key = str(kp)
                break
    if not key:
        return None

    ssh_cmd.extend(["-i", key])
    return ssh_cmd, user, target


def _ssh_run(name: str, command: str, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    """Run a single SSH command on a VM and return the result."""
    base = _build_ssh_base(name)
    if not base:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="no ssh base")
    ssh_cmd, user, target = base
    cmd = list(ssh_cmd)
    cmd.extend([f"{user}@{target}", command])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _probe_cloud_init_status(name: str, timeout: int = 15) -> str:
    """Non-blocking check of cloud-init status inside the VM.

    Returns one of: 'done', 'running', 'error', 'n/a'.
    """
    cp = _ssh_run(name, "sudo cloud-init status 2>/dev/null", timeout=timeout)
    if cp.returncode not in (0, 1, 2):
        return "n/a"
    out = cp.stdout.lower()
    if "status: done" in out:
        return "done"
    if "status: running" in out:
        return "running"
    if "status: error" in out:
        return "error"
    return "n/a"


def fetch_vm_error_log(name: str) -> str:
    """Collect diagnostic logs from a running VM via SSH.

    Returns a string with cloud-init status, failed systemd units,
    and recent error-level journal entries.  Also persists the log
    to ``<instance_dir>/last-errors.log`` so it survives VM destruction.
    """
    from .metadata import error_log_path

    if get_vm_state(name) != "running":
        p = error_log_path(name)
        if p.exists():
            return p.read_text()
        return "VM is not running — no logs available."

    sections = []

    cp = _ssh_run(name, "sudo cloud-init status --long 2>/dev/null", timeout=15)
    sections.append(f"=== cloud-init status ===\n{cp.stdout.strip() or cp.stderr.strip() or '(no output)'}")

    cp = _ssh_run(name, "sudo systemctl list-units --failed --no-legend 2>/dev/null", timeout=15)
    sections.append(f"=== Failed systemd units ===\n{cp.stdout.strip() or '(none)'}")

    cp = _ssh_run(name, "sudo journalctl -p err --since today -b -n 30 --no-pager 2>/dev/null", timeout=15)
    sections.append(f"=== Recent errors (journalctl -p err) ===\n{cp.stdout.strip() or '(none)'}")

    log_text = "\n\n".join(sections)
    p = error_log_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(log_text)
    p.chmod(0o600)
    return log_text


def _probe_desktop_readiness(name: str, timeout: int = 15) -> str:
    """Check whether the desktop environment inside the VM is ready.

    Returns one of: 'ready', 'starting', 'n/a'.
    """
    recipe = read_instance_recipe(name)
    template_name = recipe.get("template_name", "") if recipe else ""
    profile = recipe.get("profile", "") if recipe else ""

    if profile != "desktop":
        return "n/a"

    # desktop-minimal uses autologin+startx, not a display manager
    if template_name == "desktop-minimal":
        cp = _ssh_run(
            name,
            "ps aux | grep -E 'openbox' | grep -v grep >/dev/null 2>&1",
            timeout=timeout,
        )
        if cp.returncode == 0:
            return "ready"
        return "starting"

    # Check graphical.target first (universal for display-manager desktops)
    cp = _ssh_run(name, "systemctl is-active graphical.target 2>/dev/null", timeout=timeout)
    if cp.returncode != 0:
        return "starting"

    # Template-specific checks    if template_name == "desktop":
        cp = _ssh_run(name, "systemctl is-active lightdm 2>/dev/null", timeout=timeout)
        if cp.returncode == 0:
            return "ready"
        return "starting"

    # Unknown desktop template — graphical.target is the best we can do
    return "ready"


def get_vm_init_state(name: str) -> dict[str, str]:
    """Return the initialization state of a VM (non-blocking).

    Keys:
      - cloud_init : 'done' | 'running' | 'error' | 'n/a'
      - desktop    : 'ready' | 'starting' | 'n/a'
      - overall    : 'ready' | 'initializing' | 'failed' | 'n/a'
    """
    state = get_vm_state(name)
    if state != "running":
        return {"cloud_init": "n/a", "desktop": "n/a", "overall": "n/a"}

    base = _build_ssh_base(name)
    if not base:
        return {"cloud_init": "n/a", "desktop": "n/a", "overall": "initializing"}

    cloud = _probe_cloud_init_status(name)
    desktop = _probe_desktop_readiness(name)

    if cloud == "error":
        overall = "failed"
    elif cloud == "done" and desktop in ("ready", "n/a"):
        overall = "ready"
    elif cloud in ("running", "done") and desktop == "starting":
        overall = "initializing"
    elif cloud == "n/a" and desktop == "n/a":
        overall = "n/a"
    else:
        overall = "initializing"

    return {"cloud_init": cloud, "desktop": desktop, "overall": overall}


def wait_for_vm_ready(name: str, max_wait: float | None = None) -> bool:
    """Block until a VM finishes cloud-init and (for desktops) the GUI is ready.

    Prints progress messages.  Returns True when ready, False on timeout or
    cloud-init error.
    """
    import time

    validate_name(name)
    recipe = read_instance_recipe(name)
    profile = recipe.get("profile", "") if recipe else ""
    is_desktop = profile == "desktop"

    if max_wait is None:
        max_wait = 480.0 if is_desktop else 300.0

    console.print(f"[cyan]Waiting for {name} to be ready...[/cyan]")

    # Stage 1: SSH authentication ready
    base = _build_ssh_base(name)
    if not base:
        console.print(f"[yellow]Cannot determine SSH endpoint for {name}.[/yellow]")
        return False
    ssh_cmd, user, target = base

    start = time.monotonic()
    attempt = 0
    while time.monotonic() - start < max_wait:
        attempt += 1
        cp = subprocess.run(
            ssh_cmd + [f"{user}@{target}", "true"],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode == 0:
            if attempt > 1:
                console.print(f"  [green]SSH ready after {attempt} attempts[/green]")
            break
        time.sleep(2.0)
    else:
        console.print(f"[red]SSH not ready for {name} after {int(max_wait)}s.[/red]")
        return False

    # Stage 2: cloud-init status (polling, non-blocking)
    elapsed = int(time.monotonic() - start)
    remaining = max_wait - elapsed
    if remaining <= 0:
        console.print(f"[red]Timeout waiting for {name}.[/red]")
        return False

    console.print(f"  [dim]Waiting for cloud-init (up to {int(remaining)}s)...[/dim]")
    ci_start = time.monotonic()
    while time.monotonic() - ci_start < remaining:
        cp = subprocess.run(
            ssh_cmd + [f"{user}@{target}", "sudo cloud-init status 2>/dev/null"],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode not in (0, 1, 2):
            break
        out = cp.stdout.lower()
        if "status: done" in out:
            console.print(f"  [green]Cloud-init finished[/green]")
            elapsed = int(time.monotonic() - start)
            remaining = max_wait - elapsed
            break
        if "status: error" in out:
            console.print(f"  [red]Cloud-init finished with errors:[/red]")
            console.print(f"  {cp.stdout.strip()}")
            return False
        time.sleep(5.0)
    else:
        console.print(f"  [yellow]Cloud-init did not finish within {int(remaining)}s[/yellow]")
        # Not a hard failure — the VM is still usable via SSH

    # Stage 3: desktop readiness (desktop profiles only)
    if is_desktop:
        elapsed = int(time.monotonic() - start)
        remaining = max_wait - elapsed
        if remaining <= 0:
            console.print(f"[red]Timeout before desktop check.[/red]")
            return False

        template_name = recipe.get("template_name", "") if recipe else ""
        console.print(f"  [dim]Waiting for desktop environment ({template_name})...[/dim]")

        check_cmd = "systemctl is-active graphical.target"
        if template_name == "desktop-minimal":
            check_cmd = "ps aux | grep -E 'openbox' | grep -v grep >/dev/null"
        elif template_name == "desktop":
            check_cmd = "systemctl is-active graphical.target && systemctl is-active lightdm"

        desk_start = time.monotonic()
        while time.monotonic() - desk_start < remaining:
            cp = subprocess.run(
                ssh_cmd + [f"{user}@{target}", f"bash -lc {shlex.quote(check_cmd)}"],
                capture_output=True, text=True, timeout=10,
            )
            if cp.returncode == 0:
                console.print(f"  [green]Desktop ready[/green]")
                break
            time.sleep(3.0)
        else:
            console.print(f"  [yellow]Desktop did not become ready within timeout.[/yellow]")
            # Not a hard failure — the VM is still usable via SSH

    console.print(f"[green]{name} is ready[/green]")
    return True


# ---------------------------------------------------------------------------
# Inventory / listing
# ---------------------------------------------------------------------------

def _is_latita_instance(name: str, cfg: Config) -> bool:
    """Return True if name is a latita-managed instance (has metadata files)."""
    inst_dir = cfg.inst_dir / name
    if not inst_dir.is_dir():
        return False
    return any((inst_dir / f).exists() for f in ("recipe.json", "spec.json", "env"))


def _looks_like_latita_created(name: str, cfg: Config) -> bool:
    """Return True if the VM's disk path suggests it was created by latita.

    Detects orphaned VMs (disk files from /tmp/latita-run-* or latita vault) that
    lost their metadata.
    """
    cp = run(
        ["virsh", "-c", cfg.libvirt_uri, "dumpxml", name],
        capture=True, check=False,
    )
    if cp.returncode != 0:
        return False
    xml = cp.stdout
    import re
    for match in re.finditer(r'<source file="([^"]*)"', xml):
        disk_path = match.group(1)
        if "/latita-run-" in disk_path:
            return True
        vault = str(cfg.root_dir)
        if disk_path.startswith(vault):
            return True
    return False


def scan_instances() -> list[dict[str, Any]]:
    cfg = get_config()
    cfg.ensure_dirs()
    inst_names = {d.name for d in cfg.inst_dir.iterdir() if d.is_dir()} if cfg.inst_dir.exists() else set()
    names = set(inst_names)
    virsh_names = set()
    try:
        cp = run(["virsh", "-c", cfg.libvirt_uri, "list", "--all", "--name"], capture=True, check=False)
        if cp.returncode == 0:
            virsh_names = {line.strip() for line in cp.stdout.splitlines() if line.strip()}
            names |= virsh_names
    except Exception:
        pass

    entries: list[dict[str, Any]] = []
    for name in sorted(names):
        is_latita = _is_latita_instance(name, cfg)
        is_orphaned = not is_latita and _looks_like_latita_created(name, cfg)
        recipe = read_instance_recipe(name)
        spec = read_instance_spec(name)
        env = read_instance_env(name)
        overlay = cfg.inst_dir / name / f"{name}.qcow2"
        downloading_marker = cfg.inst_dir / name / ".downloading"
        creating_marker = cfg.inst_dir / name / ".creating"

        if downloading_marker.exists():
            entries.append({
                "name": name,
                "source": "latita",
                "profile": recipe.get("profile", "unknown") if recipe else "unknown",
                "template": recipe.get("template_name", "") if recipe else "",
                "status": "downloading",
                "mgmt_ip": "",
                "ip": "",
                "interfaces": {},
                "cpus": recipe.get("cpus") if recipe else None,
                "memory": recipe.get("memory") if recipe else None,
                "transient": False,
                "destroy_on_stop": False,
                "expire_at": None,
                "max_runs": None,
                "run_count": 0,
                "overlay_exists": False,
                "applied_capsules": [],
            })
            continue

        if creating_marker.exists():
            entries.append({
                "name": name,
                "source": "latita",
                "profile": recipe.get("profile", "unknown") if recipe else "unknown",
                "template": recipe.get("template_name", "") if recipe else "",
                "status": "creating",
                "mgmt_ip": "",
                "ip": "",
                "interfaces": {},
                "cpus": recipe.get("cpus") if recipe else None,
                "memory": recipe.get("memory") if recipe else None,
                "transient": False,
                "destroy_on_stop": False,
                "expire_at": None,
                "max_runs": None,
                "run_count": 0,
                "overlay_exists": False,
                "applied_capsules": [],
            })
            continue

        state = ""
        defined = False
        try:
            st = get_vm_state(name)
            state = st
            defined = bool(st)
        except Exception:
            pass

        interfaces = {}
        if is_latita and state == "running":
            try:
                interfaces = get_vm_interfaces(name)
            except Exception:
                pass

        mgmt_ip = env.get("MGMT_IP", "")
        transient = env.get("TRANSIENT", "no") == "yes"
        destroy_on_stop = env.get("DESTROY_ON_STOP", "no") == "yes"
        expire_at = spec.get("expire_at")
        max_runs = spec.get("max_runs")
        run_count = spec.get("run_count", 0)

        if is_latita:
            status = state or ("stored" if overlay.exists() else "broken")
        else:
            status = state if state else "unknown"
        wan_ip = get_vm_wan_ip(name) if is_latita and state == "running" else ""
        cpus = recipe.get("cpus") if recipe else None
        memory = recipe.get("memory") if recipe else None
        entries.append({
            "name": name,
            "source": "orphaned" if is_orphaned else ("latita" if is_latita else "external"),
            "profile": recipe.get("profile", env.get("PROFILE", "unknown")) if recipe else env.get("PROFILE", "unknown"),
            "template": recipe.get("template_name", env.get("TEMPLATE", "")) if recipe else env.get("TEMPLATE", ""),
            "status": status,
            "mgmt_ip": mgmt_ip,
            "ip": wan_ip or mgmt_ip,
            "interfaces": interfaces,
            "cpus": cpus,
            "memory": memory,
        "transient": transient,
        "destroy_on_stop": destroy_on_stop,
            "expire_at": expire_at,
            "max_runs": max_runs,
            "run_count": run_count,
            "overlay_exists": overlay.exists(),
            "applied_capsules": spec.get("applied_capsules", []) if spec else [],
        })
    return entries


def list_instances() -> None:
    entries = scan_instances()
    if not entries:
        console.print("No instances found", style="yellow")
        return
    table = Table(title="Instances")
    table.add_column("Name")
    table.add_column("Source")
    table.add_column("Template")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Mgmt IP")
    table.add_column("Constraints")
    for e in entries:
        ctype = []
        if e["transient"]:
            ctype.append("transient")
        if e["destroy_on_stop"]:
            ctype.append("ephemeral")
        constraints = []
        if e["expire_at"]:
            constraints.append(f"expires {e['expire_at'][:10]}")
        if e["max_runs"] is not None:
            constraints.append(f"runs {e['run_count']}/{e['max_runs']}")
        if e.get("applied_capsules"):
            constraints.append("caps: " + ",".join(e["applied_capsules"]))
        table.add_row(
            e["name"],
            e["source"],
            e["template"] or e["profile"],
            ", ".join(ctype) if ctype else "persistent",
            e["status"],
            e["mgmt_ip"] or "-",
            ", ".join(constraints) if constraints else "-",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------

def doctor() -> None:
    import os, subprocess, sys
    cfg = get_config()
    console.print('\n[bold]Doctor — checking system dependencies[/bold]\n')

    issues = []

    for cmd in ['virsh', 'virt-install', 'qemu-img', 'ssh', 'curl', 'ssh-keygen', 'openssl']:
        status = 'ok' if shutil.which(cmd) else 'MISSING'
        if status == 'MISSING':
            issues.append(cmd)
        console.print(f'  {cmd}: [green]{status}[/green]' if status == 'ok' else f'  {cmd}: [red]{status}[/red]')

    if cfg.is_session:
        console.print(f'  libvirt URI: [green]{cfg.libvirt_uri}[/green] (session mode)')
    else:
        console.print(f'  libvirt URI: [yellow]{cfg.libvirt_uri}[/yellow] (system mode — may need sudo)')

    # Mode implications
    console.print('\n[bold]Mode implications[/bold]')
    if cfg.is_session:
        console.print('  [bold]Session mode[/bold] — unprivileged, works out of the box')
        console.print('    - VMs use isolated SLIRP networking (10.0.2.0/24 each)')
        console.print('    - VMs [red]cannot[/red] reach each other (isolated per VM)')
        console.print('    - SSH access via [green]localhost:PORT[/green] with port forwarding')
        console.print('    - Use this for quick one-offs, CI, and unprivileged workstations')
        console.print('    - Switch to system mode for multi-VM labs or VM-to-VM networking')
    else:
        console.print('  [bold]System mode[/bold] — requires root / libvirtd')
        console.print('    - VMs share a NAT bridge (e.g. 192.168.122.0/24)')
        console.print('    - VMs [green]can[/green] reach each other on the same network')
        console.print('    - SSH access via direct VM IP (e.g. 192.168.122.x)')
        console.print('    - Use this for production, multi-VM labs, and networking tests')
        console.print('    - Requires libvirtd running and user in libvirt group (or sudo)')

    try:
        subprocess.run(['/usr/bin/python3', '-c', 'import gi'], check=True, capture_output=True)
        console.print('  gi (PyGObject): [green]ok[/green]')
    except subprocess.CalledProcessError:
        console.print('  gi (PyGObject): [red]MISSING[/red]')
        issues.append('gi')

    if shutil.which('virt-viewer'):
        console.print('  virt-viewer: [green]ok[/green]')
    else:
        console.print('  virt-viewer: [yellow]not installed (recommended for desktop VMs)[/yellow]')

    uid = os.getuid()
    socket_path = f'/run/user/{uid}/libvirt/virtqemud-sock'
    if os.path.exists(socket_path):
        console.print(f'  libvirtd socket: [green]running[/green] ({socket_path})')
    else:
        console.print(f'  libvirtd socket: [red]not found[/red] ({socket_path})')
        issues.append('libvirtd-socket')

    console.print(f'\n  Root: {cfg.root_dir}')
    console.print(f'  Base images: {cfg.base_dir}')

    if not issues:
        console.print('\n[green]All checks passed![/green]')
        return

    console.print(f'\n[yellow]Found {len(issues)} issue(s):[/yellow]')
    for issue in issues:
        console.print(f'  - {issue}')

    script = Path(__file__).parent.parent / 'scripts' / 'install-deps.sh'
    if script.exists():
        console.print(f'\n[bold]Run `latita doctor --install` to attempt automatic fixes,[/bold]')
        console.print(f'or run:')
        console.print(f'  python3 {script}')
    else:
        console.print(f'\nTo fix, run the install-deps script:')
        console.print(f'  python3 scripts/install-deps.sh')


def doctor_install() -> int:
    script = Path(__file__).parent.parent / 'scripts' / 'install-deps.sh'
    if not script.exists():
        console.print('[red]install-deps.sh not found[/red]')
        return 1
    import subprocess
    r = subprocess.run([sys.executable, str(script)])
    return r.returncode


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _suggest_name(profile: str) -> str:
    prefix = "desktop-" if profile == "desktop" else "vm-"
    cfg = get_config()
    used = set()
    if cfg.inst_dir.exists():
        used |= {d.name for d in cfg.inst_dir.iterdir() if d.is_dir()}
    try:
        cp = run(["virsh", "-c", cfg.libvirt_uri, "list", "--all", "--name"], capture=True, check=False)
        if cp.returncode == 0:
            used |= {line.strip() for line in cp.stdout.splitlines() if line.strip()}
    except Exception:
        pass
    for idx in range(1, 10000):
        candidate = f"{prefix}{idx:03d}"
        if candidate not in used:
            return candidate
    return f"{prefix}unknown"
