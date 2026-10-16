"""render.py — readable text for a person at a terminal.

``jira`` prints JSON to agents, pipes, the console's Forms tab and schedulers.
When a person runs it in an interactive terminal, the compact views read better
as text, so :func:`human_text` returns that for the verbs that have one (and
``None`` for the rest, which stay JSON).
"""

from __future__ import annotations

from typing import Any

_ISSUE_LINES = (
    ("Status", "status"),
    ("Type", "type"),
    ("Priority", "priority"),
    ("Assignee", "assignee"),
    ("Reporter", "reporter"),
    ("Resolution", "resolution"),
    ("Created", "created"),
    ("Updated", "updated"),
    ("Parent", "parent"),
    ("Labels", "labels"),
    ("Components", "components"),
    ("Fix versions", "fix_versions"),
    ("URL", "url"),
)


def _plain(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(str(v) for v in value.values())
    if isinstance(value, list):
        return ", ".join(_plain(v) for v in value)
    return str(value)


def _issue(view: dict[str, Any]) -> str:
    lines = [f"{view.get('key', '')}  {view.get('summary', '')}", ""]
    width = max(len(label) for label, _ in _ISSUE_LINES)
    for label, key in _ISSUE_LINES:
        if key in view:
            lines.append(f"{label:<{width}}  {_plain(view[key])}")
    counts = [
        f"{view[k]} {name}"
        for k, name in (
            ("comment_count", "comments"),
            ("link_count", "links"),
            ("subtask_count", "subtasks"),
            ("attachment_count", "attachments"),
        )
        if view.get(k)
    ]
    if counts:
        lines.append(f"{'Has':<{width}}  {', '.join(counts)}")
    if view.get("description"):
        lines += ["", view["description"]]
        if view.get("description_chars"):
            lines.append(f"(preview; {view['description_chars']} chars in full: --include description)")
    for c in view.get("comments") or []:
        lines += ["", f"— {c.get('author', '?')}, {c.get('created', '')}", c.get("body", "")]
    for title, key in (("Links", "links"), ("Subtasks", "subtasks")):
        if view.get(key):
            lines += ["", f"{title}:"]
            lines += [f"  {_plain(row)}" for row in view[key]]
    if view.get("attachments"):
        lines += ["", "Attachments:"]
        lines += [f"  {a.get('filename')} ({a.get('size', '?')} bytes)" for a in view["attachments"]]
    if view.get("changelog"):
        lines += ["", "Changelog:"]
        lines += [f"  {h.get('when', '')} {h.get('who', '')}: {h.get('field')} {h.get('from', '')} → {h.get('to', '')}" for h in view["changelog"]]
    if view.get("custom"):
        lines += ["", "Custom fields:"]
        lines += [f"  {name}: {_plain(value)}" for name, value in view["custom"].items()]
    return "\n".join(lines)


def _rows(page: dict[str, Any]) -> str:
    rows = page.get("issues") or []
    if not rows:
        return "No issues."
    cols = ("key", "status", "assignee", "summary")
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols[:-1]}
    head = "  ".join(f"{c.upper():<{widths[c]}}" for c in cols[:-1]) + "  SUMMARY"
    body = ["  ".join(f"{str(r.get(c, '')):<{widths[c]}}" for c in cols[:-1]) + f"  {r.get('summary', '')}" for r in rows]
    footer = f"{page.get('count', len(rows))} of {page.get('total', '?')}"
    if page.get("truncated"):
        footer += f" — next page: --start {page.get('next_start')}"
    return "\n".join([head, *body, "", footer])


def human_text(command: str, payload: Any, *, raw: bool) -> str | None:
    """Text for a terminal, or ``None`` to print JSON instead."""
    if raw or not isinstance(payload, dict):
        return None
    if command == "get-issue":
        return _issue(payload)
    if command in ("search", "get-project-issues"):
        return _rows(payload)
    return None
