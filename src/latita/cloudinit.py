from __future__ import annotations

import json
import shlex
import textwrap
from pathlib import Path
from typing import Any


def _package_install_block(packages: list[str], package_manager: str = "dnf") -> list[str]:
    """Generate package installation lines for the bootstrap script."""
    if not packages:
        return []

    if package_manager == "apt":
        package_str = " ".join(shlex.quote(p) for p in packages)
        return [
            "",
            "export DEBIAN_FRONTEND=noninteractive",
            "apt-get update || true",
            f"apt-get install -y {package_str}",
        ]

    if package_manager == "apk":
        package_str = " ".join(shlex.quote(p) for p in packages)
        return [
            "",
            f"apk add --no-cache {package_str}",
        ]

    # Default: dnf
    package_block = (" \\\n      ").join(packages)
    return [
        "",
        "dnf clean all || true",
        "for i in 1 2 3; do",
        "  dnf -y install \\",
        f"      {package_block} && break",
        "  sleep 5",
        "done",
    ]


import re


class _SafeFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _format_value(value: Any, context: dict[str, str]) -> Any:
    if isinstance(value, str):
        return re.sub(
            r"\{(\w+)\}",
            lambda m: context.get(m.group(1), m.group(0)),
            value,
        )
    if isinstance(value, list):
        return [_format_value(item, context) for item in value]
    if isinstance(value, dict):
        return {str(key): _format_value(item, context) for key, item in value.items()}
    return value


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def _yaml_lines(value: Any, indent: int = 0) -> list[str]:
    prefix = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, str) and "\n" in item:
                lines.append(f"{prefix}{key}: |")
                lines.extend(f"{prefix}  {line}" for line in item.splitlines())
            elif isinstance(item, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.extend(_yaml_lines(item, indent + 2))
            else:
                lines.append(f"{prefix}{key}: {_yaml_scalar(item)}")
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                lines.append(f"{prefix}-")
                lines.extend(_yaml_lines(item, indent + 2))
            elif isinstance(item, str) and "\n" in item:
                lines.append(f"{prefix}- |")
                lines.extend(f"{prefix}  {line}" for line in item.splitlines())
            elif isinstance(item, list):
                lines.append(f"{prefix}-")
                lines.extend(_yaml_lines(item, indent + 2))
            else:
                lines.append(f"{prefix}- {_yaml_scalar(item)}")
        return lines
    return [f"{prefix}{_yaml_scalar(value)}"]


def _render_cloud_config(config: dict[str, Any]) -> str:
    return "#cloud-config\n" + "\n".join(_yaml_lines(config)) + "\n"


def _base_provision(profile: str, context: dict[str, str], os_family: str = "fedora") -> dict[str, Any]:
    """Base provision fragment for a profile (headless or desktop)."""
    workspace_dir = context["workspace_dir"]
    home_dir = context["home_dir"]
    guest_user = context["guest_user"]
    is_debian = os_family in ("ubuntu", "debian")
    ssh_svc = "ssh" if is_debian else "sshd"

    if profile == "desktop":
        return {
            "packages": ["openssh-server"],
            "root_commands": [
                f"systemctl enable --now {ssh_svc}",
            ],
        }

    return {
        "packages": ["openssh-server"],
        "root_commands": [
            f"systemctl enable --now {ssh_svc}",
        ],
    }


def _bootstrap_script(
    profile: str,
    fragment: dict[str, Any],
    context: dict[str, str],
    package_manager: str = "dnf",
) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "set -euxo pipefail",
        f"exec > >(tee -a /var/log/latita-bootstrap-{profile}.log) 2>&1",
    ]
    packages = fragment["packages"]
    lines.extend(_package_install_block(packages, package_manager))

    for command in fragment["root_commands"]:
        lines.extend(["", command])
    for command in fragment["user_commands"]:
        lines.extend(["", _run_as_user(context["guest_user"], command)])
    return "\n".join(lines) + "\n"


def _run_as_user(user: str, script: str) -> str:
    return f"runuser -u {shlex.quote(user)} -- bash -lc {shlex.quote(script)}"


def build_user_data(
    *,
    profile: str,
    guest_user: str,
    host_pubkey: str,
    lab_pubkey: str = "",
    lab_privkey: Path | None = None,
    login_hash: str = "",
    provision: dict[str, Any] | None = None,
    capsule_provisions: list[dict[str, Any]] | None = None,
    passwordless_sudo: bool = True,
    package_manager: str = "dnf",
    os_family: str = "fedora",
) -> str:
    home_dir = f"/home/{guest_user}"
    context = {
        "guest_user": guest_user,
        "home_dir": home_dir,
        "workspace_dir": f"{home_dir}/workspace",
        "host_pubkey": host_pubkey,
        "lab_pubkey": lab_pubkey,
        "login_hash": login_hash,
        "lab_privkey_b64": (
            _b64_file(lab_privkey) if lab_privkey else ""
        ),
    }

    base = _base_provision(profile, context, os_family=os_family)
    formatted_provision = _format_value(provision or {}, context) if provision else {}
    formatted_capsules = [_format_value(c, context) for c in (capsule_provisions or [])]
    merged = _merge_provisions(base, formatted_provision, *formatted_capsules)

    bootstrap_path = f"/root/bootstrap-{profile}.sh"
    write_files = list(merged.get("write_files", []))
    write_files.append(
        {
            "path": bootstrap_path,
            "owner": "root:root",
            "permissions": "0755",
            "content": _bootstrap_script(profile, merged, context, package_manager),
        }
    )

    config = {
        "users": [_user_definition(profile, context, passwordless_sudo, os_family=os_family)],
        "ssh_pwauth": False,
        "write_files": write_files,
        "runcmd": [bootstrap_path],
    }
    return _render_cloud_config(config)


def _b64_file(path: Path) -> str:
    import base64

    return base64.b64encode(path.read_bytes()).decode()


def _merge_provisions(*provisions: dict[str, Any]) -> dict[str, Any]:
    merged = {
        "packages": [],
        "write_files": [],
        "root_commands": [],
        "user_commands": [],
    }
    seen_packages: set[str] = set()
    seen_files: set[str] = set()
    for prov in provisions:
        for pkg in prov.get("packages", []):
            if pkg not in seen_packages:
                seen_packages.add(pkg)
                merged["packages"].append(pkg)
        for wf in prov.get("write_files", []):
            if isinstance(wf, dict):
                path = str(wf.get("path", ""))
                if path and path not in seen_files:
                    seen_files.add(path)
                    merged["write_files"].append(wf)
        for cmd in prov.get("root_commands", []):
            if cmd not in merged["root_commands"]:
                merged["root_commands"].append(cmd)
        for cmd in prov.get("user_commands", []):
            if cmd not in merged["user_commands"]:
                merged["user_commands"].append(cmd)
    return merged


def _user_definition(
    profile: str,
    context: dict[str, str],
    passwordless_sudo: bool = True,
    os_family: str = "fedora",
) -> dict[str, Any]:
    is_debian = os_family in ("ubuntu", "debian")
    user = {
        "name": context["guest_user"],
        "groups": ["sudo"] if is_debian else ["wheel"],
        "shell": "/bin/bash",
        "ssh_authorized_keys": [
            k for k in (context["host_pubkey"], context["lab_pubkey"]) if k
        ],
    }
    if passwordless_sudo:
        user["sudo"] = "ALL=(ALL) NOPASSWD:ALL"
    else:
        user["sudo"] = "ALL=(ALL) ALL"
    if context.get("login_hash"):
        user["lock_passwd"] = False
        user["passwd"] = context["login_hash"]
    return user


def build_network_config(
    wan_mac: str,
    mgmt_mac: str,
    mgmt_ip: str,
    mgmt_prefix: str = "24",
) -> str:
    return textwrap.dedent(
        f"""\
    version: 2
    ethernets:
      wan0:
        match:
          macaddress: \"{wan_mac}\"
        set-name: wan0
        dhcp4: true
        dhcp6: false
      mgmt0:
        match:
          macaddress: \"{mgmt_mac}\"
        set-name: mgmt0
        dhcp4: false
        dhcp6: false
        addresses:
          - {mgmt_ip}/{mgmt_prefix}
    """
    )
