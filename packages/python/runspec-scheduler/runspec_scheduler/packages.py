"""packages.py — schedules that installed runnables declare in their runspec.toml.

A package author writes one table per schedule under the runnable's ``meta``:

.. code-block:: toml

    [cleanup-tmp.meta.scheduler.nightly]     # schedule id: cleanup-tmp.nightly
    description = "Remove old files from /tmp"
    at       = "02:30"          # or "midnight", "noon"
    on       = "daily"          # or "weekdays", "mon-fri", "weekends", "sun", "1", "last"
    timezone = "local"          # "local" (default), "UTC", or an IANA name
    spread   = "30m"            # each host fires at a fixed offset inside 0-30m
    args     = {older-than = "7d"}

Exactly one of ``at`` (with ``on``), ``every`` or ``cron``. ``meta`` is a
pass-through for core runspec, so it reaches here only from the raw TOML (the
normaliser drops runnable-level ``meta``), read with ``include`` resolved.

What a venv does with them is its ``[scheduler] package_schedules`` policy
(``offer``, the default, lists them switched off; ``auto`` turns them on;
``off`` ignores them) — see :func:`runspec_scheduler.config.effective_config`.
This module only reads and checks them: :func:`scan` returns every package
schedule as a ``[[schedule]]``-shaped dict plus the problems found.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib

#: The keys a package schedule table may hold (anything else is a typo).
KEYS = ("description", "at", "on", "every", "cron", "timezone", "spread", "args", "command", "timeout", "catch_up")

#: A schedule's name inside its runnable (the id is ``<runnable>.<name>``).
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

#: What a venv may do with package schedules.
POLICIES = ("offer", "auto", "off")

#: ``timezone = "local"``: the venv's ``[scheduler] timezone``, else the host's.
LOCAL = "local"

_TIME_WORDS = {"midnight": (0, 0), "noon": (12, 0)}
_DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_DAY_NAMES = {d: d for d in _DAYS} | {
    "sunday": "sun",
    "monday": "mon",
    "tuesday": "tue",
    "wednesday": "wed",
    "thursday": "thu",
    "friday": "fri",
    "saturday": "sat",
}
_ON_WORDS = {
    "daily": ("*", "*"),
    "every day": ("*", "*"),
    "weekdays": ("*", "mon-fri"),
    "weekends": ("*", "sat,sun"),
    "last": ("last", "*"),
}
#: The longest spread (a spread never moves a fire past midnight either).
MAX_SPREAD = 12 * 3600


# ── the words ─────────────────────────────────────────────────────────────────


def parse_at(at: Any) -> tuple[int, int]:
    """``"02:30"`` / ``"midnight"`` / ``"noon"`` → ``(hour, minute)``."""
    text = str(at).strip().lower()
    if text in _TIME_WORDS:
        return _TIME_WORDS[text]
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f'\'at\' must be a time like "02:30", or "midnight" / "noon" (got {at!r})')
    return int(m.group(1)), int(m.group(2))


def _day(token: str) -> str:
    day = _DAY_NAMES.get(token)
    if day is None:
        raise ValueError(f"{token!r} isn't a day (use mon, tue, … or monday, …)")
    return day


def parse_on(on: Any) -> tuple[str, str]:
    """``on`` → ``(day of month, day of week)`` cron fields.

    ``daily`` (default), ``weekdays`` / ``mon-fri``, ``weekends``, a day or a
    list/range of days (``sun``, ``mon,wed,fri``, ``tue-thu``), a day of the
    month (``1``, ``1,15``), or ``last`` (the month's last day; daemon only)."""
    text = re.sub(r"\s*([,-])\s*", r"\1", str(on if on is not None else "daily").strip().lower())
    if not text:
        text = "daily"
    if text in _ON_WORDS:
        return _ON_WORDS[text]
    if re.fullmatch(r"\d{1,2}(,\d{1,2})*", text):
        days = [int(d) for d in text.split(",")]
        if any(d < 1 or d > 31 for d in days):
            raise ValueError(f"'on' day of the month must be 1-31 (got {on!r})")
        return ",".join(str(d) for d in days), "*"
    try:
        parts = []
        for token in text.split(","):
            if "-" in token:
                a, _, b = token.partition("-")
                parts.append(f"{_day(a)}-{_day(b)}")
            else:
                parts.append(_day(token))
    except ValueError as exc:
        raise ValueError(f"'on' must be daily, weekdays, weekends, days like mon,wed or tue-thu, a day of the month, or last: {exc}") from None
    return "*", ",".join(parts)


def spread_offset(spread: int, host: str, sid: str) -> int:
    """This host's fixed offset (seconds, whole minutes) inside ``spread``: a hash
    of host name and schedule id, so a host always fires at the same time and
    the fleet spreads over the window."""
    minutes = spread // 60
    if minutes <= 0:
        return 0
    digest = hashlib.sha256(f"{host}\0{sid}".encode()).digest()
    return (int.from_bytes(digest[:8], "big") % minutes) * 60


def words_to_cron(hour: int, minute: int, on: tuple[str, str], offset: int = 0) -> str:
    """The 5-field cron line for ``at`` + ``on``, moved ``offset`` seconds later
    but never past midnight (so the day ``on`` names stays right)."""
    start = hour * 60 + minute
    shifted = min(start + offset // 60, 24 * 60 - 1)
    dom, dow = on
    return f"{shifted % 60} {shifted // 60} {dom} * {dow}"


def describe(table: dict[str, Any], offset: int = 0) -> str:
    """A package schedule's timing in words, e.g. ``02:30 weekdays, spread 30m (this host 02:47)``."""
    if table.get("every"):
        text = f"every {table['every']}"
    elif table.get("cron"):
        text = str(table["cron"])
    else:
        hour, minute = parse_at(table.get("at"))
        text = f"{hour:02d}:{minute:02d} {str(table.get('on') or 'daily').strip()}"
        if table.get("spread"):
            start = hour * 60 + minute
            shifted = min(start + offset // 60, 24 * 60 - 1)
            text += f", spread {table['spread']} (this host {shifted // 60:02d}:{shifted % 60:02d})"
    zone = str(table.get("timezone") or LOCAL)
    if zone != LOCAL:
        text += f" ({zone})"
    return text


# ── one table ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PackageSchedule:
    """One schedule a runnable declares, as a ``[[schedule]]`` dict (``base``)."""

    id: str
    runnable: str
    name: str
    description: str
    words: str
    source: str
    base: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "runnable": self.runnable, "name": self.name, "description": self.description, "words": self.words, "source": self.source, "when": dict(self.base.get("when") or {})}


def _zone(value: Any) -> str:
    zone = str(value if value is not None else LOCAL).strip() or LOCAL
    if zone.lower() == LOCAL:
        return LOCAL
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(zone)
    except Exception:  # noqa: BLE001 — any lookup failure is an unknown zone
        raise ValueError(f'timezone {zone!r} isn\'t a zone name (use "local", "UTC" or one like "Europe/London")') from None
    return zone


def parse_table(runnable: str, name: str, table: Any, *, host: str, source: str = "", run_as: str = "", become_method: str = "sudo") -> PackageSchedule:
    """Check one ``[<runnable>.meta.scheduler.<name>]`` table. Raises ``ValueError``."""
    from .config import parse_duration, schedule_id_problem
    from .timing import expand_cron, parse_when

    if not NAME_RE.match(name):
        raise ValueError(f"schedule name {name!r} must be letters, digits, '_' or '-' (at most 32), starting with a letter or digit")
    sid = f"{runnable}.{name}"
    problem = schedule_id_problem(sid)
    if problem:
        raise ValueError(problem)
    if not isinstance(table, dict):
        raise ValueError("must be a table")
    unknown = sorted(set(table) - set(KEYS))
    if unknown:
        raise ValueError(f"unknown key(s) {', '.join(unknown)} (known: {', '.join(KEYS)})")
    kinds = [k for k in ("at", "every", "cron") if table.get(k)]
    if len(kinds) != 1:
        raise ValueError("needs exactly one of 'at' (with 'on'), 'every' or 'cron'")
    if table.get("on") and not table.get("at"):
        raise ValueError("'on' goes with 'at' (e.g. at = \"02:30\", on = \"mon-fri\")")
    zone = _zone(table.get("timezone"))
    spread = parse_duration(table.get("spread") or 0, "spread")
    if spread and not table.get("at"):
        raise ValueError("'spread' works with 'at' only")
    if spread > MAX_SPREAD:
        raise ValueError("'spread' can be at most 12h")
    offset = spread_offset(spread, host, sid)
    if table.get("at"):
        hour, minute = parse_at(table["at"])
        cron = words_to_cron(hour, minute, parse_on(table.get("on")), offset)
        when: dict[str, Any] = {"cron": cron}
    elif table.get("cron"):
        expand_cron(str(table["cron"]))
        when = {"cron": str(table["cron"]).strip()}
    else:
        when = {"every": str(table["every"]).strip()}
    when["timezone"] = zone
    parse_when(when)  # every / cron syntax
    args = table.get("args") or {}
    if not isinstance(args, dict):
        raise ValueError("'args' must be a table, e.g. args = {path = \"/tmp\"}")
    base: dict[str, Any] = {"id": sid, "runnable": runnable, "when": when, "args": dict(args)}
    if table.get("command"):
        from .config import parse_command

        base["command"] = list(parse_command(table["command"]))
    if table.get("timeout") is not None:
        base["timeout"] = parse_duration(table["timeout"], "timeout")
    if "catch_up" in table:
        if not isinstance(table["catch_up"], bool):
            raise ValueError("'catch_up' must be true or false")
        base["catch_up"] = table["catch_up"]
    if run_as:
        base["run_as"] = run_as
        base["become_method"] = become_method
    return PackageSchedule(
        id=sid,
        runnable=runnable,
        name=name,
        description=str(table.get("description") or "").strip(),
        words=describe(table, offset),
        source=source,
        base=base,
    )


# ── every installed runnable ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Scan:
    """Every package schedule in this venv, and what was wrong with the rest."""

    schedules: tuple[PackageSchedule, ...] = ()
    errors: tuple[dict[str, str], ...] = ()

    @property
    def signature(self) -> str:
        """Changes when a package adds, drops or changes a schedule."""
        data = [s.base | {"_d": s.description} for s in self.schedules] + list(self.errors)
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()

    def by_id(self) -> dict[str, PackageSchedule]:
        return {s.id: s for s in self.schedules}


def _read_raw(source: str) -> dict[str, Any]:
    path = Path(source)
    try:
        from runspec.loader import read_resolved
    except ImportError:  # pragma: no cover - runspec < 0.77
        with open(path, "rb") as f:
            return dict(tomllib.load(f))
    return dict(read_resolved(path))


def schedules_in(raw: dict[str, Any], runnable: str) -> tuple[dict[str, Any], str]:
    """The ``meta.scheduler`` tables of one runnable in a raw runspec.toml, and
    a problem with their shape ("" when fine)."""
    node = raw.get(runnable)
    meta = node.get("meta") if isinstance(node, dict) else None
    tables = meta.get("scheduler") if isinstance(meta, dict) else None
    if tables is None:
        return {}, ""
    if not isinstance(tables, dict):
        return {}, "meta.scheduler must be a table of named schedules"
    loose = sorted(k for k, v in tables.items() if not isinstance(v, dict))
    if loose:
        return {k: v for k, v in tables.items() if isinstance(v, dict)}, (f"meta.scheduler has {', '.join(loose)} directly — put each schedule under a name: [{runnable}.meta.scheduler.<name>]")
    return tables, ""


def scan(hostname: str | None = None) -> Scan:
    """Read every installed runnable's package schedules (the scheduler's own and
    ``runspec`` are never scheduled). A broken table is reported, never raised:
    one package's typo mustn't stop the venv's other schedules."""
    from runspec.become import resolve_run_as

    from .discovery import discover

    host = hostname or socket.gethostname()
    found: list[PackageSchedule] = []
    errors: list[dict[str, str]] = []
    raw_cache: dict[str, dict[str, Any]] = {}
    try:
        records = discover()
    except Exception as exc:  # noqa: BLE001 — a broken install is reported, not raised
        return Scan(errors=({"runnable": "", "id": "", "error": f"could not list the installed runnables: {exc}"},))
    for name, record in sorted(records.items()):
        source = str(record.get("source") or "")
        if not source:
            continue
        if source not in raw_cache:
            try:
                raw_cache[source] = _read_raw(source)
            except Exception as exc:  # noqa: BLE001
                errors.append({"runnable": name, "id": "", "error": f"could not read {source}: {exc}"})
                raw_cache[source] = {}
        tables, shape = schedules_in(raw_cache[source], name)
        if shape:
            errors.append({"runnable": name, "id": "", "error": shape})
        spec = record.get("spec") or {}
        run_as = resolve_run_as(spec.get("run_as"), host)
        method = str(spec.get("become_method") or "sudo").strip() or "sudo"
        for sched_name, table in sorted(tables.items()):
            try:
                found.append(parse_table(name, sched_name, table, host=host, source=source, run_as=run_as, become_method=method))
            except ValueError as exc:
                errors.append({"runnable": name, "id": f"{name}.{sched_name}", "error": str(exc)})
    return Scan(schedules=tuple(found), errors=tuple(errors))
