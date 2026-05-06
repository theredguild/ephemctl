from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from .config import get_config


def instance_dir(name: str, cfg: Config | None = None) -> Path:
    cfg = cfg or get_config()
    return cfg.inst_dir / name


def overlay_path(name: str, cfg: Config | None = None) -> Path:
    return instance_dir(name, cfg) / f"{name}.qcow2"


def recipe_path(name: str, cfg: Config | None = None) -> Path:
    return instance_dir(name, cfg) / "recipe.json"


def spec_path(name: str, cfg: Config | None = None) -> Path:
    return instance_dir(name, cfg) / "spec.json"


def env_path(name: str, cfg: Config | None = None) -> Path:
    return instance_dir(name, cfg) / "instance.env"


def error_log_path(name: str, cfg: Config | None = None) -> Path:
    return instance_dir(name, cfg) / "last-errors.log"


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_instance_env(name: str, data: dict[str, Any], cfg: Config | None = None) -> None:
    lines: list[str] = []
    for k, v in data.items():
        if v is None:
            continue
        lines.append(f"{k}={shlex.quote(str(v))}")
    p = env_path(name, cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n")
    p.chmod(0o600)


def read_instance_env(name: str, cfg: Config | None = None) -> dict[str, str]:
    env: dict[str, str] = {}
    p = env_path(name, cfg)
    if not p.exists():
        return env
    for line in p.read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k] = shlex.split(v)[0] if v.strip() else ""
    return env


def write_instance_recipe(name: str, data: dict[str, Any], cfg: Config | None = None) -> None:
    write_json(recipe_path(name, cfg), data)


def read_instance_recipe(name: str, cfg: Config | None = None) -> dict[str, Any]:
    return read_json(recipe_path(name, cfg))


def write_instance_spec(name: str, data: dict[str, Any], cfg: Config | None = None) -> None:
    write_json(spec_path(name, cfg), data)


def read_instance_spec(name: str, cfg: Config | None = None) -> dict[str, Any]:
    return read_json(spec_path(name, cfg))


def increment_run_count(name: str, cfg: Config | None = None) -> int:
    spec = read_instance_spec(name, cfg)
    count = int(spec.get("run_count", 0)) + 1
    spec["run_count"] = count
    write_instance_spec(name, spec, cfg)
    return count


def get_run_count(name: str, cfg: Config | None = None) -> int:
    return int(read_instance_spec(name, cfg).get("run_count", 0))


def read_applied_capsules(name: str, cfg: Config | None = None) -> list[str]:
    return list(read_instance_spec(name, cfg).get("applied_capsules", []))


def append_applied_capsule(name: str, capsule_name: str, cfg: Config | None = None) -> None:
    spec = read_instance_spec(name, cfg)
    caps = list(spec.get("applied_capsules", []))
    if capsule_name not in caps:
        caps.append(capsule_name)
        spec["applied_capsules"] = caps
        write_instance_spec(name, spec, cfg)


from .config import Config
