"""render.py — readable text for a person at a terminal.

``grafana`` prints JSON to agents, pipes, the console's Forms tab and
schedulers. When a person runs it in an interactive terminal the compact views
read better as text: scalar fields as ``Label  value`` lines, the main list as
a table (or one string per line), and Loki log lines as ``time {labels} line``.
"""

from __future__ import annotations

from typing import Any

_MAX_CELL = 60


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        text = ", ".join(f"{k}={v}" for k, v in value.items())
    elif isinstance(value, list):
        text = ", ".join(_cell(v) for v in value)
    else:
        text = str(value)
    return text if len(text) <= _MAX_CELL else text[: _MAX_CELL - 1] + "…"


def _table(rows: list[dict[str, Any]]) -> list[str]:
    cols: list[str] = []
    for row in rows:
        for key, value in row.items():
            # Long nested parts (queries, alerts, values) stay out of the table.
            if key not in cols and not (isinstance(value, list) and value and isinstance(value[0], (dict, list))):
                cols.append(key)
    widths = {c: max(len(c), *(len(_cell(r.get(c))) for r in rows)) for c in cols}
    lines = ["  ".join(c.upper().ljust(widths[c]) for c in cols).rstrip()]
    lines += ["  ".join(_cell(r.get(c)).ljust(widths[c]) for c in cols).rstrip() for r in rows]
    return lines


def _logs(payload: dict[str, Any]) -> list[str]:
    streams = payload.get("streams") or []
    lines = []
    for entry in payload.get("entries") or []:
        labels = streams[entry["stream"]]["labels"] if entry.get("stream") is not None and entry["stream"] < len(streams) else {}
        label_text = "{" + ", ".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
        lines.append(f"{entry.get('time', '')} {label_text} {entry.get('line', '')}")
    return lines


def _is_rows(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


def human_text(payload: Any) -> str | None:
    """Text for a terminal, or ``None`` to print JSON instead."""
    if not isinstance(payload, dict):
        return None
    if "entries" in payload and "streams" in payload:
        body = _logs(payload)
        footer = f"{payload.get('line_count', len(body))} lines" + (" (more — raise --limit)" if payload.get("truncated") else "")
        return "\n".join([*body, "", footer]) if body else "No log lines."

    lists = {k: v for k, v in payload.items() if isinstance(v, list) and k not in ("tags", "orgs", "variables", "warnings", "infos")}
    main_key = next(iter(lists), None)
    header = {k: v for k, v in payload.items() if k not in lists and k != "truncated" and not _is_rows(v)}
    sub_tables = {k: v for k, v in payload.items() if k not in lists and _is_rows(v)}

    lines: list[str] = []
    if header:
        labels = {k: k.replace("_", " ").capitalize() for k in header}
        width = max(len(label) for label in labels.values())
        lines += [f"{labels[k]:<{width}}  {_cell(v) if not isinstance(v, str) else v}" for k, v in header.items()]
    for key, rows in sub_tables.items():
        lines += ["", f"{key.replace('_', ' ').capitalize()}:", *(f"  {line}" for line in _table(rows))]
    if main_key is not None:
        rows = lists[main_key]
        lines.append("")
        if not rows:
            lines.append(f"No {main_key.replace('_', ' ')}.")
        elif all(isinstance(r, dict) for r in rows):
            lines += _table(rows)
            # Nested row lists (a panel's queries, a rule's alerts) follow the table.
            for row in rows:
                for key, value in row.items():
                    if _is_rows(value):
                        name = row.get("title") or row.get("name") or row.get("id")
                        lines += ["", f"{name} — {key.replace('_', ' ')}:", *(f"  {line}" for line in _table(value))]
        else:
            lines += [_cell(r) for r in rows]
        if payload.get("truncated"):
            lines += ["", "(more — raise --limit)"]
    return "\n".join(lines).strip("\n")
