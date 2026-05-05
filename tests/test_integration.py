from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from latita.config import Config, get_config, set_config
from latita.libvirt import vm_exists
from latita.operations import (
    bootstrap_host,
    create_instance,
    destroy_instance,
    revive_instance,
    scan_instances,
    start_instance,
    stop_instance,
)
from latita.utils import create_lab_key


CIRROS_IMG = Path("/tmp/cirros.img")


@pytest.fixture(scope="session", autouse=True)
def _cleanup_lingering_test_vms():
    """Remove any test-* VMs left behind by interrupted previous runs.

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
                    if name.startswith("test-"):
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
def integration_cfg(tmp_path):
    """Provide an isolated session-mode config with a CirrOS base image."""
    cfg = Config.for_tests(tmp_path)
    set_config(cfg)
    cfg.ensure_dirs()

    # Seed a tiny base image
    base = cfg.base_dir / cfg.default_base_name
    if not CIRROS_IMG.exists():
        pytest.skip("CirrOS base image not available at /tmp/cirros.img")
    shutil.copy(CIRROS_IMG, base)

    # SSH keys
    create_lab_key("lab1")

    yield cfg

    # Cleanup: destroy any lingering test domains
    try:
        cp = subprocess.run(
            ["virsh", "-c", cfg.libvirt_uri, "list", "--all", "--name"],
            capture_output=True,
            text=True,
            check=False,
        )
        for name in cp.stdout.splitlines():
            name = name.strip()
            if name.startswith("test-"):
                subprocess.run(
                    ["virsh", "-c", cfg.libvirt_uri, "destroy", name],
                    capture_output=True,
                    check=False,
                )
                subprocess.run(
                    ["virsh", "-c", cfg.libvirt_uri, "undefine", name, "--remove-all-storage"],
                    capture_output=True,
                    check=False,
                )
    except Exception:
        pass

    # Cleanup instance directory
    inst = cfg.inst_dir
    if inst.exists():
        for d in inst.iterdir():
            if d.is_dir() and d.name.startswith("test-"):
                shutil.rmtree(d)

    # Reset global config
    from latita.config import reset_config

    reset_config()


class TestVmLifecycle:
    def _create_test_vm(self, cfg: Config, name: str, transient: bool = False):
        overrides = {
            "base_image": cfg.default_base_name,
            "ephemeral": {"transient": transient},
            "memory": 512,
            "cpus": 1,
            "disk_size": "1G",
            "network": {"mode": "user"},
        }
        create_instance("headless", name=name, overrides=overrides)

    def test_create_instance_defines_domain(self, integration_cfg):
        cfg = integration_cfg
        name = "test-create"
        self._create_test_vm(cfg, name)
        assert vm_exists(name)
        destroy_instance(name)
        assert not vm_exists(name)

    def test_start_stop_instance(self, integration_cfg):
        cfg = integration_cfg
        name = "test-startstop"
        self._create_test_vm(cfg, name)
        assert vm_exists(name)

        stop_instance(name)
        # After stop, domain still exists but is shut off
        assert vm_exists(name)

        start_instance(name)
        assert vm_exists(name)

        destroy_instance(name)
        assert not vm_exists(name)

    def test_destroy_instance_cleans_files(self, integration_cfg):
        cfg = integration_cfg
        name = "test-cleanup"
        self._create_test_vm(cfg, name)
        inst_dir = cfg.inst_dir / name
        assert inst_dir.exists()

        destroy_instance(name)
        assert not inst_dir.exists()
        assert not vm_exists(name)

    def test_revive_redefines_domain(self, integration_cfg):
        cfg = integration_cfg
        name = "test-revive"
        self._create_test_vm(cfg, name)
        assert vm_exists(name)

        # Simulate domain loss (undefine but keep files)
        subprocess.run(
            ["virsh", "-c", cfg.libvirt_uri, "destroy", name],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["virsh", "-c", cfg.libvirt_uri, "undefine", name],
            capture_output=True,
            check=False,
        )
        assert not vm_exists(name)
        assert (cfg.inst_dir / name).exists()

        revive_instance(name)
        assert vm_exists(name)

        destroy_instance(name)
        assert not vm_exists(name)

    def test_bootstrap_is_idempotent(self, integration_cfg):
        cfg = integration_cfg
        # bootstrap_host should not crash when run repeatedly in session mode
        bootstrap_host()
        bootstrap_host()
        assert cfg.root_dir.exists()

    def test_scan_instances_finds_vm(self, integration_cfg):
        cfg = integration_cfg
        name = "test-scan"
        self._create_test_vm(cfg, name)

        entries = scan_instances()
        names = [e["name"] for e in entries]
        assert name in names

        # Check fields are populated
        entry = next(e for e in entries if e["name"] == name)
        assert entry["profile"] == "headless"
        assert entry["status"] != ""

        destroy_instance(name)

    def test_transient_vm_is_removed_after_destroy(self, integration_cfg):
        cfg = integration_cfg
        name = "test-transient"
        self._create_test_vm(cfg, name, transient=True)

        # Transient domain might already be gone if it shut down,
        # but we can still try to destroy it cleanly.
        if vm_exists(name):
            destroy_instance(name)

        assert not vm_exists(name)
