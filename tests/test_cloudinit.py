from __future__ import annotations

import yaml

from latita.cloudinit import (
    _base_provision,
    _format_value,
    _package_install_block,
    _user_definition,
    build_network_config,
    build_user_data,
)


class TestPackageInstallBlock:
    def test_dnf_block(self):
        lines = _package_install_block(["git", "vim"], "dnf")
        assert any("dnf" in line for line in lines)
        assert any("git" in line for line in lines)

    def test_apt_block(self):
        lines = _package_install_block(["git", "vim"], "apt")
        assert any("apt-get" in line for line in lines)
        assert any("DEBIAN_FRONTEND" in line for line in lines)

    def test_apk_block(self):
        lines = _package_install_block(["git", "vim"], "apk")
        assert any("apk add" in line for line in lines)

    def test_empty_packages(self):
        lines = _package_install_block([], "dnf")
        assert lines == []


class TestUserDefinition:
    def test_headless_passwordless(self):
        ctx = {"guest_user": "dev", "host_pubkey": "ssh-ed25519 AAA", "lab_pubkey": "ssh-ed25519 BBB", "login_hash": ""}
        user = _user_definition("headless", ctx, passwordless_sudo=True)
        assert user["name"] == "dev"
        assert user["sudo"] == "ALL=(ALL) NOPASSWD:ALL"
        assert len(user["ssh_authorized_keys"]) == 2
        assert "passwd" not in user

    def test_headless_with_password(self):
        ctx = {"guest_user": "dev", "host_pubkey": "ssh-ed25519 AAA", "lab_pubkey": "ssh-ed25519 BBB", "login_hash": "$6$hash"}
        user = _user_definition("headless", ctx, passwordless_sudo=False)
        assert user["sudo"] == "ALL=(ALL) ALL"
        assert user["passwd"] == "$6$hash"
        assert user["lock_passwd"] is False

    def test_desktop_without_password(self):
        ctx = {"guest_user": "dev", "host_pubkey": "ssh-ed25519 AAA", "lab_pubkey": "", "login_hash": ""}
        user = _user_definition("desktop", ctx)
        # Empty login_hash should NOT set passwd for desktop
        assert "passwd" not in user
        assert "lock_passwd" not in user

    def test_desktop_with_password(self):
        ctx = {"guest_user": "dev", "host_pubkey": "ssh-ed25519 AAA", "lab_pubkey": "", "login_hash": "$6$hash"}
        user = _user_definition("desktop", ctx)
        assert user["passwd"] == "$6$hash"
        assert user["lock_passwd"] is False


class TestBuildUserData:
    def test_generates_valid_yaml(self):
        ud = build_user_data(
            profile="headless",
            guest_user="dev",
            host_pubkey="ssh-ed25519 AAA",
            lab_pubkey="ssh-ed25519 BBB",
            package_manager="dnf",
        )
        assert ud.startswith("#cloud-config")
        data = yaml.safe_load(ud)
        assert data["ssh_pwauth"] is False
        assert len(data["users"]) == 1
        assert data["runcmd"]

    def test_apt_package_manager(self):
        ud = build_user_data(
            profile="headless",
            guest_user="dev",
            host_pubkey="ssh-ed25519 AAA",
            package_manager="apt",
            provision={"packages": ["curl"], "write_files": [], "root_commands": [], "user_commands": []},
        )
        assert "apt-get" in ud

    def test_capsule_provisions_merged(self):
        ud = build_user_data(
            profile="headless",
            guest_user="dev",
            host_pubkey="ssh-ed25519 AAA",
            capsule_provisions=[
                {"packages": ["vim"], "write_files": [], "root_commands": [], "user_commands": []}
            ],
        )
        data = yaml.safe_load(ud)
        write_files = data["write_files"]
        bootstrap = [f for f in write_files if f["path"].endswith("bootstrap-headless.sh")]
        assert bootstrap


class TestBuildNetworkConfig:
    def test_basic_structure(self):
        nc = build_network_config("52:54:00:00:00:01", "52:54:00:00:00:02", "10.31.0.10")
        data = yaml.safe_load(nc)
        assert data["version"] == 2
        assert data["ethernets"]["wan0"]["dhcp4"] is True
        assert data["ethernets"]["mgmt0"]["addresses"] == ["10.31.0.10/24"]

    def test_custom_prefix(self):
        nc = build_network_config("52:54:00:00:00:01", "52:54:00:00:00:02", "10.31.0.10", "16")
        data = yaml.safe_load(nc)
        assert data["ethernets"]["mgmt0"]["addresses"] == ["10.31.0.10/16"]


class TestBaseProvisionOsFamily:
    def test_fedora_sshd(self):
        ctx = {"guest_user": "dev", "home_dir": "/home/dev", "workspace_dir": "/home/dev/workspace"}
        prov = _base_provision("headless", ctx, os_family="fedora")
        assert any("sshd" in cmd for cmd in prov["root_commands"])

    def test_ubuntu_ssh(self):
        ctx = {"guest_user": "dev", "home_dir": "/home/dev", "workspace_dir": "/home/dev/workspace"}
        prov = _base_provision("headless", ctx, os_family="ubuntu")
        assert any("ssh" in cmd and "sshd" not in cmd for cmd in prov["root_commands"])
        assert not any("sshd" in cmd for cmd in prov["root_commands"])

    def test_fedora_no_restorecon_in_base(self):
        ctx = {"guest_user": "dev", "home_dir": "/home/dev", "workspace_dir": "/home/dev/workspace"}
        prov = _base_provision("desktop", ctx, os_family="fedora")
        assert not any("restorecon" in cmd for cmd in prov["root_commands"])

    def test_ubuntu_no_restorecon_in_base(self):
        ctx = {"guest_user": "dev", "home_dir": "/home/dev", "workspace_dir": "/home/dev/workspace"}
        prov = _base_provision("desktop", ctx, os_family="ubuntu")
        assert not any("restorecon" in cmd for cmd in prov["root_commands"])


class TestUserDefinitionOsFamily:
    def test_fedora_wheel(self):
        ctx = {"guest_user": "dev", "host_pubkey": "ssh-ed25519 AAA", "lab_pubkey": "", "login_hash": ""}
        user = _user_definition("headless", ctx, os_family="fedora")
        assert "wheel" in user["groups"]
        assert "sudo" not in user["groups"]

    def test_ubuntu_sudo(self):
        ctx = {"guest_user": "dev", "host_pubkey": "ssh-ed25519 AAA", "lab_pubkey": "", "login_hash": ""}
        user = _user_definition("headless", ctx, os_family="ubuntu")
        assert "sudo" in user["groups"]
        assert "wheel" not in user["groups"]


class TestFormatValue:
    def test_simple_substitution(self):
        ctx = {"guest_user": "dev"}
        assert _format_value("hello {guest_user}", ctx) == "hello dev"

    def test_bash_parameter_expansion(self):
        ctx = {"guest_user": "dev"}
        result = _format_value("${HOME:-/tmp}", ctx)
        assert result == "${HOME:-/tmp}"

    def test_mixed_template_and_bash(self):
        ctx = {"home_dir": "/home/dev"}
        result = _format_value("{home_dir}/${SOME_VAR:-fallback}", ctx)
        assert result == "/home/dev/${SOME_VAR:-fallback}"

    def test_unmatched_braces_passthrough(self):
        ctx = {"guest_user": "dev"}
        result = _format_value("no-match {unknown_key}", ctx)
        assert result == "no-match {unknown_key}"

    def test_nested_dict(self):
        ctx = {"guest_user": "dev"}
        result = _format_value({"path": "/home/{guest_user}/.bashrc"}, ctx)
        assert result == {"path": "/home/dev/.bashrc"}


class TestBuildUserDataUbuntu:
    def test_ubuntu_cloud_init_valid(self):
        ud = build_user_data(
            profile="desktop",
            guest_user="dev",
            host_pubkey="ssh-ed25519 AAA host",
            provision={
                "packages": ["openssh-client"],
                "root_commands": [
                    "systemctl enable --now ssh",
                    "systemctl enable lightdm",
                ],
                "write_files": [],
                "user_commands": [],
            },
            package_manager="apt",
            os_family="ubuntu",
            login_hash="$6$salt$hash",
        )
        data = yaml.safe_load(ud)
        user = data["users"][0]
        assert "sudo" in user["groups"]
        assert "wheel" not in user["groups"]
        bootstrap_content = ""
        for wf in data["write_files"]:
            if "bootstrap" in wf["path"]:
                bootstrap_content = wf["content"]
        assert "systemctl enable --now ssh" in bootstrap_content
        assert "sshd" not in bootstrap_content
        assert "apt-get" in bootstrap_content
        assert "restorecon" not in bootstrap_content

    def test_fedora_cloud_init_valid(self):
        ud = build_user_data(
            profile="desktop",
            guest_user="dev",
            host_pubkey="ssh-ed25519 AAA host",
            provision={
                "packages": ["spice-vdagent"],
                "root_commands": [
                    "systemctl enable --now sshd",
                    "systemctl set-default graphical.target",
                    "restorecon -RF /home/dev || true",
                ],
                "write_files": [],
                "user_commands": [],
            },
            package_manager="dnf",
            os_family="fedora",
        )
        data = yaml.safe_load(ud)
        user = data["users"][0]
        assert "wheel" in user["groups"]
        assert "sudo" not in user["groups"]
        bootstrap_content = ""
        for wf in data["write_files"]:
            if "bootstrap" in wf["path"]:
                bootstrap_content = wf["content"]
        assert "systemctl enable --now sshd" in bootstrap_content
        assert "restorecon" in bootstrap_content
        assert "apt-get" not in bootstrap_content

    def test_ubuntu_no_duplicate_commands(self):
        ud = build_user_data(
            profile="desktop",
            guest_user="dev",
            host_pubkey="ssh-ed25519 AAA host",
            provision={
                "packages": [],
                "root_commands": [
                    "systemctl enable --now ssh",
                    "mkdir -p /home/{guest_user}/Downloads",
                    "chown -R {guest_user}:{guest_user} /home/{guest_user}",
                ],
                "write_files": [],
                "user_commands": [],
            },
            package_manager="apt",
            os_family="ubuntu",
        )
        bootstrap_content = ""
        for wf in yaml.safe_load(ud)["write_files"]:
            if "bootstrap" in wf["path"]:
                bootstrap_content = wf["content"]
        assert bootstrap_content.count("mkdir -p /home/dev/Downloads") == 1
        assert bootstrap_content.count("chown -R dev:dev /home/dev") == 1


class TestBuiltinTemplatesCloudInit:
    """Generate cloud-init from every built-in template and validate OS-specific behavior."""

    @staticmethod
    def _build_from_template(template_name: str) -> tuple[dict, str]:
        from latita.operations import build_recipe
        from latita.cloudinit import build_user_data

        recipe = build_recipe(template_name)
        provision = recipe.get("provision", {})
        os_family = recipe.get("os_family", "fedora")
        pkg_mgr = "apt" if os_family in ("ubuntu", "debian") else "dnf"

        ud = build_user_data(
            profile=recipe["profile"],
            guest_user=recipe["guest_user"],
            host_pubkey="ssh-ed25519 AAAAtest host",
            lab_pubkey="ssh-ed25519 AAAAtest lab",
            login_hash="$6$test$hash" if recipe["profile"] == "desktop" and template_name != "desktop-minimal" else "",
            provision=provision,
            passwordless_sudo=recipe.get("passwordless_sudo", True),
            package_manager=pkg_mgr,
            os_family=os_family,
        )
        return recipe, ud

    def _bootstrap_content(self, ud: str) -> str:
        data = yaml.safe_load(ud)
        for wf in data["write_files"]:
            if "bootstrap" in wf["path"]:
                return wf["content"]
        return ""

    def test_headless_fedora(self):
        recipe, ud = self._build_from_template("headless")
        data = yaml.safe_load(ud)
        user = data["users"][0]
        assert "wheel" in user["groups"]
        bs = self._bootstrap_content(ud)
        assert "sshd" in bs
        assert "ssh" not in bs.split("sshd")[0][-20:]
        assert "dnf" in bs
        assert "restorecon" in bs

    def test_desktop_minimal_fedora(self):
        recipe, ud = self._build_from_template("desktop-minimal")
        data = yaml.safe_load(ud)
        user = data["users"][0]
        assert "wheel" in user["groups"]
        bs = self._bootstrap_content(ud)
        assert "sshd" in bs
        assert "dnf" in bs
        assert "restorecon" in bs
        assert "xorg-x11-xinit" in bs
        assert "openbox" in bs

    def test_desktop_fedora(self):
        recipe, ud = self._build_from_template("desktop")
        data = yaml.safe_load(ud)
        user = data["users"][0]
        assert "wheel" in user["groups"]
        assert user.get("passwd")
        bs = self._bootstrap_content(ud)
        assert "sshd" in bs
        assert "dnf" in bs
        assert "lightdm" in bs
