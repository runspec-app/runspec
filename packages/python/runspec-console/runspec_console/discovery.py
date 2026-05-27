"""
discovery.py — discover runnables by scanning site-packages for runspec.toml files.

For local hosts: scan the venv's site-packages directory directly (no subprocess).
For remote hosts: SSH + `runspec local --format json` (runspec is already installed there).

Returns a list of Runnable dicts matching the bridge/index.ts Runnable interface.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from .hosts import scripts_dir, site_packages, venv_name

# Dirs inside site-packages that never contain user packages
_SKIP_DIRS = frozenset({
    "__pycache__", "bin", "lib", "lib64", "include", "share",
})


_SELF_EXCLUDE = frozenset({"runspec-console"})


def discover_local(runspec_path: str, host: str) -> list[dict[str, Any]]:
    """Shell out to the local runspec binary for discovery.

    Using 'runspec local --format json' rather than scanning site-packages
    directly ensures inference rules are applied (type, required) — giving the
    same arg shape as discover_remote(), which also goes through the CLI.
    """
    group = venv_name(runspec_path)
    try:
        result = subprocess.run(
            [runspec_path, "local", "--format", "json"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if result.returncode != 0:
        return []
    try:
        items = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    runnables: list[dict[str, Any]] = []
    for item in items:
        if item["runnable"] in _SELF_EXCLUDE:
            continue
        runnables.extend(_spec_to_runnables(item["runnable"], item["spec"], host, group))
    return runnables


def discover_remote(ssh_target: str, runspec_path: str, host: str, identity_file: str | None = None) -> list[dict[str, Any]]:
    """SSH + runspec local --format json to discover runnables on a remote host."""
    from .executor import ssh_flags
    group = venv_name(runspec_path)
    cmd = ["ssh", *ssh_flags(identity_file), ssh_target, runspec_path, "local", "--format", "json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if result.returncode != 0:
        return []
    try:
        items = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    runnables: list[dict[str, Any]] = []
    for item in items:
        runnables.extend(_spec_to_runnables(item["runnable"], item["spec"], host, group))
    return runnables


def _parse_toml(
    toml_path: Path, host: str, group: str, bin_dir: Path
) -> list[dict[str, Any]]:
    try:
        with open(toml_path, "rb") as f:
            raw = tomllib.load(f)
    except Exception:
        return []

    runnables: list[dict[str, Any]] = []
    for name, spec in raw.items():
        if name == "config" or not isinstance(spec, dict):
            continue
        # Only include runnables that have an installed entry point
        if not (bin_dir / name).exists() and not (bin_dir / f"{name}.exe").exists():
            continue
        runnables.extend(_spec_to_runnables(name, spec, host, group))
    return runnables


def _spec_to_runnables(
    name: str, spec: dict[str, Any], host: str, group: str
) -> list[dict[str, Any]]:
    runnable: dict[str, Any] = {
        "name": name,
        "group": group,
        "host": host,
        "description": spec.get("description") or "",
        "args": _build_args(spec.get("args", {})),
        "autonomy": spec.get("autonomy") or "confirm",
    }
    if spec.get("run_as"):
        run_as = spec["run_as"]
        # run_as may be a string, or a dict with host patterns — surface the default
        if isinstance(run_as, str):
            runnable["runAs"] = run_as
        elif isinstance(run_as, dict):
            runnable["runAs"] = str(run_as.get("default", ""))

    commands = spec.get("commands")
    if commands:
        runnable["commands"] = {
            sub_name: _spec_to_runnables(sub_name, sub_spec, host, group)[0]
            for sub_name, sub_spec in commands.items()
            if isinstance(sub_spec, dict)
        }

    return [runnable]


def _build_args(args_spec: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for name, arg in args_spec.items():
        if not isinstance(arg, dict):
            continue
        arg_type = arg.get("type", "str")
        required = arg.get("required", arg.get("default") is None and arg_type not in ("flag",))
        entry: dict[str, Any] = {
            "name": name,
            "type": arg_type,
            "required": required,
        }
        if arg.get("description"):
            entry["description"] = arg["description"]
        if arg.get("default") is not None:
            entry["default"] = arg["default"]
        if arg.get("options"):
            entry["options"] = arg["options"]
        result.append(entry)
    return result
