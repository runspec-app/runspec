"""
config.py — app-level paths and config file locations.

All persistent state lives under %APPDATA%\runspec-console\ on Windows.
"""

from __future__ import annotations

import os
from pathlib import Path


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
