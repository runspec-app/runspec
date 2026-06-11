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


def _read_trigger_file() -> dict[str, Any]:
    """Full parse of ``runspec_triggers.toml`` ({} on missing/unreadable)."""
    path = triggers_path()
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return dict(tomllib.load(f))
    except Exception:
        return {}


def read_triggers() -> list[dict[str, Any]]:
    """The ``[triggers.<name>]`` tables ([] on missing/unreadable — optional).

    One canonical shape, mirroring ``runspec.toml``'s named runnable sections:
    a named table per trigger, ``[triggers.<name>]``, with its sub-tables
    namespaced under the same name (``[triggers.<name>.match]`` / ``.map`` /
    ``.reply``). The table key *is* the trigger's name, so no ``name`` keyword is
    needed (and any in-table ``name`` is ignored — the key wins). Grouping each
    trigger under one prefix means a misplaced key can't bleed into the trigger
    above it.
    """
    raw = _read_trigger_file().get("triggers")
    if not isinstance(raw, dict):
        return []  # missing, or the unsupported [[triggers]] array form
    out: list[dict[str, Any]] = []
    for key, val in raw.items():
        if isinstance(val, dict):
            trig = dict(val)
            trig["name"] = key  # the table key is authoritative
            out.append(trig)
    return out


def trigger_file_error() -> str | None:
    """A human message if ``runspec_triggers.toml`` exists but isn't valid TOML.

    A single trigger's syntax error makes ``tomllib`` reject the *whole* file, so
    every trigger silently disappears. Surfacing this lets the UI say *why*
    instead of just showing zero triggers.
    """
    path = triggers_path()
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        return f"runspec_triggers.toml is not valid TOML — all triggers disabled until fixed: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"runspec_triggers.toml could not be read: {exc}"
    if isinstance(data.get("triggers"), list):
        return (
            "runspec_triggers.toml uses the old [[triggers]] array form, which is "
            "no longer supported. Use a named table per trigger instead: "
            "[triggers.<name>] with [triggers.<name>.match] / .map / .reply."
        )
    return None


def read_rota() -> dict[str, Any]:
    """The ``[rota]`` working-hours table ({} when unset)."""
    rota = _read_trigger_file().get("rota")
    return dict(rota) if isinstance(rota, dict) else {}


def _write_trigger_file(rota: dict[str, Any], triggers: list[dict[str, Any]]) -> None:
    parts = []
    if rota:
        parts.append(_rota_to_toml(rota))
    if triggers:
        parts.append(_triggers_to_toml(triggers))
    triggers_path().write_text("\n".join(parts), encoding="utf-8")


def write_triggers(triggers: list[dict[str, Any]]) -> None:
    """Save the named ``[triggers.<name>]`` tables, preserving the ``[rota]``."""
    _write_trigger_file(read_rota(), triggers)


def write_rota(rota: dict[str, Any]) -> None:
    """Save the ``[rota]`` working-hours table, preserving the existing triggers."""
    _write_trigger_file(rota, read_triggers())


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


def confirm_notifications_enabled() -> bool:
    """`[console] confirm_notifications` — raise a desktop toast when an agent
    tool call hits the autonomy gate. Defaults to True; set false to silence the
    OS notification while keeping the in-app Approve/Deny card."""
    console = read_config().get("console")
    if isinstance(console, dict):
        val = console.get("confirm_notifications")
        if isinstance(val, bool):
            return val
    return True


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
    """Serialise named ``[triggers.<name>]`` tables (the recommended shape), each
    with optional nested ``match`` / ``map`` / ``reply`` sub-tables.

    The trigger's ``name`` becomes the table key, so it is *not* re-emitted as a
    scalar — ``read_triggers`` reconstructs it from the key. Scalars are written
    before sub-tables (TOML requires a table's own keys to precede its child
    tables). Keys are quoted, so a name with dots/spaces stays one table; a
    fallback name is synthesised (and duplicates de-duped) so output is always
    valid TOML."""
    blocks: list[str] = []
    seen: set[str] = set()
    for i, t in enumerate(triggers):
        name = str(t.get("name") or f"trigger{i + 1}")
        key = name
        n = 2
        while key in seen:  # keep table keys unique even if names collide
            key = f"{name}-{n}"
            n += 1
        seen.add(key)
        lines = [f"[triggers.{_toml_str(key)}]"]
        subtables = [(k, v) for k, v in t.items() if isinstance(v, dict)]
        for k, v in t.items():
            if isinstance(v, dict) or v is None or k == "name":
                continue
            lines.append(f"{k} = {_toml_scalar(v)}")
        for sub_name, sub in subtables:
            lines.append(f"[triggers.{_toml_str(key)}.{sub_name}]")
            for kk, vv in sub.items():
                if vv is not None:
                    lines.append(f"{kk} = {_toml_scalar(vv)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _rota_to_toml(rota: dict[str, Any]) -> str:
    """Serialise the ``[rota]`` table — scalar keys (e.g. ``enabled``) first, then
    a ``[rota.<day>]`` sub-table per weekday ({on, start, end})."""
    lines = ["[rota]"]
    subtables = [(k, v) for k, v in rota.items() if isinstance(v, dict)]
    for k, v in rota.items():
        if isinstance(v, dict) or v is None:
            continue
        lines.append(f"{k} = {_toml_scalar(v)}")
    for name, sub in subtables:
        lines.append(f"[rota.{name}]")
        for kk, vv in sub.items():
            if vv is not None:
                lines.append(f"{kk} = {_toml_scalar(vv)}")
    return "\n".join(lines) + "\n"


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
