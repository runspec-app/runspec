"""Event log runnables: query-eventlog, recent-errors."""

from __future__ import annotations

import json

import runspec as rs

from runspec_windows import _platform

# Get-WinEvent numeric levels.
_LEVELS = {"critical": 1, "error": 2, "warning": 3, "information": 4}


def normalize_events(data: object) -> list[dict]:
    """Reshape parsed ``Get-WinEvent | ConvertTo-Json`` output into rows."""
    if isinstance(data, dict):
        items = [data]
    elif isinstance(data, list):
        items = [d for d in data if isinstance(d, dict)]
    else:
        items = []
    rows = []
    for item in items:
        message = item.get("message") or ""
        if isinstance(message, str) and len(message) > 500:
            message = message[:500] + "…"
        rows.append(
            {
                "time": item.get("time"),
                "level": item.get("level"),
                "id": item.get("Id"),
                "provider": item.get("ProviderName"),
                "message": message,
            }
        )
    return rows


def _select() -> str:
    return "Select-Object @{N='time';E={$_.TimeCreated.ToString('s')}},@{N='level';E={$_.LevelDisplayName}},Id,ProviderName,@{N='message';E={$_.Message}}"


def _query(log: str, count: int, level: str | None) -> list[dict]:
    filt = f"LogName='{log}'"
    if level and level != "all":
        filt += f"; Level={_LEVELS[level]}"
    script = f"Get-WinEvent -FilterHashtable @{{{filt}}} -MaxEvents {count} -ErrorAction Stop | {_select()} | ConvertTo-Json -Compress"
    out = _platform.run_powershell(script).strip()
    return normalize_events(json.loads(out)) if out else []


def main_query_eventlog() -> None:
    spec = rs.parse("query-eventlog")
    _platform.ensure_windows()
    log = str(spec.log)
    level = str(spec.level)
    count = int(spec.count)
    try:
        print(json.dumps({"log": log, "level": level, "events": _query(log, count, level)}))
    except RuntimeError as e:
        # Get-WinEvent raises "No events were found" when the filter matches nothing.
        if "No events were found" in str(e):
            print(json.dumps({"log": log, "level": level, "events": []}))
        else:
            _platform.fail(str(e), log=log)
    except Exception as e:
        _platform.fail(str(e), log=log)


def main_recent_errors() -> None:
    spec = rs.parse("recent-errors")
    _platform.ensure_windows()
    count = int(spec.count)
    result: dict[str, list[dict]] = {}
    try:
        for log in ("System", "Application"):
            try:
                result[log] = _query(log, count, "error")
            except RuntimeError as e:
                if "No events were found" in str(e):
                    result[log] = []
                else:
                    raise
        print(json.dumps(result))
    except Exception as e:
        _platform.fail(str(e))
