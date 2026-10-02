"""
loader.py — Reads and normalises runspec.toml into a raw spec dict.

Runnables are top-level sections:
    [greeter]
    [greeter.args]

The reserved name 'config' is used for project-wide settings.
Everything else at the top level is treated as a runnable.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]


def load_raw(config_path: Path) -> dict[str, Any]:
    """
    Read runspec.toml and return the normalised spec dict.

    Args:
        config_path: Path to runspec.toml

    Returns:
        Normalised dict with keys: config, runnables

    Raises:
        ValueError: if the runspec section is malformed
        FileNotFoundError: if config_path does not exist
    """
    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    runnables_raw = {key: value for key, value in data.items() if key != "config" and isinstance(value, dict)}

    return {
        "config": _normalise_config(data.get("config", {})),
        "runnables": _normalise_runnables(runnables_raw),
    }


def _normalise_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise the [config] section, applying defaults."""
    return {
        "autonomy_default": raw.get("autonomy-default", "confirm"),
        "run_as": raw.get("run_as"),
        "enforce_run_as": raw.get("enforce_run_as", "error"),
        "lang": raw.get("lang"),
        "name": raw.get("name"),
        "version": str(raw.get("version", "1")),
        "logging": _normalise_logging(raw.get("logging")),
        "history": _normalise_history(raw.get("history")),
        "audit": _normalise_audit(raw.get("audit")),
        "runspec_env": raw.get("runspec_env"),
    }


def _normalise_audit(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalise [config.audit] — where `runspec.audit()` events go besides the
    venv ledger and the run's own log. Always returns a dict (the audit trail has
    no off switch; these only tune the OS-log sink).

    `syslog` (default true) also writes each event to the OS log — syslog/journald
    on POSIX, the Application event log on Windows. `facility` (default "auth")
    is the syslog facility; unknown names fall back to "auth" at write time.
    """
    raw = raw or {}
    return {
        "syslog": bool(raw.get("syslog", True)),
        "facility": str(raw.get("facility", "auth")),
    }


def _normalise_history(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalise [config.history]. Returns None if the section is absent.

    Controls the per-venv runs index (`runspec_runs.db`) — a derived index over
    the audit log that core writes at run start/finalize so the console History /
    Logs tabs read an indexed query instead of parsing files, and get a live
    "what's running" view. See docs/design/console-history-db.md.

    `db` (default true) writes the index; set false to skip it (History falls back
    to parsing the audit logs). `retention_days` (90) / `max_rows` (50000) bound
    the ring-buffer prune that keeps the file small; `max_run_hours` (24) is the
    orphan age ceiling for reaping a `running` row a crash left behind. A `None`
    section still means "on with defaults" — the write is best-effort, so it never
    affects a run.
    """
    if raw is None:
        return None
    return {
        "db": bool(raw.get("db", True)),
        "retention_days": int(raw.get("retention_days", 90)),
        "max_rows": int(raw.get("max_rows", 50000)),
        "max_run_hours": int(raw.get("max_run_hours", 24)),
    }


def _normalise_logging(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalise [config.logging]. Returns None if section is absent.

    Console routing is fixed: INFO+ → stdout, WARNING+ → stderr. DEBUG is
    file-only unless the caller passes `--debug`. There is no `level` knob —
    silencing INFO would break agent responses (stdout is the MCP tool
    response body), and verbosity for debugging is handled by the `--debug`
    flag injected at parse time.

    `summary` (default true) writes one structured record per run to the audit
    log at process exit: duration, exit code, log-event counts by level. This
    is the data the console history/analytics tabs and `runspec logs` read.

    `summary_console` (default false) additionally echoes a one-line
    human-readable summary to the terminal (stderr). It is off by default so
    stdout stays clean for pipes and the line never lands on a stream that log
    processors (e.g. Rundeck) tag as ERROR. Turn it on for interactive use.

    The record is part of the always-on run audit: `summary = false` is still
    parsed (so existing files keep loading) but ignored since 0.71.0.
    `summary_console = false` (the default) keeps the terminal quiet.
    """
    if raw is None:
        return None
    store = str(raw.get("store", "single"))
    return {
        # false = "audit-only": runspec leaves the process's logging alone (no
        # root handlers, no stdout routing, no print tee, no --debug) but still
        # writes the run record and audit events. See spec/SPEC.md → Logging.
        "enabled": bool(raw.get("enabled", True)),
        "rotate": str(raw.get("rotate", "midnight")),
        "keep": int(raw.get("keep", 7)),
        "summary": bool(raw.get("summary", True)),
        "summary_console": bool(raw.get("summary_console", False)),
        # 'single' = one rotating {runnable}.log; 'per-run' = one file per
        # invocation. Unknown values fall back to 'single' — the schema check
        # flags typos; runtime stays lenient like the rest of this normaliser.
        "store": store if store in ("single", "per-run") else "single",
    }


def _normalise_runnables(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise runnables — everything at the top level except [config]."""
    return {name: _normalise_script(name, script_data) for name, script_data in raw.items()}


def _normalise_script(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise a single script definition."""
    return {
        "name": name,
        "description": raw.get("description"),
        "autonomy": raw.get("autonomy"),
        "autonomy_reason": raw.get("autonomy-reason"),
        "output": raw.get("output", "text"),
        "serve": raw.get("serve"),
        "discoverable": raw.get("discoverable"),
        "run_as": raw.get("run_as"),
        "enforce_run_as": raw.get("enforce_run_as"),
        "become_method": raw.get("become_method", "sudo"),
        "become_flags": raw.get("become_flags"),
        "runspec_env": raw.get("runspec_env"),
        "examples": _normalise_examples(raw.get("examples", [])),
        "args": _normalise_args(raw.get("args", {})),
        "groups": _normalise_groups(raw.get("groups", {})),
        "require_command": bool(raw.get("require-command", False)),
        "commands": {cmd_name: _normalise_script(cmd_name, cmd_data) for cmd_name, cmd_data in raw.get("commands", {}).items()},
    }


def _normalise_examples(raw: Any) -> list[dict[str, str]]:
    """Normalise an examples list. Each entry is a {cmd, description} dict.

    Canonical form is inline TOML tables:
        examples = [
          {cmd = "runspec local",             description = "Discover runnables"},
          {cmd = "runspec local --format mcp", description = "Emit MCP schemas"},
        ]

    Bare strings are also accepted as a shorthand and treated as
    {cmd = <string>, description = ""}.
    """
    if not isinstance(raw, list):
        return []

    result: list[dict[str, str]] = []
    for entry in raw:
        if isinstance(entry, str):
            result.append({"cmd": entry, "description": ""})
        elif isinstance(entry, dict):
            cmd = entry.get("cmd") or entry.get("command")
            if not cmd:
                continue
            result.append(
                {
                    "cmd": str(cmd),
                    "description": str(entry.get("description", "")),
                }
            )
    return result


def _normalise_args(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Expand all arg shorthand forms into full dicts.

    Handles:
      bare value:   verbose = false
      inline table: quality = {default = 85, range = [1, 100]}
      full block:   already a dict, pass through
    """
    normalised: dict[str, Any] = {}

    for name, value in raw.items():
        if name.startswith("runspec_") or name.startswith("runspec-"):
            raise ValueError(f"✗  Arg name '{name}' uses a reserved prefix.\n   Names starting with 'runspec_' or 'runspec-' are reserved for the runspec framework.\n   Rename your argument.")
        if isinstance(value, dict):
            normalised[name] = _normalise_arg(name, value)
        else:
            normalised[name] = _normalise_arg(name, {"default": value})

    return normalised


def _normalise_arg(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise a single argument definition dict."""
    return {
        "name": name,
        "type": raw.get("type"),
        "default": raw.get("default"),
        "required": raw.get("required"),
        "description": raw.get("description"),
        "options": raw.get("options"),
        "range": tuple(raw["range"]) if "range" in raw else None,
        "pattern": raw.get("pattern"),
        "min_length": raw.get("min-length"),
        "max_length": raw.get("max-length"),
        "multiple": raw.get("multiple", False),
        "delimiter": raw.get("delimiter"),
        "short": raw.get("short"),
        "env": _normalise_env(raw.get("env")),
        "deprecated": raw.get("deprecated"),
        "autonomy": raw.get("autonomy"),
        "ui": raw.get("ui"),
        "hint": raw.get("hint"),
        "meta": raw.get("meta"),
        "position": raw.get("position"),
    }


def _normalise_env(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw]
    return list(raw)


def _normalise_groups(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise the groups section of a script."""
    normalised: dict[str, Any] = {}

    for name, group_data in raw.items():
        normalised[name] = {
            "name": name,
            "args": group_data.get("args", []),
            "exclusive": group_data.get("exclusive", False),
            "inclusive": group_data.get("inclusive", False),
            "at_least_one": group_data.get("at-least-one", False),
            "exactly_one": group_data.get("exactly-one", False),
            "condition": group_data.get("if"),
            "requires": group_data.get("requires", []),
        }

    return normalised
