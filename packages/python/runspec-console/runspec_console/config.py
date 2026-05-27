"""
config.py — app-level paths, config file locations, and config I/O helpers.

All persistent state lives under %APPDATA%\runspec-console\ on Windows.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def app_dir() -> Path:
    base = os.environ.get("APPDATA")
    if not base:
        base = Path.home() / "AppData" / "Roaming"
    d = Path(base) / "runspec-console"
    d.mkdir(parents=True, exist_ok=True)
    return d


def hosts_path() -> Path:
    return app_dir() / "runspec_hosts.toml"


def config_path() -> Path:
    return app_dir() / "config.toml"


def read_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return dict(tomllib.load(f))
    except Exception:
        return {}


def write_config(data: dict[str, Any]) -> None:
    config_path().write_text(_dict_to_toml(data), encoding="utf-8")


def _dict_to_toml(data: dict[str, Any]) -> str:
    """Minimal TOML serialiser for flat and one-level-nested dicts."""
    lines: list[str] = []
    nested: list[tuple[str, dict[str, Any]]] = []
    for k, v in data.items():
        if isinstance(v, dict):
            nested.append((k, v))
        elif isinstance(v, str):
            lines.append(f'{k} = "{v}"')
        elif isinstance(v, bool):
            lines.append(f'{k} = {"true" if v else "false"}')
        elif v is None:
            pass
        else:
            lines.append(f"{k} = {v}")
    for section, sub in nested:
        lines.append(f"\n[{section}]")
        for k, v in sub.items():
            if isinstance(v, str):
                lines.append(f'{k} = "{v}"')
            elif isinstance(v, bool):
                lines.append(f'{k} = {"true" if v else "false"}')
            elif v is not None:
                lines.append(f"{k} = {v}")
    return "\n".join(lines) + "\n"
