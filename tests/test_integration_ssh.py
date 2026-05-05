from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from latita.config import Config, get_config, load_latita_template, reset_config, set_config
from latita.libvirt import get_vm_ip_addresses, get_vm_state
from latita.metadata import read_instance_env, read_instance_recipe
from latita.operations import (
    apply_capsule_live,
    create_instance,
    destroy_instance,
    get_vm_init_state,
    get_vm_ip,
    ssh_instance,
    start_instance,
    stop_instance,
    wait_for_vm_ready,
)
from latita.utils import create_lab_key

FEDORA_IMG = Path("/home/matta/latita-vault/vm/base/fedora43-base.qcow2")
FEDORA_MIN_SIZE = 500_000_000  # ~500 MB


def _wait_for_vm_ip(name: str, timeout: int = 180) -> str:
    """Poll get_vm_ip_addresses until a valid dynamic IP is discovered or timeout."""
    start = time.time()
    while time.time() - start < timeout:
        addresses = get_vm_ip_addresses(name)
        if addresses:
            ip = addresses[0]["ip"]
            if "." in ip or ":" in ip:
                return ip
        time.sleep(3)
    raise TimeoutError(f"No dynamic IP discovered for {name} after {timeout}s")


def _safe_destroy(name: str) -> None:
    """Destroy a VM, silently ignoring if it was already removed."""
    try:
        destroy_instance(name)
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
def _cleanup_lingering_test_vms():
    """Remove any test-f43-* VMs left behind by interrupted previous runs.

    Cleans up both at session start and session end.
    """
    def _clean():
        for uri in ("qemu:///system", "qemu:///session"):
            try:
                cp = subprocess.run(
                    ["virsh", "-c", uri, "list", "--all", "--name"],
                    capture_output=True, text=True, check=False, timeout=10,
                )
                for name in cp.stdout.splitlines():
                    name = name.strip()
                    if name.startswith("test-f43-"):
                        subprocess.run(
                            ["virsh", "-c", uri, "destroy", name],
                            capture_output=True, check=False, timeout=10,
                        )
                        subprocess.run(
                            ["virsh", "-c", uri, "undefine", name, "--remove-all-storage"],
                            capture_output=True, check=False, timeout=10,
                        )
            except Exception:
                pass

    _clean()
    yield
    _clean()


@pytest.fixture
def fedora_cfg(isolated_config):
    """Override the isolated config with a Fedora 43 base image symlink."""
    cfg = isolated_config
    base = cfg.base_dir / cfg.default_base_name
    if not FEDORA_IMG.exists() or FEDORA_IMG.stat().st_size < FEDORA_MIN_SIZE:
        pytest.skip("Fedora 43 base image not available or incomplete")
    base.symlink_to(FEDORA_IMG)

    # Seed SSH keys
    create_lab_key("lab1")

    # Create a test capsule in the isolated config
    capsule_dir = cfg.root_dir / "capsules"
    capsule_dir.mkdir(parents=True, exist_ok=True)
    (capsule_dir / "test-echo.cap").write_text(
        """description: Test capsule for integration tests
compatibility:
  profiles: [headless, desktop]
  os_family: [fedora]
live:
  user: dev
  commands:
    - echo "capsule-live-test"
"""
    )

    yield cfg

    # Cleanup: destroy any lingering test domains
    try:
        cp = subprocess.run(
            ["virsh", "-c", cfg.libvirt_uri, "list", "--all", "--name"],
            capture_output=True, text=True, check=False,
        )
        for name in cp.stdout.splitlines():
            name = name.strip()
            if name.startswith("test-f43-"):
                subprocess.run(
                    ["virsh", "-c", cfg.libvirt_uri, "destroy", name],
                    capture_output=True, check=False,
                )
                subprocess.run(
                    ["virsh", "-c", cfg.libvirt_uri, "undefine", name, "--remove-all-storage"],
                    capture_output=True, check=False,
                )
    except Exception:
        pass

    # Cleanup instance directory
    inst = cfg.inst_dir
    if inst.exists():
        for d in inst.iterdir():
            if d.is_dir() and d.name.startswith("test-f43-"):
                import shutil
                shutil.rmtree(d)


@pytest.mark.slow
class TestFedoraSsh:
    def _create_fedora_vm(self, cfg: Config, name: str):
        overrides = {
            "base_image": cfg.default_base_name,
            "ephemeral": {"transient": False, "destroy_on_stop": False},
            "memory": 2048,
            "cpus": 2,
            "disk_size": "10G",
            "network": {"mode": "user"},
            "security": {"no_guest_agent": False},
        }
        create_instance("headless", name=name, overrides=overrides)

    def test_dynamic_ip_discovery(self, fedora_cfg):
        """get_vm_ip discovers a real dynamic IP via the guest agent, not the static mgmt_ip."""
        cfg = fedora_cfg
        name = "test-f43-ip"
        self._create_fedora_vm(cfg, name)
        try:
            start_instance(name)
            ip = _wait_for_vm_ip(name, timeout=180)
            assert ip
            assert ip != "10.31.0.10"
            addresses = get_vm_ip_addresses(name)
            assert any(a["ip"] == ip for a in addresses)
        finally:
            _safe_destroy(name)

    def test_ssh_instance_builds_correct_command(self, fedora_cfg):
        """ssh_instance constructs the right SSH command with dynamic IP or forwarded port."""
        cfg = fedora_cfg
        name = "test-f43-ssh"
        self._create_fedora_vm(cfg, name)
        try:
            start_instance(name)
            ip = _wait_for_vm_ip(name, timeout=180)
            env = read_instance_env(name)
            forwarded_port = env.get("FORWARDED_SSH_PORT")

            real_run = subprocess.run

            def _side_effect(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                if isinstance(cmd, list) and len(cmd) > 0 and cmd[0] == "ssh":
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
                return real_run(*args, **kwargs)

            with patch("latita.operations.subprocess.run", side_effect=_side_effect) as mock_run:
                ssh_instance(name, command="echo hello")
                ssh_calls = [c for c in mock_run.call_args_list if c.args and len(c.args[0]) > 0 and c.args[0][0] == "ssh"]
                assert len(ssh_calls) == 1
                cmd = ssh_calls[0].args[0]
                assert cmd[0] == "ssh"
                assert any("lab1_ed25519" in a for a in cmd)
                assert "echo hello" in cmd
                if forwarded_port:
                    assert "localhost" in str(cmd)
                    assert "-p" in cmd
                    assert forwarded_port in str(cmd)
                else:
                    assert any(ip in a for a in cmd)
        finally:
            _safe_destroy(name)

    def test_apply_capsule_live_builds_correct_commands(self, fedora_cfg):
        """apply_capsule_live builds the right SSH command sequence for a capsule."""
        cfg = fedora_cfg
        name = "test-f43-capsule"
        self._create_fedora_vm(cfg, name)
        try:
            start_instance(name)
            ip = _wait_for_vm_ip(name, timeout=180)
            env = read_instance_env(name)
            forwarded_port = env.get("FORWARDED_SSH_PORT")

            ssh_cmds: list[list[str]] = []

            def _capture_stream_ssh(cmd):
                ssh_cmds.append(cmd)
                return True

            with patch("latita.operations._stream_ssh", side_effect=_capture_stream_ssh):
                apply_capsule_live(name, "test-echo")
                assert len(ssh_cmds) >= 1
                cmd = ssh_cmds[0]
                assert cmd[0] == "ssh"
                assert "capsule-live-test" in str(cmd)
                if forwarded_port:
                    assert "localhost" in str(cmd)
                    assert "-p" in cmd
                    assert forwarded_port in str(cmd)
                else:
                    assert any(ip in a for a in cmd)
        finally:
            _safe_destroy(name)

    def test_apply_capsule_live_runs_via_ssh(self, fedora_cfg):
        """apply_capsule_live actually executes capsule commands over SSH."""
        cfg = fedora_cfg
        name = "test-f43-capsule-real"
        self._create_fedora_vm(cfg, name)
        marker_capsule = cfg.root_dir / "capsules" / "test-marker.cap"
        marker_capsule.write_text(
            """description: Write a marker file for E2E testing
compatibility:
  profiles: [headless]
  os_family: [fedora]
live:
  user: dev
  commands:
    - echo "executed" > /tmp/capsule-applied
"""
        )
        try:
            start_instance(name)
            _wait_for_ssh_ready(name, timeout=300)
            apply_capsule_live(name, "test-marker")
            cp = _ssh_run(name, "cat /tmp/capsule-applied", timeout=30)
            assert cp.returncode == 0, f"Marker file missing: {cp.stderr}"
            assert "executed" in cp.stdout
        finally:
            _safe_destroy(name)

    def test_ensure_running_auto_starts_stopped_vm(self, fedora_cfg):
        """_ensure_running starts a stopped VM so it can be reached again."""
        from latita.cli import _ensure_running

        cfg = fedora_cfg
        name = "test-f43-autostart"
        self._create_fedora_vm(cfg, name)
        try:
            start_instance(name)
            _wait_for_vm_ip(name, timeout=180)
            stop_instance(name)
            assert get_vm_state(name) != "running"
            _ensure_running(name)
            assert get_vm_state(name) == "running"
            ip = _wait_for_vm_ip(name, timeout=180)
            assert ip
            assert ip != "10.31.0.10"
        finally:
            _safe_destroy(name)

    def test_real_ssh_to_vm_executes_command(self, fedora_cfg):
        """Real SSH end-to-end via localhost port forwarding in session mode."""
        cfg = fedora_cfg
        name = "test-f43-realssh"
        self._create_fedora_vm(cfg, name)
        try:
            start_instance(name)
            _wait_for_vm_ip(name, timeout=180)
            env = read_instance_env(name)
            forwarded_port = env.get("FORWARDED_SSH_PORT")
            assert forwarded_port, "Expected FORWARDED_SSH_PORT in session mode"

            user = env.get("GUEST_USER", "dev")
            recipe = read_instance_recipe(name)
            key = None
            if recipe:
                keys = recipe.get("_keys", {})
                lab_priv = keys.get("lab_privkey_path")
                if lab_priv and Path(lab_priv).exists():
                    key = lab_priv
            assert key, "No SSH private key found"

            start = time.time()
            result = None
            while time.time() - start < 180:
                cmd = [
                    "ssh",
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "ConnectTimeout=5",
                    "-o", "BatchMode=yes",
                    "-p", forwarded_port,
                    "-i", key,
                    f"{user}@localhost",
                    "echo ssh-real-test",
                ]
                cp = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                if cp.returncode == 0 and "ssh-real-test" in cp.stdout:
                    result = cp
                    break
                time.sleep(3)

            assert result is not None
            assert "ssh-real-test" in result.stdout
        finally:
            _safe_destroy(name)


def _wait_for_ssh_ready(name: str, timeout: int = 300) -> str:
    """Poll until SSH accepts connections, returning the target host (IP or localhost)."""
    env = read_instance_env(name)
    user = env.get("GUEST_USER", "dev")
    forwarded_port = env.get("FORWARDED_SSH_PORT")
    recipe = read_instance_recipe(name)
    key = None
    if recipe:
        keys = recipe.get("_keys", {})
        lab_priv = keys.get("lab_privkey_path")
        if lab_priv and Path(lab_priv).exists():
            key = lab_priv
    if not key:
        raise RuntimeError("No SSH private key found")

    start = time.time()
    target = None
    while time.time() - start < timeout:
        if forwarded_port:
            target = "localhost"
        else:
            try:
                target = _wait_for_vm_ip(name, timeout=10)
            except TimeoutError:
                time.sleep(3)
                continue
        cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes",
            "-i", key,
        ]
        if forwarded_port:
            cmd.extend(["-p", forwarded_port])
        cmd.extend([f"{user}@{target}", "echo ssh-ready"])

        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if cp.returncode == 0 and "ssh-ready" in cp.stdout:
            return target
        time.sleep(3)
    raise TimeoutError(f"SSH not ready for {name} after {timeout}s")


def _ssh_run(name: str, command: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run a command on a VM via SSH using the injected lab key."""
    env = read_instance_env(name)
    user = env.get("GUEST_USER", "dev")
    forwarded_port = env.get("FORWARDED_SSH_PORT")
    recipe = read_instance_recipe(name)
    key = None
    if recipe:
        keys = recipe.get("_keys", {})
        lab_priv = keys.get("lab_privkey_path")
        if lab_priv and Path(lab_priv).exists():
            key = lab_priv
    if not key:
        raise RuntimeError("No SSH private key found")

    target = _wait_for_ssh_ready(name, timeout=300)

    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        "-i", key,
    ]
    if forwarded_port:
        cmd.extend(["-p", forwarded_port])
    cmd.extend([f"{user}@{target}", command])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


@pytest.mark.slow
class TestCapsuleProvision:
    """Test capsules applied at VM creation time (cloud-init provision path).

    NOTE: Real SSH into session-mode VMs with type=user (SLIRP) networking
    is not possible from the host — the VM's internal IP (e.g. 10.0.2.15)
    is not routable. These tests verify provisioning by inspecting the
    generated cloud-init user-data.yaml and by checking VM state.
    """

    def _create_fedora_vm_with_capsule(self, cfg: Config, name: str, capsule_names: list[str]):
        overrides = {
            "base_image": cfg.default_base_name,
            "ephemeral": {"transient": False, "destroy_on_stop": False},
            "memory": 2048,
            "cpus": 2,
            "disk_size": "10G",
            "network": {"mode": "user"},
            "security": {"no_guest_agent": False},
            "capsules": capsule_names,
        }
        create_instance("headless", name=name, overrides=overrides)

    def test_podman_host_user_data_contains_packages(self, fedora_cfg):
        """VM created with podman-host has podman packages in cloud-init user-data."""
        cfg = fedora_cfg
        name = "test-f43-podman-ud"
        self._create_fedora_vm_with_capsule(cfg, name, ["podman-host"])
        ud_path = cfg.inst_dir / name / "user-data.yaml"
        assert ud_path.exists()
        ud = ud_path.read_text()
        assert "podman" in ud
        assert "slirp4netns" in ud
        assert "loginctl enable-linger" in ud
        destroy_instance(name)

    def test_code_server_user_data_has_dependency_commands(self, fedora_cfg):
        """VM created with code-server includes podman-host provisions in user-data."""
        cfg = fedora_cfg
        name = "test-f43-cs-ud"
        self._create_fedora_vm_with_capsule(cfg, name, ["code-server"])
        ud_path = cfg.inst_dir / name / "user-data.yaml"
        assert ud_path.exists()
        ud = ud_path.read_text()
        # podman-host provisions
        assert "podman" in ud
        assert "loginctl enable-linger" in ud
        # code-server provisions
        assert "code-server" in ud
        assert "ghcr.io/coder/code-server" in ud
        # Dependency should come before dependent (podman setup before container run)
        podman_idx = ud.index("podman")
        cs_idx = ud.index("code-server")
        assert podman_idx < cs_idx
        destroy_instance(name)

    def test_ai_agents_user_data_contains_nodejs(self, fedora_cfg):
        """VM created with ai-agents capsule has Node.js packages in user-data."""
        cfg = fedora_cfg
        name = "test-f43-ai-ud"
        self._create_fedora_vm_with_capsule(cfg, name, ["ai-agents"])
        ud_path = cfg.inst_dir / name / "user-data.yaml"
        assert ud_path.exists()
        ud = ud_path.read_text()
        assert "nodejs" in ud or "npm" in ud
        assert "claude-code" in ud or "anthropic" in ud
        assert "kimi-cli" in ud or "kimi" in ud
        destroy_instance(name)


class TestCapsuleHeavyIntegration:
    """Heavy tests that create real VMs and verify capsule packages via SSH.

    Each test creates a Fedora VM, waits for cloud-init to finish, then SSHs
    into the VM to verify the capsule's packages are actually installed.
    These are marked ``slow`` and require internet access inside the VM.
    """

    def _create_and_wait_for_ssh(self, cfg: Config, name: str, capsule_names: list[str]) -> tuple[str, str]:
        """Create a VM with capsules, start it, and wait for SSH to accept connections.

        Returns (forwarded_port, ssh_key_path).
        """
        overrides = {
            "base_image": cfg.default_base_name,
            "ephemeral": {"transient": False, "destroy_on_stop": False},
            "memory": 2048,
            "cpus": 2,
            "disk_size": "10G",
            "network": {"mode": "user"},
            "security": {"no_guest_agent": False},
            "capsules": capsule_names,
        }
        create_instance("headless", name=name, overrides=overrides)
        start_instance(name)

        # Wait for guest agent to report IP (signals cloud-init finished boot)
        _wait_for_vm_ip(name, timeout=180)

        env = read_instance_env(name)
        forwarded_port = env.get("FORWARDED_SSH_PORT")
        assert forwarded_port, "Expected FORWARDED_SSH_PORT in session mode"

        recipe = read_instance_recipe(name)
        key = None
        if recipe:
            keys = recipe.get("_keys", {})
            lab_priv = keys.get("lab_privkey_path")
            if lab_priv and Path(lab_priv).exists():
                key = lab_priv
        assert key, "No SSH private key found"

        # Poll until SSH accepts connections
        user = env.get("GUEST_USER", "dev")
        start = time.time()
        while time.time() - start < 300:
            cp = subprocess.run(
                [
                    "ssh",
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "ConnectTimeout=5",
                    "-o", "BatchMode=yes",
                    "-p", forwarded_port,
                    "-i", key,
                    f"{user}@localhost",
                    "echo ssh-ready",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if cp.returncode == 0 and "ssh-ready" in cp.stdout:
                break
            time.sleep(5)
        else:
            destroy_instance(name)
            raise TimeoutError(f"SSH not ready for {name} after 300s")

        return forwarded_port, key

    def _ssh_run(self, port: str, key: str, user: str, command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=10",
                "-o", "BatchMode=yes",
                "-p", port,
                "-i", key,
                f"{user}@localhost",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _retry_ssh_command(
        self,
        port: str,
        key: str,
        user: str,
        command: str,
        timeout: int = 300,
        interval: int = 5,
    ) -> subprocess.CompletedProcess[str]:
        """Retry an SSH command until it succeeds or timeout is reached."""
        start = time.time()
        last_cp = None
        while time.time() - start < timeout:
            cp = self._ssh_run(port, key, user, command)
            if cp.returncode == 0:
                return cp
            last_cp = cp
            time.sleep(interval)
        raise AssertionError(
            f"Command '{command}' did not succeed within {timeout}s. "
            f"Last rc={last_cp.returncode}, stderr={last_cp.stderr}"
        )

    @pytest.mark.slow
    def test_podman_host_installs_podman(self, fedora_cfg):
        """podman-host capsule installs podman inside the VM."""
        cfg = fedora_cfg
        name = "test-f43-podman-heavy"
        try:
            port, key = self._create_and_wait_for_ssh(cfg, name, ["podman-host"])
            cp = self._retry_ssh_command(port, key, "dev", "podman --version")
            assert "podman" in cp.stdout.lower()
        finally:
            _safe_destroy(name)

    @pytest.mark.slow
    def test_tailscale_installs_tailscale(self, fedora_cfg):
        """tailscale capsule installs tailscale inside the VM."""
        cfg = fedora_cfg
        name = "test-f43-tailscale-heavy"
        try:
            port, key = self._create_and_wait_for_ssh(cfg, name, ["tailscale"])
            cp = self._retry_ssh_command(port, key, "dev", "tailscale --version || tailscale version")
            assert "tailscale" in cp.stdout.lower()
        finally:
            _safe_destroy(name)

    @pytest.mark.slow
    def test_whisper_installs_build_tools(self, fedora_cfg):
        """whisper capsule installs git, make, gcc inside the VM."""
        cfg = fedora_cfg
        name = "test-f43-whisper-heavy"
        try:
            port, key = self._create_and_wait_for_ssh(cfg, name, ["whisper"])
            for cmd in ("git --version", "make --version", "gcc --version"):
                cp = self._retry_ssh_command(port, key, "dev", cmd)
                assert cp.returncode == 0, f"{cmd} failed: {cp.stderr}"
        finally:
            _safe_destroy(name)

    @pytest.mark.slow
    def test_ai_agents_installs_nodejs(self, fedora_cfg):
        """ai-agents capsule installs nodejs and npm inside the VM."""
        cfg = fedora_cfg
        name = "test-f43-ai-heavy"
        try:
            port, key = self._create_and_wait_for_ssh(cfg, name, ["ai-agents"])
            cp = self._retry_ssh_command(port, key, "dev", "node --version")
            assert "v" in cp.stdout
            cp = self._retry_ssh_command(port, key, "dev", "npm --version")
            assert cp.returncode == 0, f"npm --version failed: {cp.stderr}"
        finally:
            _safe_destroy(name)


@pytest.mark.slow
class TestTemplateProvisioning:
    """End-to-end tests: create VM from each template and verify ALL provisioned packages are installed."""

    TEMPLATES = [
        ("headless", "fedora", "rpm -q"),
        ("desktop", "fedora", "rpm -q"),
        ("desktop-minimal", "fedora", "rpm -q"),
    ]

    def _create_vm_from_template(self, cfg: Config, name: str, template: str):
        overrides = {
            "base_image": cfg.default_base_name,
            "ephemeral": {"transient": False, "destroy_on_stop": False},
            "memory": 2048,
            "cpus": 2,
            "disk_size": "10G",
            "network": {"mode": "user"},
            "security": {"no_guest_agent": False},
        }
        # desktop-minimal skips password prompt; others need it mocked
        if template == "desktop-minimal":
            create_instance(template, name=name, overrides=overrides)
        else:
            with patch("latita.operations.hash_password_interactive", return_value="$6$testhash"):
                create_instance(template, name=name, overrides=overrides)

    def _get_template_packages(self, template_name: str) -> list[str]:
        from latita.config import load_latita_template
        data = load_latita_template(template_name)
        return list(data.get("provision", {}).get("packages", []))

    def _build_verify_command(self, template_name: str, pkg_manager: str, packages: list[str]) -> str:
        """Build a shell command that verifies all packages are installed.

        Returns a command that fails with a clear error message if any package is missing.
        """
        if not packages:
            return "true"

        pkg_list = " ".join(shlex.quote(p) for p in packages)

        if pkg_manager.startswith("rpm"):
            # rpm -q exits non-zero and prints missing packages to stderr
            return f"rpm -q {pkg_list}"
        elif pkg_manager.startswith("dpkg"):
            # dpkg-query exits 0 even for missing, so we check individually
            checks = "\n".join(
                f"dpkg-query -W -f='${{Status}}' {shlex.quote(p)} | grep -q 'install ok installed' || {{ echo 'Missing: {p}' >&2; exit 1; }}"
                for p in packages
            )
            return f"bash -c '{checks}'"
        else:
            return f"{pkg_manager} {pkg_list}"

    @pytest.mark.parametrize("template,os_family,pkg_manager", TEMPLATES)
    def test_all_template_packages_installed(self, fedora_cfg, template, os_family, pkg_manager):
        """Create VM from template, verify every package in provision.packages is installed."""
        cfg = fedora_cfg
        name = f"test-f43-{template}"
        self._create_vm_from_template(cfg, name, template)

        try:
            start_instance(name)
            _wait_for_ssh_ready(name, timeout=300)
            packages = self._get_template_packages(template)
            assert packages, f"Template '{template}' has no packages to verify"
            verify_cmd = self._build_verify_command(template, pkg_manager, packages)
            start = time.time()
            while time.time() - start < 600:
                cp = _ssh_run(name, verify_cmd, timeout=60)
                if cp.returncode == 0:
                    break
                time.sleep(10)
            assert cp.returncode == 0, f"Not all packages installed for '{template}': stdout={cp.stdout!r} stderr={cp.stderr!r}"
        finally:
            _safe_destroy(name)


@pytest.mark.slow
class TestVmInitState:
    """Verify get_vm_init_state tracks headless and desktop VMs correctly
    from initializing through to ready."""

    def _create_headless_vm(self, cfg: Config, name: str):
        overrides = {
            "base_image": cfg.default_base_name,
            "ephemeral": {"transient": False, "destroy_on_stop": False},
            "memory": 2048,
            "cpus": 2,
            "disk_size": "10G",
            "network": {"mode": "user"},
            "security": {"no_guest_agent": False},
        }
        create_instance("headless", name=name, overrides=overrides)

    def _create_desktop_minimal_vm(self, cfg: Config, name: str):
        overrides = {
            "base_image": cfg.default_base_name,
            "ephemeral": {"transient": False, "destroy_on_stop": False},
            "memory": 2048,
            "cpus": 2,
            "disk_size": "10G",
            "network": {"mode": "user"},
            "security": {"no_guest_agent": True},
        }
        create_instance("desktop-minimal", name=name, overrides=overrides, wait=False)

    def test_headless_reaches_ready(self, fedora_cfg):
        """Headless VM: init state transitions to ready after cloud-init finishes."""
        cfg = fedora_cfg
        name = "test-f43-init-headless"
        self._create_headless_vm(cfg, name)
        try:
            start_instance(name)
            _wait_for_ssh_ready(name, timeout=300)
            # Log intermediate state (timing-dependent, do not assert)
            print(f"[init-state] pre-wait: {get_vm_init_state(name)}")
            ok = wait_for_vm_ready(name, max_wait=600)
            assert ok, "wait_for_vm_ready returned False (timeout or error)"
            state = get_vm_init_state(name)
            print(f"[init-state] post-wait: {state}")
            assert state["overall"] == "ready", f"Expected ready, got {state}"
            assert state["cloud_init"] == "done", f"Expected cloud-init done, got {state['cloud_init']}"
            assert state["desktop"] == "n/a", f"Expected desktop n/a for headless, got {state['desktop']}"
        finally:
            _safe_destroy(name)

    @pytest.mark.slow
    def test_desktop_minimal_tracks_init_during_boot(self, fedora_cfg):
        """desktop-minimal VM: init state reports 'initializing' during boot.

        Verifies the desktop probe fires and the state machine tracks correctly
        without waiting for the full installation (which can exceed 60 min).
        """
        cfg = fedora_cfg
        name = "test-f43-init-desktop"
        self._create_desktop_minimal_vm(cfg, name)
        try:
            start_instance(name, wait=False)
            _wait_for_ssh_ready(name, timeout=600)
            state = get_vm_init_state(name)
            print(f"[init-state] during-boot: {state}")
            # During boot, desktop-minimal is installing packages.
            # Accept either 'initializing' (still working) or 'ready' (fast machine).
            overall = state.get("overall")
            assert overall in ("initializing", "ready"), f"Unexpected state: {state}"
            if overall == "initializing":
                assert state.get("cloud_init") == "running", f"Expected cloud-init running: {state}"
                assert state.get("desktop") == "starting", f"Expected desktop starting: {state}"
        finally:
            _safe_destroy(name)


@pytest.mark.very_slow
class TestDesktopMinimalBoot:
    """Verify desktop-minimal template boots, cloud-init finishes, and the
    Openbox desktop environment is ready."""

    def _create_desktop_minimal_vm(self, cfg: Config, name: str):
        overrides = {
            "base_image": cfg.default_base_name,
            "ephemeral": {"transient": False, "destroy_on_stop": False},
            "memory": 2048,
            "cpus": 2,
            "disk_size": "10G",
            "network": {"mode": "user"},
            "security": {"no_guest_agent": True},
        }
        create_instance("desktop-minimal", name=name, overrides=overrides, wait=False)

    def test_cloud_init_finishes_and_desktop_ready(self, fedora_cfg):
        """desktop-minimal VM: cloud-init completes, openbox running,
        Xorg running, getty@tty1 active."""
        cfg = fedora_cfg
        name = "test-f43-desktop-minimal"
        self._create_desktop_minimal_vm(cfg, name)
        try:
            start_instance(name, wait=False)
            _wait_for_ssh_ready(name, timeout=600)
            start = time.time()
            while time.time() - start < 1800:
                cp = _ssh_run(name, "sudo cloud-init status 2>/dev/null")
                if cp.returncode in (0, 1, 2) and "status: done" in cp.stdout.lower():
                    break
                if "status: error" in cp.stdout.lower():
                    pytest.fail(f"cloud-init failed: {cp.stdout} {cp.stderr}")
                time.sleep(10)
            else:
                pytest.fail("cloud-init did not finish within 1800s")
            cp = _ssh_run(name, "ps aux | grep openbox | grep -v grep")
            assert cp.returncode == 0, f"openbox not running: {cp.stderr}"
            cp = _ssh_run(name, "ps aux | grep Xorg | grep -v grep")
            assert cp.returncode == 0, f"Xorg not running: {cp.stderr}"
            cp = _ssh_run(name, "systemctl is-active getty@tty1")
            assert cp.returncode == 0, f"getty@tty1 not active: {cp.stderr}"
        finally:
            _safe_destroy(name)

    def test_wait_command_blocks_until_ready(self, fedora_cfg):
        """wait_for_vm_ready returns True once desktop-minimal is fully up."""
        cfg = fedora_cfg
        name = "test-f43-desktop-minimal-wait"
        self._create_desktop_minimal_vm(cfg, name)
        try:
            start_instance(name, wait=False)
            ok = wait_for_vm_ready(name, max_wait=3600)
            assert ok, "wait_for_vm_ready returned False (timeout or error)"
            state = get_vm_init_state(name)
            assert state["overall"] == "ready", f"Expected ready, got {state}"
            assert state["cloud_init"] == "done", f"Expected cloud-init done, got {state['cloud_init']}"
            assert state["desktop"] == "ready", f"Expected desktop ready, got {state['desktop']}"
        finally:
            _safe_destroy(name)


@pytest.mark.system
@pytest.mark.very_slow
class TestMvpE2E:
    """MVP end-to-end: desktop-minimal VM reaches code-server on a headless VM.

    Requires qemu:///system and the default libvirt NAT network so both VMs
    share a routable subnet (192.168.122.0/24).
    """

    @staticmethod
    def _system_mode_available() -> bool:
        cp = subprocess.run(
            ["virsh", "-c", "qemu:///system", "uri"],
            capture_output=True,
            text=True,
        )
        return cp.returncode == 0 and "qemu:///system" in cp.stdout

    def _setup_system_config(self) -> Config:
        tmp = Path(tempfile.mkdtemp(prefix="latita-e2e-"))
        cfg = Config(
            root_dir=tmp,
            libvirt_uri="qemu:///system",
            default_base_url="",
            default_base_name="fedora43-base.qcow2",
            net_name="default",
        )
        set_config(cfg)
        cfg.ensure_dirs()
        # Link base image if it exists in the user's vault
        base_img = cfg.base_dir / "fedora43-base.qcow2"
        if not base_img.exists():
            user_base = Path.home() / "latita-vault" / "vm" / "base" / "fedora43-base.qcow2"
            if user_base.exists():
                base_img.symlink_to(user_base)
        return cfg

    def _ensure_default_network(self, cfg: Config) -> None:
        from latita.libvirt import (
            network_exists,
            network_is_active,
            define_network,
            start_network,
            create_network_xml,
        )
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
        if not network_is_active("default"):
            start_network("default")

    def _wait_for_nat_ip(self, name: str, timeout: int = 180) -> str:
        """Poll libvirt for a 192.168.122.x DHCP lease."""
        start = time.time()
        while time.time() - start < timeout:
            for addr in get_vm_ip_addresses(name):
                ip = addr.get("ip", "")
                if ip.startswith("192.168.122."):
                    return ip
            time.sleep(5)
        raise TimeoutError(f"No NAT IP found for {name} after {timeout}s")

    def test_desktop_minimal_reaches_code_server(self):
        if not self._system_mode_available():
            pytest.skip("qemu:///system not available (requires libvirtd + sudo)")

        cfg = self._setup_system_config()
        self._ensure_default_network(cfg)

        # Verify base image
        if not (cfg.base_dir / "fedora43-base.qcow2").exists():
            reset_config()
            shutil.rmtree(cfg.root_dir, ignore_errors=True)
            pytest.skip("Fedora 43 base image not available")

        overrides = {
            "base_image": "fedora43-base.qcow2",
            "ephemeral": {"transient": False, "destroy_on_stop": False},
            "memory": 2048,
            "cpus": 2,
            "disk_size": "10G",
            "network": {"mode": "nat", "nat_network": "default"},
            "security": {"no_guest_agent": False},
        }

        headless = "test-e2e-headless"
        desktop = "test-e2e-desktop"

        try:
            # 1. Create headless VM with code-server capsule
            create_instance(
                "headless",
                name=headless,
                overrides=overrides,
                capsule_names=["code-server"],
            )
            start_instance(headless)

            # 2. Discover headless IP on the shared NAT
            headless_ip = self._wait_for_nat_ip(headless, timeout=180)

            # 3. Create desktop-minimal VM
            create_instance("desktop-minimal", name=desktop, overrides=overrides)
            start_instance(desktop)

            # 4. Wait for desktop SSH
            _wait_for_ssh_ready(desktop, timeout=300)

            # 5. From desktop, curl code-server on headless
            #    (container image pull + startup can take a few minutes)
            curl_cmd = (
                f"curl -s -o /dev/null -w '%{{http_code}}' "
                f"--connect-timeout 10 --max-time 30 "
                f"http://{headless_ip}:8443/login"
            )
            code = None
            start = time.time()
            while time.time() - start < 300:
                cp = _ssh_run(desktop, curl_cmd, timeout=60)
                if cp.returncode == 0:
                    code = cp.stdout.strip()
                    if code in ("200", "301", "302"):
                        break
                time.sleep(10)
            assert code in ("200", "301", "302"), (
                f"Code-server not reachable from desktop-minimal VM: "
                f"code={code!r} stdout={cp.stdout!r} stderr={cp.stderr!r}"
            )

            # 6. Verify openbox is running on desktop-minimal
            cp = _ssh_run(desktop, "ps aux | grep openbox | grep -v grep", timeout=30)
            assert cp.returncode == 0, f"openbox not running on desktop: {cp.stderr}"

        finally:
            for vm in (headless, desktop):
                try:
                    destroy_instance(vm)
                except Exception:
                    pass
            reset_config()
            shutil.rmtree(cfg.root_dir, ignore_errors=True)
