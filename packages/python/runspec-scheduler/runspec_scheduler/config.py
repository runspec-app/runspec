"""config.py — the venv-scoped scheduler config (``runspec_scheduler.toml``).

The config holds ``[[schedule]]`` entries for runnables installed in *this* venv.
It is deliberately **local-only**: no host/group targeting (the daemon only runs
its own venv on localhost). The location mirrors the denylist pattern
(``{sys.prefix}/runspec_scheduler.toml``), overridable by ``--config`` or
``$RUNSPEC_SCHEDULER_CONFIG``.

Writes are **atomic** (temp file + ``os.replace``) so the daemon's mtime-poll
reader never sees a half-written file. Reads are tolerant: a missing file is an
empty schedule list; a broken file raises so the caller can report it.

Stdlib only (``tomllib`` on 3.11+, ``tomli`` on 3.10). There is no stdlib TOML
*writer*, so we hand-serialize the small, known ``[[schedule]]`` shape.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .timing import When, parse_when

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib

DEFAULT_CONFIG_NAME = "runspec_scheduler.toml"


@dataclass(frozen=True)
class ScheduleEntry:
    """One ``[[schedule]]`` entry — a runnable in this venv on a cadence."""

    id: str
    when: When
    runnable: str
    args: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    # The service account this schedule escalates to (from the runnable's run_as,
    # resolved for this host at add-time). Empty = run as the daemon's own user.
    run_as: str = ""
    become_method: str = "sudo"
    # Absolute path to a per-schedule credential env file (owned by run_as, mode
    # 600, in the run_as account's HOME) written when console credentials were
    # attached. The daemon points RUNSPEC_ENV_FILE at it when firing. Empty = none.
    env_file: str = ""
    # The subcommand path for a runnable with verbs (e.g. ("list-directory",) for
    # `filesystem list-directory`, or ("admin", "users") for a nested one). Empty =
    # the runnable itself, which is only valid when it doesn't require a command.
    command: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        when: dict[str, Any] = {}
        if self.when.cron:
            when["cron"] = self.when.cron
        if self.when.every:
            when["every"] = self.when.every
        if self.when.timezone:
            when["timezone"] = self.when.timezone
        out: dict[str, Any] = {
            "id": self.id,
            "runnable": self.runnable,
            "enabled": self.enabled,
            "when": when,
        }
        if self.command:
            out["command"] = list(self.command)
        if self.args:
            out["args"] = dict(self.args)
        if self.run_as:
            out["run_as"] = self.run_as
            out["become_method"] = self.become_method
        if self.env_file:
            out["env_file"] = self.env_file
        return out


def parse_entry(data: dict[str, Any]) -> ScheduleEntry:
    """Validate + parse one ``[[schedule]]`` table. Raises ``ValueError``."""
    sid = str(data.get("id") or "").strip()
    if not sid:
        raise ValueError("schedule is missing an 'id'")
    runnable = str(data.get("runnable") or "").strip()
    if not runnable:
        raise ValueError(f"schedule '{sid}' is missing 'runnable'")
    when = parse_when(data.get("when") or {})
    return ScheduleEntry(
        command=parse_command(data.get("command")),
        id=sid,
        when=when,
        runnable=runnable,
        args=dict(data.get("args") or {}),
        enabled=bool(data.get("enabled", True)),
        run_as=str(data.get("run_as") or "").strip(),
        become_method=str(data.get("become_method") or "sudo").strip() or "sudo",
        env_file=str(data.get("env_file") or "").strip(),
    )


def parse_command(raw: Any) -> tuple[str, ...]:
    """A schedule's subcommand path from config/CLI: a TOML array of strings
    (``["admin", "users"]``) or a space-separated string (``"admin users"``).
    ``None``/empty → ``()``. Raises ``ValueError`` on any other shape."""
    if raw is None or raw == "":
        return ()
    if isinstance(raw, str):
        return tuple(raw.split())
    if isinstance(raw, (list, tuple)) and all(isinstance(p, str) for p in raw):
        return tuple(p.strip() for p in raw if p.strip())
    raise ValueError(f"'command' must be a string or a list of strings, got {raw!r}")


def config_path(explicit: str | None = None) -> Path:
    """Resolve the config path: ``--config`` > ``$RUNSPEC_SCHEDULER_CONFIG`` >
    ``{sys.prefix}/runspec_scheduler.toml``. Returned even if absent."""
    if explicit:
        return Path(explicit)
    env = (os.environ.get("RUNSPEC_SCHEDULER_CONFIG") or "").strip()
    if env:
        return Path(env)
    return Path(sys.prefix) / DEFAULT_CONFIG_NAME


def read_config(path: Path) -> dict[str, Any]:
    """Parse the TOML config into a dict, or ``{}`` when the file is absent."""
    if not path.is_file():
        return {}
    with open(path, "rb") as f:
        return dict(tomllib.load(f))


def load_schedules(path: Path) -> list[ScheduleEntry]:
    """Parse all ``[[schedule]]`` entries. Invalid entries raise ``ValueError``."""
    data = read_config(path)
    return [parse_entry(raw) for raw in data.get("schedule", [])]


# ── serialization (hand-written; no stdlib TOML writer) ───────────────────────


def _toml_str(value: str) -> str:
    """A double-quoted TOML basic string with the necessary escapes."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return _toml_str(str(value))


def _inline_table(table: dict[str, Any]) -> str:
    inner = ", ".join(f"{k} = {_toml_scalar(v)}" for k, v in table.items())
    return "{ " + inner + " }" if inner else "{}"


def _render_entry(entry: dict[str, Any]) -> str:
    lines = ["[[schedule]]"]
    # Scalars first (stable, readable order), then the command array, then inline
    # tables last.
    for key in ("id", "runnable", "enabled", "run_as", "become_method", "env_file"):
        if key in entry:
            lines.append(f"{key} = {_toml_scalar(entry[key])}")
    if entry.get("command"):
        lines.append("command = [" + ", ".join(_toml_str(str(p)) for p in entry["command"]) + "]")
    for key in ("when", "args"):
        if key in entry and entry[key]:
            lines.append(f"{key} = {_inline_table(entry[key])}")
    return "\n".join(lines)


def write_schedules(path: Path, entries: list[ScheduleEntry]) -> None:
    """Atomically write the schedule list to ``path`` (temp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "# runspec-scheduler config — managed by `runspec-scheduler add/remove`\n# and by runspec-console. Edit by hand only when the daemon is stopped.\n\n"
    body = "\n\n".join(_render_entry(e.to_dict()) for e in entries)
    text = header + body + ("\n" if body else "")
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def upsert_schedule(path: Path, entry: ScheduleEntry) -> list[ScheduleEntry]:
    """Add ``entry`` or replace the one with the same id, then write. Returns the
    new list."""
    entries = load_schedules(path)
    entries = [e for e in entries if e.id != entry.id]
    entries.append(entry)
    write_schedules(path, entries)
    return entries


def remove_schedule(path: Path, schedule_id: str) -> bool:
    """Remove the schedule with ``schedule_id``. Returns True if one was removed."""
    entries = load_schedules(path)
    kept = [e for e in entries if e.id != schedule_id]
    if len(kept) == len(entries):
        return False
    write_schedules(path, kept)
    return True
