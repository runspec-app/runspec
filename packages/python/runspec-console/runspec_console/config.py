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

# Where the in-app "Documentation" icon points. Overridable per-install via
# `[console] docs_url` in config.toml so a fork / internal mirror can retarget it.
DEFAULT_DOCS_URL = "https://jasonfinestone.github.io/runspec/"


def app_dir() -> Path:
    env = os.environ.get("APPDATA")
    base = Path(env) if env else Path.home() / "AppData" / "Roaming"
    d = base / "runspec-console"
    d.mkdir(parents=True, exist_ok=True)
    return d


def hosts_path() -> Path:
    return app_dir() / "runspec_hosts.toml"


def filters_path() -> Path:
    return app_dir() / "runspec_filters.toml"


def config_path() -> Path:
    return app_dir() / "config.toml"


def triggers_path() -> Path:
    return app_dir() / "runspec_triggers.toml"


def read_triggers() -> list[dict[str, Any]]:
    """Parse ``runspec_triggers.toml`` into a list of ``[[triggers]]`` tables.

    Returns ``[]`` on a missing or unreadable file — triggers are optional.
    """
    path = triggers_path()
    if not path.exists():
        return []
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return []
    raw = data.get("triggers")
    return [dict(t) for t in raw] if isinstance(raw, list) else []


def write_triggers(triggers: list[dict[str, Any]]) -> None:
    """Serialise a list of trigger tables to ``runspec_triggers.toml``."""
    triggers_path().write_text(_triggers_to_toml(triggers), encoding="utf-8")


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


def docs_url() -> str:
    """Documentation URL: `[console] docs_url` from config, else the default."""
    console = read_config().get("console")
    if isinstance(console, dict):
        url = console.get("docs_url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    return DEFAULT_DOCS_URL


def _toml_str(v: str) -> str:
    """Escape a Python string as a TOML basic string literal.

    TOML basic strings (double-quoted) recognise backslash escapes — meaning
    a Windows path like ``C:\\Program Files\\PuTTY\\plink.exe`` written
    verbatim is invalid TOML (``\\P`` is not a recognised escape) and
    ``tomllib`` will refuse to read the file back. Escape ``\\`` / ``"`` and the
    common control chars (so a multiline ``rules`` string survives) before
    wrapping in quotes, so the round-trip preserves the original string.
    """
    escaped = (
        v.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _toml_scalar(v: Any) -> str:
    """Render a scalar (or list of scalars) as a TOML value."""
    if isinstance(v, str):
        return _toml_str(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return "[" + ", ".join(_toml_scalar(x) for x in v) + "]"
    return str(v)


def _triggers_to_toml(triggers: list[dict[str, Any]]) -> str:
    """Serialise ``[[triggers]]`` array-of-tables, each with optional nested
    ``match`` / ``map`` / ``reply`` sub-tables. Scalars are written before
    sub-tables (TOML requires a table's own keys to precede its child tables)."""
    blocks: list[str] = []
    for t in triggers:
        lines = ["[[triggers]]"]
        subtables = [(k, v) for k, v in t.items() if isinstance(v, dict)]
        for k, v in t.items():
            if isinstance(v, dict) or v is None:
                continue
            lines.append(f"{k} = {_toml_scalar(v)}")
        for name, sub in subtables:
            lines.append(f"[triggers.{name}]")
            for kk, vv in sub.items():
                if vv is not None:
                    lines.append(f"{kk} = {_toml_scalar(vv)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _dict_to_toml(data: dict[str, Any]) -> str:
    """Minimal TOML serialiser for flat and one-level-nested dicts."""
    lines: list[str] = []
    nested: list[tuple[str, dict[str, Any]]] = []
    for k, v in data.items():
        if isinstance(v, dict):
            nested.append((k, v))
        elif isinstance(v, str):
            lines.append(f"{k} = {_toml_str(v)}")
        elif isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif v is None:
            pass
        else:
            lines.append(f"{k} = {v}")
    for section, sub in nested:
        lines.append(f"\n[{section}]")
        for k, v in sub.items():
            if isinstance(v, str):
                lines.append(f"{k} = {_toml_str(v)}")
            elif isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif v is not None:
                lines.append(f"{k} = {v}")
    return "\n".join(lines) + "\n"
