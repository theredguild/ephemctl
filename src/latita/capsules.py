from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import typer

from .config import list_capsules, load_capsule
from .ui import console


def check_capsule_compatibility(
    capsule: dict[str, Any],
    *,
    profile: str = "",
    os_family: str = "",
) -> tuple[bool, str]:
    meta = capsule.get("compatibility") or {}
    profiles = meta.get("profiles")
    if profiles is not None and profile and profile not in profiles:
        return False, f"profile '{profile}' not in {profiles}"
    os_families = meta.get("os_family")
    if os_families is not None and os_family and os_family not in os_families:
        return False, f"os_family '{os_family}' not in {os_families}"
    return True, ""


def merge_provision_fragments(
    base: dict[str, Any], *fragments: dict[str, Any]
) -> dict[str, Any]:
    """Merge provision dicts (packages, write_files, root_commands, user_commands)."""
    merged = {
        "packages": list(base.get("packages", [])),
        "write_files": list(base.get("write_files", [])),
        "root_commands": list(base.get("root_commands", [])),
        "user_commands": list(base.get("user_commands", [])),
    }
    seen_packages: set[str] = set(merged["packages"])
    seen_files: set[str] = set()
    for wf in merged["write_files"]:
        if isinstance(wf, dict):
            seen_files.add(str(wf.get("path", "")))

    for fragment in fragments:
        for pkg in fragment.get("packages", []):
            if isinstance(pkg, str) and pkg not in seen_packages:
                seen_packages.add(pkg)
                merged["packages"].append(pkg)
        for wf in fragment.get("write_files", []):
            if isinstance(wf, dict):
                path = str(wf.get("path", ""))
                if path and path not in seen_files:
                    seen_files.add(path)
                    merged["write_files"].append(dict(wf))
        for cmd in fragment.get("root_commands", []):
            if isinstance(cmd, str) and cmd not in merged["root_commands"]:
                merged["root_commands"].append(cmd)
        for cmd in fragment.get("user_commands", []):
            if isinstance(cmd, str) and cmd not in merged["user_commands"]:
                merged["user_commands"].append(cmd)

    return merged


def resolve_capsules(
    capsule_names: list[str],
    *,
    profile: str = "",
    os_family: str = "",
) -> list[tuple[str, dict[str, Any]]]:
    """Resolve capsule names recursively, including dependencies.

    Uses depth-first traversal of `depends_on` lists. Dependencies are
    prepended before the requesting capsule so provisioning order is correct.

    Returns a list of (name, data) tuples preserving dependency order.
    """
    resolved_map: dict[str, dict[str, Any]] = {}

    def _resolve(name: str, stack: list[str]) -> None:
        if name in resolved_map:
            return
        if name in stack:
            raise typer.BadParameter(
                f"capsule dependency cycle detected: {' -> '.join(stack + [name])}"
            )
        capsule = load_capsule(name)
        ok, reason = check_capsule_compatibility(
            capsule, profile=profile, os_family=os_family
        )
        if not ok:
            raise typer.BadParameter(f"capsule '{name}' incompatible: {reason}")
        # Recurse into dependencies first (depth-first)
        deps = capsule.get("depends_on", [])
        if isinstance(deps, str):
            deps = [deps]
        for dep in deps:
            _resolve(dep, stack + [name])
        resolved_map[name] = capsule

    for name in capsule_names:
        _resolve(name, [])

    return [(name, resolved_map[name]) for name in resolved_map]


def capsule_provision_fragment(capsule: dict[str, Any]) -> dict[str, Any]:
    return capsule.get("provision", {})


def capsule_live_commands(capsule: dict[str, Any]) -> list[str]:
    live = capsule.get("live", {})
    cmds = live.get("commands", [])
    return [str(c) for c in cmds if isinstance(c, str) and c.strip()]


def capsule_live_user(capsule: dict[str, Any], default: str = "dev") -> str:
    live = capsule.get("live", {})
    return str(live.get("user", default))


def capsule_verify_command(capsule: dict[str, Any]) -> str | None:
    """Return the verify command string, or None if none defined."""
    verify = capsule.get("verify")
    if isinstance(verify, str) and verify.strip():
        return verify.strip()
    return None


class _SafeFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _format_value(value: Any, context: dict[str, str]) -> Any:
    if isinstance(value, str):
        return value.format_map(_SafeFormatDict(context))
    if isinstance(value, list):
        return [_format_value(item, context) for item in value]
    if isinstance(value, dict):
        return {str(key): _format_value(item, context) for key, item in value.items()}
    return value


def format_live_commands(capsule: dict[str, Any], guest_user: str) -> list[str]:
    """Return live commands with {guest_user}, {home_dir}, etc. substituted."""
    home_dir = f"/home/{guest_user}"
    context = {
        "guest_user": guest_user,
        "home_dir": home_dir,
        "workspace_dir": f"{home_dir}/workspace",
    }
    raw = capsule_live_commands(capsule)
    return [_format_value(cmd, context) for cmd in raw]


def format_verify_command(capsule: dict[str, Any], guest_user: str) -> str | None:
    """Return verify command with {guest_user}, {home_dir}, etc. substituted."""
    cmd = capsule_verify_command(capsule)
    if not cmd:
        return None
    home_dir = f"/home/{guest_user}"
    context = {
        "guest_user": guest_user,
        "home_dir": home_dir,
        "workspace_dir": f"{home_dir}/workspace",
    }
    return _format_value(cmd, context)


def list_compatible_capsules(
    profile: str = "", os_family: str = ""
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name, data in list_capsules().items():
        ok, _ = check_capsule_compatibility(data, profile=profile, os_family=os_family)
        if ok:
            result[name] = data
    return result


def format_capsule_table(capsules: dict[str, dict[str, Any]]) -> None:
    from rich.table import Table

    table = Table(title="Capsules")
    table.add_column("Name")
    table.add_column("Description")
    table.add_column("Compatible Profiles")
    for name, data in capsules.items():
        desc = str(data.get("description", "")).strip()
        meta = data.get("compatibility") or {}
        profiles = ", ".join(meta.get("profiles", [])) or "all"
        table.add_row(name, desc, profiles)
    console.print(table)
