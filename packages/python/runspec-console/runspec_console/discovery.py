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
            encoding="utf-8", errors="replace",
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
        runnables.extend(_spec_to_runnables(item["runnable"], item["spec"], host, group, item.get("config_autonomy")))
    return runnables


def discover_remote(ssh_target: str, runspec_path: str, host: str, identity_file: str | None = None, ssh_binary: str = "ssh") -> list[dict[str, Any]]:
    """SSH + runspec local --format json to discover runnables on a remote host."""
    from .executor import ssh_flags
    group = venv_name(runspec_path)
    cmd = [ssh_binary, *ssh_flags(identity_file, ssh_binary), ssh_target, runspec_path, "local", "--format", "json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                                encoding="utf-8", errors="replace")
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
        runnables.extend(_spec_to_runnables(item["runnable"], item["spec"], host, group, item.get("config_autonomy")))
    return runnables


def _parse_toml(
    toml_path: Path, host: str, group: str, bin_dir: Path
) -> list[dict[str, Any]]:
    try:
        with open(toml_path, "rb") as f:
            raw = tomllib.load(f)
    except Exception:
        return []

    config_autonomy: str | None = None
    if isinstance(raw.get("config"), dict):
        config_autonomy = raw["config"].get("autonomy")
    runnables: list[dict[str, Any]] = []
    for name, spec in raw.items():
        if name == "config" or not isinstance(spec, dict):
            continue
        # Only include runnables that have an installed entry point
        if not (bin_dir / name).exists() and not (bin_dir / f"{name}.exe").exists():
            continue
        runnables.extend(_spec_to_runnables(name, spec, host, group, config_autonomy))
    return runnables


_RAW_SPEC_EXCLUDE = frozenset({"args", "commands"})


def _spec_to_runnables(
    name: str, spec: dict[str, Any], host: str, group: str,
    config_autonomy: str | None = None,
) -> list[dict[str, Any]]:
    runnable: dict[str, Any] = {
        "name": name,
        "group": group,
        "host": host,
        "description": spec.get("description") or "",
        "args": _build_args(spec.get("args", {})),
        "autonomy": spec.get("autonomy") or config_autonomy or "confirm",
        # Full raw spec minus processed keys — SpecPanel uses this for the holistic view.
        "rawSpec": {k: v for k, v in spec.items() if k not in _RAW_SPEC_EXCLUDE},
    }
    if spec.get("run_as"):
        run_as = spec["run_as"]
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
        # load_raw normalises args with "required": None when not set in TOML,
        # so dict.get(key, fallback) won't trigger — check for None explicitly.
        required_raw = arg.get("required")
        required = required_raw if required_raw is not None else (arg.get("default") is None and arg_type not in ("flag",))
        entry: dict[str, Any] = {
            "name": name,
            "type": arg_type,
            "required": required,
        }
        # Optional fields — only include if present in spec
        for key in ("description", "options", "short", "deprecated", "ui", "env"):
            if arg.get(key) is not None:
                entry[key] = arg[key]
        if arg.get("default") is not None:
            entry["default"] = arg["default"]
        if arg.get("range") is not None:
            entry["range"] = arg["range"]
        if arg.get("multiple"):
            entry["multiple"] = True
        if arg.get("position") is not None:
            entry["position"] = arg["position"]
        if arg.get("autonomy"):
            entry["autonomy"] = arg["autonomy"]
       