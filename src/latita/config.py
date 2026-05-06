from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
import typer

ROOT_MARKER_NAME = ".latita-root"
DEFAULT_ROOT_DIRNAME = "latita-vault"
ROOT_SEARCH_DIRNAMES = ("latita-vault", "vms")

# ---------------------------------------------------------------------------
# Root resolution
# ---------------------------------------------------------------------------

def resolve_root_dir(
    start: Path | None = None,
    *,
    env_root: str | None = None,
) -> Path:
    selected_env_root = (
        env_root if env_root is not None else os.environ.get("LATITA_ROOT")
    )
    if selected_env_root:
        return Path(selected_env_root).expanduser().resolve()

    current = (start or Path.cwd()).resolve()
    if current.name in ROOT_SEARCH_DIRNAMES:
        return current

    for parent in (current, *current.parents):
        if (parent / ROOT_MARKER_NAME).exists():
            return parent
        for dirname in ROOT_SEARCH_DIRNAMES:
            candidate = parent / dirname
            if (candidate / ROOT_MARKER_NAME).exists():
                return candidate

    # Default to home directory, NOT current working directory
    return Path.home() / DEFAULT_ROOT_DIRNAME


def _auto_libvirt_uri() -> str:
    if os.environ.get('LIBVIRT_DEFAULT_URI'):
        return os.environ['LIBVIRT_DEFAULT_URI']
    import subprocess
    try:
        cp = subprocess.run(
            ['virsh', '-c', 'qemu:///system', 'list'],
            capture_output=True,
            text=True,
            timeout=1,
        )
        if cp.returncode == 0:
            return 'qemu:///system'
    except (subprocess.TimeoutExpired, OSError):
        pass
    return 'qemu:///session'


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    """Immutable runtime configuration for latita."""

    root_dir: Path
    libvirt_uri: str
    default_base_url: str
    default_base_name: str
    net_name: str
    qemu_binary: str = ""

    @property
    def vm_dir(self) -> Path:
        return self.root_dir / "vm"

    @property
    def base_dir(self) -> Path:
        return self.vm_dir / "base"

    @property
    def inst_dir(self) -> Path:
        return self.vm_dir / "instances"

    @property
    def keys_dir(self) -> Path:
        return self.root_dir / "keys"

    @property
    def net_dir(self) -> Path:
        return self.root_dir / "networks"

    @property
    def recipes_dir(self) -> Path:
        return self.root_dir / "recipes"

    @property
    def templates_dir(self) -> Path:
        return self.root_dir / "templates"

    @property
    def capsules_dir(self) -> Path:
        return self.root_dir / "capsules"

    @property
    def root_marker_path(self) -> Path:
        return self.root_dir / ROOT_MARKER_NAME

    @property
    def is_session(self) -> bool:
        return self.libvirt_uri.endswith(":///session")

    def ensure_dirs(self) -> None:
        for p in (
            self.root_dir,
            self.vm_dir,
            self.base_dir,
            self.inst_dir,
            self.keys_dir,
            self.net_dir,
            self.recipes_dir,
            self.templates_dir,
            self.capsules_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)
        if not self.root_marker_path.exists():
            self.root_marker_path.touch()

    @classmethod
    def default(cls) -> Config:
        root = resolve_root_dir()
        root_cfg = load_root_config(root)
        uri = root_cfg.get("libvirt_uri")
        if not uri:
            uri = _auto_libvirt_uri()
        return cls(
            root_dir=root,
            libvirt_uri=uri,
            default_base_url="",
            default_base_name="fedora43-base.qcow2",
            net_name="mgmt-nogw",
        )

    @classmethod
    def for_tests(cls, tmp_path: Path) -> Config:
        """Create a config isolated to a temporary directory."""
        return cls(
            root_dir=tmp_path,
            libvirt_uri="qemu:///session",
            default_base_url="",
            default_base_name="test-base.qcow2",
            net_name="test-mgmt",
        )


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------

_CONFIG: Config | None = None


def get_config() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config.default()
    return _CONFIG


def set_config(config: Config) -> None:
    global _CONFIG
    _CONFIG = config


def reset_config() -> None:
    global _CONFIG
    _CONFIG = None


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------

def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text())
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Project-level .latita config (like a Smolfile)
# ---------------------------------------------------------------------------

_PROJECT_CONFIG: dict[str, Any] | None = None


def load_project_config(cwd: Path | None = None) -> dict[str, Any]:
    """Load a .latita config file from the current working directory."""
    global _PROJECT_CONFIG
    if _PROJECT_CONFIG is not None:
        return _PROJECT_CONFIG
    p = (cwd or Path.cwd()) / ".latita"
    if p.exists():
        _PROJECT_CONFIG = load_yaml(p)
    else:
        _PROJECT_CONFIG = {}
    return _PROJECT_CONFIG


def clear_project_config() -> None:
    global _PROJECT_CONFIG
    _PROJECT_CONFIG = None


_ROOT_CONFIG: dict[str, Any] | None = None


def load_root_config(root: Path | None = None) -> dict[str, Any]:
    """Load a .latita config file from the latita root directory."""
    global _ROOT_CONFIG
    if _ROOT_CONFIG is not None:
        return _ROOT_CONFIG
    r = root or resolve_root_dir()
    p = r / ".latita"
    if p.exists():
        _ROOT_CONFIG = load_yaml(p)
    else:
        _ROOT_CONFIG = {}
    return _ROOT_CONFIG


def clear_root_config() -> None:
    global _ROOT_CONFIG
    _ROOT_CONFIG = None


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=True))
    path.chmod(0o600)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def list_latita_templates(cfg: Config | None = None) -> dict[str, dict[str, Any]]:
    cfg = cfg or get_config()
    templates: dict[str, dict[str, Any]] = {}
    if cfg.templates_dir.exists() and cfg.templates_dir.is_dir():
        for p in sorted(cfg.templates_dir.glob("*.latita")):
            try:
                data = load_yaml(p)
                if isinstance(data, dict):
                    templates[p.stem] = data
            except Exception:
                continue
    # Built-in templates shipped with package
    pkg_templates = Path(__file__).with_name("builtin_templates")
    if pkg_templates.exists() and pkg_templates.is_dir():
        for p in sorted(pkg_templates.glob("*.latita")):
            try:
                data = load_yaml(p)
                if isinstance(data, dict) and p.stem not in templates:
                    templates[p.stem] = data
            except Exception:
                continue
    return templates


def load_latita_template(name: str, cfg: Config | None = None) -> dict[str, Any]:
    cfg = cfg or get_config()
    path = cfg.templates_dir / f"{name}.latita"
    data = load_yaml(path)
    if not data:
        pkg_path = Path(__file__).with_name("builtin_templates") / f"{name}.latita"
        data = load_yaml(pkg_path)
    if not data:
        raise typer.BadParameter(f"template not found: {name}")
    return data


def get_template_path(name: str, cfg: Config | None = None) -> Path:
    cfg = cfg or get_config()
    user_path = cfg.templates_dir / f"{name}.latita"
    if user_path.exists():
        return user_path
    pkg_path = Path(__file__).with_name("builtin_templates") / f"{name}.latita"
    if pkg_path.exists():
        return pkg_path
    raise typer.BadParameter(f"template not found: {name}")


def is_builtin_template(name: str, cfg: Config | None = None) -> bool:
    cfg = cfg or get_config()
    user_path = cfg.templates_dir / f"{name}.latita"
    pkg_path = Path(__file__).with_name("builtin_templates") / f"{name}.latita"
    return pkg_path.exists() and not user_path.exists()


# ---------------------------------------------------------------------------
# Capsules
# ---------------------------------------------------------------------------

def list_capsules(cfg: Config | None = None) -> dict[str, dict[str, Any]]:
    cfg = cfg or get_config()
    capsules: dict[str, dict[str, Any]] = {}
    if cfg.capsules_dir.exists() and cfg.capsules_dir.is_dir():
        for p in sorted(cfg.capsules_dir.glob("*.cap")):
            try:
                data = load_yaml(p)
                if isinstance(data, dict):
                    capsules[p.stem] = data
            except Exception:
                continue
    pkg_capsules = Path(__file__).with_name("builtin_capsules")
    if pkg_capsules.exists() and pkg_capsules.is_dir():
        for p in sorted(pkg_capsules.glob("*.cap")):
            try:
                data = load_yaml(p)
                if isinstance(data, dict) and p.stem not in capsules:
                    capsules[p.stem] = data
            except Exception:
                continue
    return capsules


def load_capsule(name: str, cfg: Config | None = None) -> dict[str, Any]:
    cfg = cfg or get_config()
    capsules = list_capsules(cfg)
    if name not in capsules:
        raise typer.BadParameter(f"capsule not found: {name}")
    return capsules[name]


def get_capsule_path(name: str, cfg: Config | None = None) -> Path:
    cfg = cfg or get_config()
    user_path = cfg.capsules_dir / f"{name}.cap"
    if user_path.exists():
        return user_path
    pkg_path = Path(__file__).with_name("builtin_capsules") / f"{name}.cap"
    if pkg_path.exists():
        return pkg_path
    raise typer.BadParameter(f"capsule not found: {name}")


def is_builtin_capsule(name: str, cfg: Config | None = None) -> bool:
    cfg = cfg or get_config()
    user_path = cfg.capsules_dir / f"{name}.cap"
    pkg_path = Path(__file__).with_name("builtin_capsules") / f"{name}.cap"
    return pkg_path.exists() and not user_path.exists()


# ---------------------------------------------------------------------------
# Base images catalog
# ---------------------------------------------------------------------------

BASE_IMAGES: dict[str, dict[str, Any]] = {
    "Fedora 43 Cloud": {
        "filename": "fedora43-base.qcow2",
        "url": "https://download.fedoraproject.org/pub/fedora/linux/releases/43/Cloud/x86_64/images/",
        "discover": True,
        "mirror_urls": [
            "https://mirrors.kernel.org/fedora/releases/43/Cloud/x86_64/images/",
        ],
    },
    "Fedora 42 Cloud": {
        "filename": "fedora42-base.qcow2",
        "url": "https://download.fedoraproject.org/pub/fedora/linux/releases/42/Cloud/x86_64/images/",
        "discover": True,
        "mirror_urls": [
            "https://mirrors.kernel.org/fedora/releases/42/Cloud/x86_64/images/",
        ],
    },
    "Ubuntu 24.04 LTS Cloud": {
        "filename": "ubuntu2404-base.qcow2",
        "url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
    },
}
