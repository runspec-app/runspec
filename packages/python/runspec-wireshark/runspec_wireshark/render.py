"""render.py — readable text for a person at a terminal.

``pcap`` prints JSON to agents, pipes, the console's Forms tab and schedulers.
When a person runs it in an interactive terminal the compact views read better
as text: scalar fields as ``Label  value`` lines, each list as a table, a
followed stream as its payload, and a decoded packet as an indented tree.
"""

from __future__ import annotations

from typing import Any

_MAX_CELL = 60
_SKIP = {"truncated", "next_offset", "count", "total", "offset"}


def _cell(value: Any, limit: int = _MAX_CELL) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "yes" if value else "no"
    elif isinstance(value, dict):
        text = ", ".join(f"{k}={_cell(v, 0)}" for k, v in value.items())
    elif isinstance(value, list):
        text = ", ".join(_cell(v, 0) for v in value)
    else:
        text = str(value)
    return text if not limit or len(text) <= limit else text[: limit - 1] + "…"


def _table(rows: list[dict[str, Any]]) -> list[str]:
    cols: list[str] = []
    for row in rows:
        for key in row:
            if key not in cols:
                cols.append(key)
    widths = {c: max(len(c), *(len(_cell(r.get(c))) for r in rows)) for c in cols}
    lines = ["  ".join(c.upper().ljust(widths[c]) for c in cols).rstrip()]
    lines += ["  ".join(_cell(r.get(c)).ljust(widths[c]) for c in cols).rstrip() for r in rows]
    return lines


def _label(key: str) -> str:
    return key.replace("_", " ").capitalize()


def _footer(payload: dict[str, Any]) -> list[str]:
    if not payload.get("truncated"):
        return []
    shown, total = payload.get("count"), payload.get("total")
    more = f" — next page: --offset {payload['next_offset']}" if "next_offset" in payload else " — raise --limit"
    return ["", f"Showing {shown} of {total}{more}"]


def _follow(payload: dict[str, Any]) -> str:
    lines = []
    if payload.get("a"):
        lines.append(f"{payload['protocol']} stream {payload['stream']}: {payload['a']} ⇄ {payload.get('b', '')}")
    for chunk in payload.get("chunks", []):
        lines += ["", f"── {chunk['from']} ({chunk['bytes']} bytes)", chunk["text"].replace("\r\n", "\n").rstrip("\n")]
    if payload.get("hint"):
        lines.append(payload["hint"])
    if payload.get("truncated"):
        lines += ["", f"(cut — {payload.get('bytes_total')} bytes in all; raise --max-bytes)"]
    return "\n".join(lines)


def _packet(payload: dict[str, Any]) -> str:
    lines = []
    for layer in payload.get("layers", []):
        lines.append(layer["title"])
        lines += [f"    {line}" for line in layer.get("lines", [])]
        if layer.get("more"):
            lines.append(f"    … {layer['more']} more lines")
    if payload.get("bytes"):
        lines += ["", *payload["bytes"]]
    return "\n".join(lines)


def _stats(payload: dict[str, Any]) -> str:
    return "\n".join([*payload.get("lines", []), *_footer(payload)])


def _generic(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    tables = {k: v for k, v in payload.items() if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)}
    empty = [k for k, v in payload.items() if isinstance(v, list) and not v]
    header = {k: v for k, v in payload.items() if k not in _SKIP and k not in tables and k not in empty}
    if header:
        width = max(len(_label(k)) for k in header)
        lines += [f"{_label(k):<{width}}  {_cell(v, 0) if not isinstance(v, str) else v}" for k, v in header.items()]
    for key, rows in tables.items():
        lines += ["", f"{_label(key)}:", *(f"  {line}" for line in _table(rows))]
    for key in empty:
        lines += ["", f"No {key.replace('_', ' ')}."]
    lines += _footer(payload)
    return "\n".join(lines).strip("\n")


def human_text(command: str, payload: Any) -> str | None:
    """Text for a terminal, or ``None`` to print JSON instead."""
    if not isinstance(payload, dict):
        return None
    if command == "follow":
        return _follow(payload)
    if command == "packet" and "layers" in payload:
        return _packet(payload)
    if command == "stats":
        return _stats(payload)
    return _generic(payload)
