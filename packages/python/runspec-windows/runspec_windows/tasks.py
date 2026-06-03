"""Scheduled task runnable: list-scheduled-tasks."""

from __future__ import annotations

import csv
import io
import json

import runspec as rs

from runspec_windows import _platform


def parse_schtasks_csv(text: str, name_filter: str | None = None) -> list[dict]:
    """Parse ``schtasks /query /fo csv /v`` output into structured rows.

    The first row is a header; ``schtasks`` repeats the header between task
    folders, so rows equal to the header are skipped.
    """
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []
    header = rows[0]
    idx = {name: i for i, name in enumerate(header)}

    def col(row: list[str], name: str) -> str | None:
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else None

    out: list[dict] = []
    for row in rows[1:]:
        if not row or row == header or row[0] == header[0]:
            continue
        name = col(row, "TaskName")
        if name is None:
            continue
        if name_filter and name_filter.lower() not in name.lower():
            continue
        out.append(
            {
                "name": name,
                "status": col(row, "Status"),
                "next_run": col(row, "Next Run Time"),
                "command": col(row, "Task To Run"),
            }
        )
    return out


def main_list_scheduled_tasks() -> None:
    spec = rs.parse("list-scheduled-tasks")
    _platform.ensure_windows()
    name_filter = str(spec.filter) if spec.filter is not None else None
    try:
        result = _platform.run_cmd(["schtasks", "/query", "/fo", "csv", "/v"])
        print(json.dumps(parse_schtasks_csv(result.stdout, name_filter)))
    except Exception as e:
        _platform.fail(str(e))
