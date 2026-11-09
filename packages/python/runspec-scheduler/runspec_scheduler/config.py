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
import re
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

#: The two ways a venv's schedules can fire. A venv uses exactly one.
BACKENDS = ("cron", "daemon")

#: Daemon defaults (``[scheduler]`` overrides them).
DEFAULT_TIMEOUT = 6 * 3600  # a fire running longer is killed (0 = no limit)
DEFAULT_MISFIRE_GRACE = 300  # a fire this late still runs; later = recorded as missed
DEFAULT_CATCH_UP_WINDOW = 24 * 3600  # how far back a catch_up schedule looks on start

_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: Any, what: str = "duration") -> int:
    """Seconds from ``7200`` / ``"7200"`` / ``"90m"`` / ``"6h"`` / ``"1d"`` / ``0``.

    Raises ``ValueError`` with a human message. ``0`` is allowed (it means "no
    limit" for a timeout)."""
    if isinstance(value, bool):
        raise ValueError(f"{what} must be a number of seconds or a duration like 90m / 6h (got {value!r})")
    if isinstance(value, int):
        n = value
    else:
        text = str(value).strip().lower()
        if not text:
            raise ValueError(f"{what} is empty")
        mult = 1
        if text[-1] in _DURATION_UNITS:
            mult = _DURATION_UNITS[text[-1]]
            text = text[:-1]
        try:
            n = int(text) * mult
        except ValueError:
            raise ValueError(f"{what} must be a number of seconds or a duration like 90m / 6h (got {value!r})") from None
    if n < 0:
        raise ValueError(f"{what} can't be negative (got {value!r})")
    return n


@dataclass(frozen=True)
class SchedulerSettings:
    """The ``[scheduler]`` table: which backend fires this venv's schedules, plus
    the daemon's defaults."""

    backend: str = "cron"
    timeout: int = DEFAULT_TIMEOUT
    misfire_grace: int = DEFAULT_MISFIRE_GRACE
    catch_up_window: int = DEFAULT_CATCH_UP_WINDOW


def parse_settings(table: Any) -> SchedulerSettings:
    """Validate + parse the ``[scheduler]`` table (``{}``/absent = defaults)."""
    if table is None:
        table = {}
    if not isinstance(table, dict):
        raise ValueError("[scheduler] must be a table")
    backend = str(table.get("backend") or "cron").strip().lower()
    if backend not in BACKENDS:
        raise ValueError(f"[scheduler] backend must be one of {', '.join(BACKENDS)} (got {table.get('backend')!r})")
    return SchedulerSettings(
        backend=backend,
        timeout=parse_duration(table.get("timeout", DEFAULT_TIMEOUT), "[scheduler] timeout"),
        misfire_grace=max(1, parse_duration(table.get("misfire_grace", DEFAULT_MISFIRE_GRACE), "[scheduler] misfire_grace")),
        catch_up_window=parse_duration(table.get("catch_up_window", DEFAULT_CATCH_UP_WINDOW), "[scheduler] catch_up_window"),
    )


#: A schedule id ``add`` accepts: it names a lock file, a crontab marker and the
#: runs-index label ``sched:<id>``. Same rule as runspec-console's host schedules.
SCHEDULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: An id that can't work at all: a path separator or ``..`` (it names a file),
#: or a blank/control character (it's on a crontab line).
_UNSAFE_ID_RE = re.compile(r"[/\\\s\x00-\x1f\x7f]|^\.\.?$")


def schedule_id_problem(sid: str, *, strict: bool = True) -> str:
    """Why ``sid`` can't be a schedule id, or "".

    ``strict`` (what ``add`` checks): letters, digits, ``.``, ``_`` and ``-``,
    starting with a letter or digit, at most 64 long. Otherwise (a config
    written by hand or by an older version) only ids that can never work are
    refused, so an existing schedule doesn't stop for a cosmetic reason."""
    if strict and not SCHEDULE_ID_RE.match(sid):
        return f"schedule id {sid!r} must be letters, digits, '.', '_' or '-', start with a letter or digit, and be at most 64 long"
    if _UNSAFE_ID_RE.search(sid):
        return f"schedule id {sid!r} can't hold '/', '\\', spaces or control characters, or be '.' or '..' — give it a new id"
    return ""


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
    # Seconds before a fire is killed (its whole process group). None = the
    # [scheduler] default (6h on the daemon, none on cron); 0 = no limit.
    timeout: int | None = None
    # Run once on daemon start when fires were missed while it was down (several
    # missed fires collapse into one run). Default: skip them, as cron does.
    catch_up: bool = False

    def effective_timeout(self, settings: SchedulerSettings) -> int:
        """The timeout this schedule fires with on the daemon (0 = none)."""
        return settings.timeout if self.timeout is None else self.timeout

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
        if self.timeout is not None:
            out["timeout"] = self.timeout
        if self.catch_up:
            out["catch_up"] = True
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
        timeout=None if data.get("timeout") is None else parse_duration(data.get("timeout"), f"schedule '{sid}' timeout"),
        catch_up=_bool(data.get("catch_up", False), f"schedule '{sid}' catch_up"),
    )


def _bool(value: Any, what: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{what} must be true or false (got {value!r})")


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
    return load_config(path)[1]


def load_settings(path: Path) -> SchedulerSettings:
    """Parse the ``[scheduler]`` table. Invalid values raise ``ValueError``."""
    return parse_settings(read_config(path).get("scheduler"))


def load_config(path: Path) -> tuple[SchedulerSettings, list[ScheduleEntry]]:
    """Settings + schedules from one read of the file (so the two can't come from
    different versions of it). Duplicate ids are refused: the id is the key of
    the crontab marker, the run label and the daemon job."""
    data = read_config(path)
    settings = parse_settings(data.get("scheduler"))
    entries = [parse_entry(raw) for raw in data.get("schedule", [])]
    seen: set[str] = set()
    for e in entries:
        if e.id in seen:
            raise ValueError(f"schedule id '{e.id}' appears more than once")
        seen.add(e.id)
    return settings, entries


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
    for key in ("id", "runnable", "enabled", "run_as", "become_method", "env_file", "timeout", "catch_up"):
        if key in entry:
            lines.append(f"{key} = {_toml_scalar(entry[key])}")
    if entry.get("command"):
        lines.append("command = [" + ", ".join(_toml_str(str(p)) for p in entry["command"]) + "]")
    for key in ("when", "args"):
        if key in entry and entry[key]:
            lines.append(f"{key} = {_inline_table(entry[key])}")
    return "\n".join(lines)


def _render_settings(table: dict[str, Any]) -> str:
    lines = ["[scheduler]"]
    for key, value in table.items():
        if isinstance(value, (str, int, float, bool)):
            lines.append(f"{key} = {_toml_scalar(value)}")
    return "\n".join(lines)


def write_schedules(path: Path, entries: list[ScheduleEntry], scheduler: dict[str, Any] | None = None) -> None:
    """Atomically write the schedule list to ``path`` (temp file + os.replace).

    The ``[scheduler]`` table is kept as it is in the file unless ``scheduler``
    replaces it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if scheduler is None:
        try:
            scheduler = dict(read_config(path).get("scheduler") or {})
        except Exception:  # noqa: BLE001 — a broken file is being overwritten anyway
            scheduler = {}
    header = "# runspec-scheduler config — managed by `runspec-scheduler add/remove/use`\n# and by runspec-console. A running daemon reloads it when it changes.\n\n"
    parts = ([_render_settings(scheduler)] if scheduler else []) + [_render_entry(e.to_dict()) for e in entries]
    body = "\n\n".join(parts)
    text = header + body + ("\n" if body else "")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def set_backend(path: Path, backend: str) -> None:
    """Write ``[scheduler] backend`` (keeping the schedules and other settings)."""
    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of {', '.join(BACKENDS)}")
    data = read_config(path)
    table = dict(data.get("scheduler") or {})
    table["backend"] = backend
    write_schedules(path, [parse_entry(raw) for raw in data.get("schedule", [])], scheduler=table)


#: The ``[scheduler]`` durations ``settings`` may change (backend has ``use``).
SETTING_KEYS = ("timeout", "misfire_grace", "catch_up_window")


def set_settings(path: Path, changes: dict[str, Any]) -> SchedulerSettings:
    """Write ``[scheduler]`` durations (keeping the schedules, the backend and
    any other key). Each value is validated as the daemon will read it before
    anything is written; ``None`` puts a setting back to its default."""
    unknown = sorted(set(changes) - set(SETTING_KEYS))
    if unknown:
        raise ValueError(f"not a setting: {', '.join(unknown)} (one of: {', '.join(SETTING_KEYS)})")
    data = read_config(path)
    table = dict(data.get("scheduler") or {})
    for key, value in changes.items():
        if value is None or value == "":
            table.pop(key, None)
        else:
            table[key] = value
    settings = parse_settings(table)  # raises on a bad value, before the write
    write_schedules(path, [parse_entry(raw) for raw in data.get("schedule", [])], scheduler=table)
    return settings


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
