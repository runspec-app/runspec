"""Condense a noisy log into a small, bounded digest.

``summarize_log`` streams a file line-by-line (it never loads the whole file),
groups continuation/stack lines into events, clusters events by signature, and
returns only the top-N distinct signatures with counts, first/last timestamp and
one truncated sample each. The returned dict's size is bounded by ``top`` ×
``max_sample_lines`` × ``max_bytes`` — independent of how big the input log is.
That bound is the whole point: the agent reads the digest, never the log.

**Structured (JSON-lines) logs** — one JSON object per line, as runspec itself and
many apps emit — are understood natively: the timestamp comes from the ``ts``
field, the level from ``level``, the signature from the embedded exception
(``exc``/``traceback``) or else the ``message`` (so lines cluster by what they say,
not by their JSON shape), and the sample is a readable ``ts level logger: message``.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta

from runspec_logops_core.errors import SourceNotFoundError
from runspec_logops_core.signatures import detect_level, error_signature, normalize_line

_LEVEL_FLOOR = {"all": 0, "warning": 2, "error": 3, "critical": 4}

# Leading ISO timestamp captured for window filtering (syslog has no year, so it
# is intentionally left for the no-op path).
_ISO_TS = re.compile(r"^\s*\[?(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:[.,]\d+)?")
_REL = re.compile(r"^\s*(\d+)\s*([a-z]+?)s?(?:\s+ago)?\s*$", re.IGNORECASE)
_UNIT = {"s": "seconds", "sec": "seconds", "second": "seconds", "m": "minutes", "min": "minutes", "minute": "minutes", "h": "hours", "hour": "hours", "hr": "hours", "d": "days", "day": "days"}

# A line that continues the previous event rather than starting a new one.
_CONT = re.compile(r"^\s+|^(?:at\s|Caused by:|\.{3}|File\s\")", re.IGNORECASE)

# Field names structured loggers use, in priority order.
_TS_KEYS = ("ts", "timestamp", "time", "asctime", "@timestamp", "datetime")
_MSG_KEYS = ("message", "msg", "event", "log")
_EXC_KEYS = ("exc", "exception", "traceback", "stack", "stack_trace", "stacktrace", "exc_info", "error")
_LEVEL_KEYS = ("level", "levelname", "severity", "lvl", "loglevel")
_LOGGER_KEYS = ("logger", "name", "module", "logger_name")
_LEVEL_ALIAS = {"debug": 0, "trace": 0, "info": 1, "notice": 1, "warn": 2, "warning": 2, "error": 3, "err": 3, "severe": 3, "critical": 4, "crit": 4, "fatal": 4, "emerg": 4}
_EXC_HEAD = re.compile(r"(?:Error|Exception|Failure|Fault)$")


def _parse_dt(text: str) -> datetime | None:
    """Best-effort parse of an ISO-ish ``YYYY-MM-DD[ T]HH:MM:SS`` string (naive)."""
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _parse_dt_loose(value: object) -> datetime | None:
    """Parse a timestamp from a structured field — ISO with optional fractional
    seconds / ``Z`` / offset — always returning a *naive* datetime so it compares
    cleanly with the (naive) window bounds."""
    if value is None:
        return None
    t = str(value).strip()
    if not t:
        return None
    dt = _parse_dt(t.replace("Z", "+00:00"))
    if dt is None:
        dt = _parse_dt(t[:19].replace("T", " "))  # drop fractional/offset and retry
    return dt.replace(tzinfo=None) if dt else None


def _parse_line_ts(line: str) -> datetime | None:
    """Extract a leading ISO timestamp from a log line, or None."""
    m = _ISO_TS.match(line)
    if not m:
        return None
    return _parse_dt(f"{m.group(1)} {m.group(2)}")


def _parse_when(value: str | None) -> datetime | None:
    """Parse a user-supplied window bound: ISO, ``now``, or ``<n> <unit> ago``."""
    if not value:
        return None
    v = value.strip()
    if v.lower() == "now":
        return datetime.now()
    rel = _REL.match(v)
    if rel:
        unit = _UNIT.get(rel.group(2).lower())
        if unit:
            return datetime.now() - timedelta(**{unit: int(rel.group(1))})
    iso = _parse_dt(v.replace("Z", "").replace("T", " ").strip())
    return iso


def _is_continuation(line: str) -> bool:
    return bool(line) and bool(_CONT.match(line))


def _structured(line: str) -> dict | None:
    """Parse a single JSON-object log line, or None if it isn't one."""
    s = line.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return None
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _first(obj: dict, keys: tuple[str, ...]) -> object:
    for k in keys:
        v = obj.get(k)
        if v not in (None, ""):
            return v
    return None


def _structured_event(obj: dict, max_bytes: int) -> tuple[str | None, int, datetime | None, str]:
    """Derive (signature, level_rank, ts, sample) from a structured log record.

    Signature: the embedded exception (so it clusters by crash and is resolvable
    by ``map-trace``), else the normalised ``message`` (so it clusters by what the
    line says, not by its JSON shape). Sample: a readable one-liner."""
    ts = _parse_dt_loose(_first(obj, _TS_KEYS))
    lvl = _first(obj, _LEVEL_KEYS)
    rank = _LEVEL_ALIAS.get(str(lvl).strip().lower(), 0) if lvl is not None else 0
    msg = str(_first(obj, _MSG_KEYS) or "")
    exc = _first(obj, _EXC_KEYS)

    sig: str | None = None
    if exc:
        exc_lines = str(exc).splitlines()
        candidate = error_signature([msg, *exc_lines] if msg else exc_lines)
        head = candidate.split("@", 1)[0]
        if "@" in candidate or _EXC_HEAD.search(head):
            sig = candidate
            rank = max(rank, 3)  # a traceback is at least an error
    if sig is None and msg:
        sig = normalize_line(msg)[:200]

    logger = str(_first(obj, _LOGGER_KEYS) or "")
    prefix = " ".join(p for p in (str(obj.get(_TS_KEYS[0]) or ""), str(lvl or ""), logger) if p)
    sample = (f"{prefix}: {msg}" if prefix else msg).strip()[:max_bytes]
    return sig, rank, ts, sample


def _iter_events(path: str):
    """Yield ``(block_lines, first_ts)`` events, streaming the file.

    An event is a leading (non-indented) line plus the indented / stack-frame
    lines that follow it. ``lines_scanned`` is tracked by the caller.
    """
    block: list[str] = []
    block_ts: datetime | None = None
    with open(path, errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if block and _is_continuation(line):
                block.append(line)
                continue
            if block:
                yield block, block_ts
            block = [line]
            block_ts = _parse_line_ts(line)
    if block:
        yield block, block_ts


def summarize_log(
    path: str,
    *,
    level: str | None = None,
    since: str | None = None,
    until: str | None = None,
    top: int = 10,
    max_sample_lines: int = 20,
    max_bytes: int = 200,
) -> dict:
    """Return a bounded digest of ``path`` clustered by error signature.

    Raises :class:`SourceNotFoundError` if the file does not exist; propagates
    :class:`OSError` on other read failures.
    """
    if not os.path.exists(path):
        raise SourceNotFoundError(f"log file not found: {path}")

    floor = _LEVEL_FLOOR.get((level or "all").lower(), 0)
    since_dt, until_dt = _parse_when(since), _parse_when(until)

    groups: dict[str, dict] = {}
    lines_scanned = 0
    total_events = 0

    for block, ts in _iter_events(path):
        lines_scanned += len(block)

        obj = _structured(block[0]) if len(block) == 1 else None
        if obj is not None:
            sig, rank, sts, sample_text = _structured_event(obj, max_bytes)
            if sig is None:
                sig = error_signature(block)
            if sts is not None:
                ts = sts
            sample = [sample_text]
        else:
            sig = error_signature(block)
            # Event level = highest severity seen on any line; an exception-bearing
            # event with no explicit level counts as "error".
            ranks = [_LEVEL_FLOOR.get(detect_level(ln) or "", 0) for ln in block]
            rank = max(ranks) if ranks else 0
            if rank == 0 and "@" in sig:
                rank = 3
            sample = [ln[:max_bytes] for ln in block[:max_sample_lines]]

        if (since_dt and ts and ts < since_dt) or (until_dt and ts and ts > until_dt):
            continue
        if rank < floor:
            continue

        total_events += 1
        g = groups.get(sig)
        if g is None:
            groups[sig] = {"signature": sig, "level_rank": rank, "count": 1, "first_ts": ts, "last_ts": ts, "sample": sample}
        else:
            g["count"] += 1
            g["level_rank"] = max(g["level_rank"], rank)
            if ts:
                if g["first_ts"] is None or ts < g["first_ts"]:
                    g["first_ts"] = ts
                if g["last_ts"] is None or ts > g["last_ts"]:
                    g["last_ts"] = ts

    ranked = sorted(groups.values(), key=lambda g: g["count"], reverse=True)
    rank_name = {0: "info", 2: "warning", 3: "error", 4: "critical"}
    top_list = [
        {
            "signature": g["signature"],
            "level": rank_name.get(g["level_rank"], "info"),
            "count": g["count"],
            "first_ts": g["first_ts"].isoformat() if g["first_ts"] else None,
            "last_ts": g["last_ts"].isoformat() if g["last_ts"] else None,
            "sample": g["sample"],
        }
        for g in ranked[:top]
    ]

    return {
        "file": path,
        "window": {"since": since, "until": until, "lines_scanned": lines_scanned},
        "total_events": total_events,
        "distinct_signatures": len(groups),
        "truncated": len(groups) > top,
        "top": top_list,
    }
