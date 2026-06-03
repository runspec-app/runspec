"""Process runnables: list-processes, process-info, kill-process."""

from __future__ import annotations

import csv
import io
import json

import runspec as rs

from runspec_windows import _platform


def _mem_to_kb(raw: str) -> int | None:
    """Parse a tasklist memory column like ``"123,456 K"`` to integer kilobytes."""
    cleaned = raw.replace(",", "").replace("K", "").replace("\xa0", " ").strip()
    try:
        return int(cleaned)
    except ValueError:
        return None


def parse_tasklist_csv(text: str) -> list[dict]:
    """Parse ``tasklist /fo csv /nh`` output into structured rows.

    Columns (no header): Image Name, PID, Session Name, Session#, Mem Usage.
    """
    rows: list[dict] = []
    for fields in csv.reader(io.StringIO(text)):
        if len(fields) < 5:
            continue
        try:
            pid = int(fields[1])
        except ValueError:
            continue
        rows.append(
            {
                "name": fields[0],
                "pid": pid,
                "session": fields[2],
                "mem_kb": _mem_to_kb(fields[4]),
            }
        )
    return rows


def main_list_processes() -> None:
    spec = rs.parse("list-processes")
    _platform.ensure_windows()
    name_filter = str(spec.filter).lower() if spec.filter is not None else None
    limit = int(spec.limit)
    try:
        result = _platform.run_cmd(["tasklist", "/fo", "csv", "/nh"])
        rows = parse_tasklist_csv(result.stdout)
        if name_filter:
            rows = [r for r in rows if name_filter in r["name"].lower()]
        rows.sort(key=lambda r: r["mem_kb"] or 0, reverse=True)
        print(json.dumps(rows[:limit]))
    except Exception as e:
        _platform.fail(str(e))


def main_process_info() -> None:
    spec = rs.parse("process-info")
    _platform.ensure_windows()
    pid = int(spec.pid)
    script = (
        f"Get-Process -Id {pid} -ErrorAction Stop | "
        "Select-Object Id,ProcessName,"
        "@{N='cpu_seconds';E={[math]::Round($_.CPU,2)}},"
        "@{N='working_set_mb';E={[math]::Round($_.WorkingSet64/1MB)}},"
        "@{N='path';E={$_.Path}},"
        "@{N='start_time';E={if($_.StartTime){$_.StartTime.ToString('s')}else{$null}}} "
        "| ConvertTo-Json -Compress"
    )
    try:
        out = _platform.run_powershell(script).strip()
        print(json.dumps(json.loads(out)) if out else json.dumps({"error": f"No process with PID {pid}", "pid": pid}))
    except RuntimeError as e:
        _platform.fail(f"No process with PID {pid}", pid=pid, detail=str(e))
    except Exception as e:
        _platform.fail(str(e), pid=pid)


def main_kill_process() -> None:
    spec = rs.parse("kill-process")
    _platform.ensure_windows()
    pid = int(spec.pid)
    try:
        result = _platform.run_cmd(["taskkill", "/PID", str(pid), "/F"])
        if result.returncode != 0:
            _platform.fail(result.stderr.strip() or result.stdout.strip() or f"Failed to kill PID {pid}", pid=pid)
        print(json.dumps({"pid": pid, "killed": True}))
    except Exception as e:
        _platform.fail(str(e), pid=pid)
