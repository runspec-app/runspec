"""Session runnables: who, current-user."""

from __future__ import annotations

import getpass
import json
import os
import platform

import runspec as rs

from runspec_windows import _platform

# Column labels emitted by `query user`, in order.
_QUSER_COLUMNS = ["USERNAME", "SESSIONNAME", "ID", "STATE", "IDLE TIME", "LOGON TIME"]


def parse_query_user(text: str) -> list[dict]:
    """Parse ``query user`` output using the header to locate column boundaries.

    The leading ``>`` on a row marks the current session; fields such as
    "LOGON TIME" contain spaces, so columns are sliced by header position rather
    than split on whitespace.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header_idx = next((i for i, ln in enumerate(lines) if "USERNAME" in ln.upper()), None)
    if header_idx is None:
        return []
    header = lines[header_idx]
    upper = header.upper()
    starts = []
    for col in _QUSER_COLUMNS:
        pos = upper.find(col)
        starts.append(pos if pos != -1 else None)
    # Build (start, end) spans from the columns that are actually present.
    present = [(col, s) for col, s in zip(_QUSER_COLUMNS, starts, strict=True) if s is not None]
    spans = []
    for j, (col, start) in enumerate(present):
        end = present[j + 1][1] if j + 1 < len(present) else None
        spans.append((col, start, end))

    rows = []
    for line in lines[header_idx + 1 :]:
        # The leading '>' marks the current session; it occupies the same column
        # as the leading space, so slice the raw line to keep header alignment.
        row: dict[str, object] = {"current": line.startswith(">")}
        for col, start, end in spans:
            value = line[start:end] if end is not None else line[start:]
            row[col.lower().replace(" ", "_")] = value.strip()
        if row.get("username"):
            rows.append(row)
    return rows


def main_who() -> None:
    rs.parse("who")
    _platform.ensure_windows()
    try:
        result = _platform.run_cmd(["query", "user"])
        if result.returncode != 0 and not result.stdout.strip():
            # `query user` exits non-zero ("No User exists") when no one is logged on.
            print(json.dumps([]))
            return
        print(json.dumps(parse_query_user(result.stdout)))
    except Exception as e:
        _platform.fail(str(e))


def main_current_user() -> None:
    rs.parse("current-user")
    _platform.ensure_windows()
    try:
        print(
            json.dumps(
                {
                    "user": getpass.getuser(),
                    "domain": os.environ.get("USERDOMAIN"),
                    "computer": platform.node(),
                }
            )
        )
    except Exception as e:
        _platform.fail(str(e))
