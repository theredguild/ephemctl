from __future__ import annotations

from pathlib import Path

import pytest

from latita.config import (
    Config,
    get_config,
    list_capsules,
    list_latita_templates,
    load_capsule,
    load_latita_template,
    load_yaml,
    reset_config,
    write_yaml,
)


class TestConfig:
    def test_default_root_is_home(self, monkeypatch):
        monkeypatch.delenv("LATITA_ROOT", raising=False)
        cfg = Config.default()
        assert cfg.root_dir == Path.home() / "latita-vault"

    def test_for_tests(self, tmp_path):
        cfg = Config.for_tests(tmp_path)
        assert cfg.root_dir == tmp_path
        assert cfg.libvirt_uri == "qemu:///session"

    def test_ensure_dirs_creates_structure(self, tmp_path):
        cfg = Config.for_tests(tmp_path)
        cfg.ensure_dirs()
        assert cfg.inst_dir.exists()
        assert cfg.base_dir.exists()
        assert cfg.keys_dir.exists()
        assert cfg.net_dir.exists()
        assert cfg.templates_dir.exists()
        assert cfg.capsules_dir.exists()
        assert cfg.root_marker_path.exists()

    def test_get_config_singleton(self):
        reset_config()
        cfg1 = get_config()
        cfg2 = get_config()
        assert cfg1 is cfg2


class TestYamlHelpers:
    def test_load_yaml_missing(self, tmp_path):
        assert load_yaml(tmp_path / "missing.yaml") == {}

    def test_write_and_load_yaml(self, tmp_path):
        path = tmp_path / "test.yaml"
        data = {"key": "value", "nested": {"a": 1}}
        write_yaml(path, data)
        assert load_yaml(path) == data
        assert path.stat().st_mode & 0o777 == 0o600


class TestTemplates:
    def test_builtin_templates_exist(self):
        templates = list_latita_templates()
        assert "headless" in templates
        assert "desktop" in templates

    def test_load_headless_template(self):
        data = load_latita_template("headless")
        assert data["profile"] == "headless"
        assert "provision" in data

    def test_load_desktop_template(self):
        data = load_latita_template("desktop")
        assert data["profile"] == "desktop"
        assert "provision" in data
        pkgs = data["provision"]["packages"]
        assert "lightdm-gtk" in pkgs
        assert "xorg-x11-drv-qxl" in pkgs
        assert "xorg-x11-drv-virtio" not in pkgs

    def test_load_desktop_minimal_template(self):
        data = load_latita_template("desktop-minimal")
        assert data["profile"] == "desktop"
        assert "openbox" in data["provision"]["packages"]
        assert "lxpanel" in data["provision"]["packages"]
        assert "xorg-x11-xinit" in data["provision"]["packages"]

    def test_user_template_override(self, isolated_config):
        cfg = isolated_config
        user_tpl = cfg.templates_dir / "custom.latita"
        write_yaml(user_tpl, {"profile": "headless", "cpus": 8})
        templates = list_latita_templates()
        assert templates["custom"]["cpus"] == 8


class TestCapsules:
    def test_builtin_capsules_exist(self):
        capsules = list_capsules()
        assert "code-server" in capsules
        assert "ai-agents" in capsules

    def test_load_capsule(self):
        cap = load_capsule("code-server")
        assert "provision" in cap
        assert "live" in cap

    def test_user_capsule_override(self, isolated_config):
        cfg = isolated_config
        user_cap = cfg.capsules_dir / "my.cap"
        write_yaml(user_cap, {"description": "test", "compatibility": {"profiles": ["headless"]}})
        capsules = list_capsules()
        assert capsules["my"]["description"] == "test"

    def test_capsule_not_found(self):
        with pytest.raises(Exception):
            load_capsule("nonexistent-capsule-xyz")


class TestProjectConfig:
    def test_load_missing_returns_empty(self, tmp_path):
        from latita.config import load_project_config, clear_project_config
        clear_project_config()
        cfg = load_project_config(tmp_path)
        assert cfg == {}

    def test_load_existing(self, tmp_path):
        from latita.config import load_project_config, clear_project_config
        clear_project_config()
        (tmp_path / ".latita").write_text("memory: 8192\ncpus: 4\n")
        cfg = load_project_config(tmp_path)
        assert cfg["memory"] == 8192
        assert cfg["cpus"] == 4
        clear_project_config()
