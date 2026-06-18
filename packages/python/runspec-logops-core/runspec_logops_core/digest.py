"""Condense a noisy log into a small, bounded digest.

``summarize_log`` streams a file line-by-line (it never loads the whole file),
groups continuation/stack lines into events, clusters events by
:func:`signatures.error_signature`, and returns only the top-N distinct
signatures with counts, first/last timestamp and one truncated sample each. The
returned dict's size is bounded by ``top`` × ``max_sample_lines`` × ``max_bytes``
— independent of how big the input log is. That bound is the whole point: the
agent reads the digest, never the log.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

from runspec_logops_core.errors import SourceNotFoundError
from runspec_logops_core.signatures import detect_level, error_signature

_LEVEL_FLOOR = {"all": 0, "warning": 2, "error": 3, "critical": 4}

# Leading ISO timestamp captured for window filtering (syslog has no year, so it
# is intentionally left for the no-op path).
_ISO_TS = re.compile(r"^\s*\[?(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:[.,]\d+)?")
_REL = re.compile(r"^\s*(\d+)\s*([a-z]+?)s?(?:\s+ago)?\s*$", re.IGNORECASE)
_UNIT = {"s": "seconds", "sec": "seconds", "second": "seconds", "m": "minutes", "min": "minutes", "minute": "minutes", "h": "hours", "hour": "hours", "hr": "hours", "d": "days", "day": "days"}

# A line that continues the previous event rather than starting a new one.
_CONT = re.compile(r"^\s+|^(?:at\s|Caused by:|\.{3}|File\s\")", re.IGNORECASE)


def _parse_dt(text: str) -> datetime | None:
    """Best-effort parse of an ISO-ish ``YYYY-MM-DD[ T]HH:MM:SS`` string (naive)."""
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


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

        if (since_dt and ts and ts < since_dt) or (until_dt and ts and ts > until_dt):
            continue

        sig = error_signature(block)
        # Event level = highest severity seen on any line; an exception-bearing
        # event with no explicit level counts as "error".
        ranks = [_LEVEL_FLOOR.get(detect_level(ln) or "", 0) for ln in block]
        rank = max(ranks) if ranks else 0
        if rank == 0 and "@" in sig:
            rank = 3
        if rank < floor:
            continue

        total_events += 1
        g = groups.get(sig)
        if g is None:
            sample = [ln[:max_bytes] for ln in block[:max_sample_lines]]
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
