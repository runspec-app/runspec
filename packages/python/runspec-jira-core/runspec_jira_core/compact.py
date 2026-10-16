"""compact.py — small, agent-sized views of issues and search results.

A raw Jira issue is mostly empty custom fields, avatar URLs and ``self`` links:
15–30 KB for one ticket, most of it noise. An agent reads every byte of a tool
result and sends it again on each later step of its conversation, so the read
verbs return a compact view by default and add the bulky parts only when asked
(``include``). The raw REST response is still one flag away for a caller that
needs it.

These are pure functions over the JSON the API returns — no client calls — so a
private wrapper can reuse them on its own fetches.
"""

from __future__ import annotations

from typing import Any

from runspec_jira_core.client import build_client, translate_errors
from runspec_jira_core.config import JiraConfig

# Parts of an issue a caller can add to the compact view.
INCLUDE_OPTIONS = ("description", "comments", "links", "subtasks", "attachments", "changelog", "custom")

DESCRIPTION_CHARS = 600  # description preview length unless include=description
COMMENT_LIMIT = 20  # newest comments kept with include=comments
COMMENT_CHARS = 1000  # each comment body is cut here

# Fields always fetched for the compact issue view (counts need the list fields).
_ISSUE_FIELDS = (
    "summary",
    "status",
    "issuetype",
    "priority",
    "assignee",
    "reporter",
    "created",
    "updated",
    "resolution",
    "labels",
    "components",
    "fixVersions",
    "parent",
    "description",
    "comment",
    "issuelinks",
    "subtasks",
    "attachment",
)

# Fields fetched for one row of a compact search result.
SEARCH_FIELDS = ("summary", "status", "assignee", "priority", "issuetype", "updated")


def _name(value: Any, key: str = "name") -> Any:
    return value.get(key) if isinstance(value, dict) else value


def _person(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return value.get("displayName") or value.get("name") or value.get("accountId")


def _adf_text(node: Any) -> str:
    """Flatten an Atlassian Document Format body (Cloud API v3) to plain text."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return str(node.get("text", ""))
    inner = _adf_text(node.get("content") or [])
    block = node.get("type") in ("paragraph", "heading", "listItem", "codeBlock", "blockquote")
    return inner + ("\n" if block else "")


def _text(value: Any) -> str:
    return _adf_text(value).strip() if value else ""


def _cut(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + " …", True


def _drop_empty(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v not in (None, "", [], {})}


def _linked(issue: Any) -> dict[str, Any]:
    fields = issue.get("fields") or {} if isinstance(issue, dict) else {}
    return _drop_empty(
        {
            "key": issue.get("key") if isinstance(issue, dict) else None,
            "summary": fields.get("summary"),
            "status": _name(fields.get("status")),
        }
    )


def compact_issue(
    raw: dict[str, Any],
    *,
    include: list[str] | tuple[str, ...] = (),
    base_url: str = "",
) -> dict[str, Any]:
    """The compact view of one issue from its raw REST JSON.

    Always: key, url, summary, status, type, priority, people, dates, labels,
    components, fix versions, parent, a description preview, and counts of the
    comments / links / subtasks / attachments. Each name in ``include`` adds that
    part (see :data:`INCLUDE_OPTIONS`), present even when empty. Other empty
    values are dropped."""
    wanted = set(include)
    fields = raw.get("fields") or {}
    key = raw.get("key")
    status = fields.get("status") or {}
    out: dict[str, Any] = {
        "key": key,
        "url": f"{base_url.rstrip('/')}/browse/{key}" if base_url and key else None,
        "summary": fields.get("summary"),
        "status": _name(status),
        "status_category": _name((status or {}).get("statusCategory")) if isinstance(status, dict) else None,
        "type": _name(fields.get("issuetype")),
        "priority": _name(fields.get("priority")),
        "assignee": _person(fields.get("assignee")),
        "reporter": _person(fields.get("reporter")),
        "resolution": _name(fields.get("resolution")),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "labels": fields.get("labels") or [],
        "components": [_name(c) for c in fields.get("components") or []],
        "fix_versions": [_name(v) for v in fields.get("fixVersions") or []],
        "parent": _linked(fields.get("parent")) if fields.get("parent") else None,
    }

    description = _text(fields.get("description"))
    if description:
        if "description" in wanted:
            out["description"] = description
        else:
            preview, cut = _cut(description, DESCRIPTION_CHARS)
            out["description"] = preview
            if cut:
                out["description_chars"] = len(description)

    comment_block = fields.get("comment") or {}
    comments = comment_block.get("comments") or [] if isinstance(comment_block, dict) else []
    links = fields.get("issuelinks") or []
    subtasks = fields.get("subtasks") or []
    attachments = fields.get("attachment") or []
    # Counts stay even at zero, so a caller knows whether an include is worth it.
    counts = {
        "comment_count": comment_block.get("total", len(comments)) if isinstance(comment_block, dict) else 0,
        "link_count": len(links),
        "subtask_count": len(subtasks),
        "attachment_count": len(attachments),
    }
    out = _drop_empty(out) | counts

    if "comments" in wanted:
        shown = comments[-COMMENT_LIMIT:]
        rows = []
        for c in shown:
            body, cut = _cut(_text(c.get("body")), COMMENT_CHARS)
            rows.append(
                _drop_empty(
                    {
                        "id": c.get("id"),
                        "author": _person(c.get("author")),
                        "created": c.get("created"),
                        "body": body,
                        "body_chars": len(_text(c.get("body"))) if cut else None,
                    }
                )
            )
        out["comments"] = rows
        if len(comments) > len(shown):
            out["comments_shown"] = len(shown)

    if "links" in wanted:
        rows = []
        for link in links:
            kind = link.get("type") or {}
            if "outwardIssue" in link:
                rows.append({"relation": kind.get("outward"), **_linked(link["outwardIssue"])})
            elif "inwardIssue" in link:
                rows.append({"relation": kind.get("inward"), **_linked(link["inwardIssue"])})
        out["links"] = rows

    if "subtasks" in wanted:
        out["subtasks"] = [_linked(s) for s in subtasks]

    if "attachments" in wanted:
        out["attachments"] = [
            _drop_empty(
                {
                    "id": a.get("id"),
                    "filename": a.get("filename"),
                    "size": a.get("size"),
                    "created": a.get("created"),
                    "author": _person(a.get("author")),
                }
            )
            for a in attachments
        ]

    if "changelog" in wanted:
        history = []
        for h in (raw.get("changelog") or {}).get("histories") or []:
            for item in h.get("items") or []:
                history.append(
                    _drop_empty(
                        {
                            "when": h.get("created"),
                            "who": _person(h.get("author")),
                            "field": item.get("field"),
                            "from": item.get("fromString"),
                            "to": item.get("toString"),
                        }
                    )
                )
        out["changelog"] = history

    if "custom" in wanted:
        names = raw.get("names") or {}
        custom = {}
        for fid, value in fields.items():
            if not fid.startswith("customfield_") or value in (None, "", [], {}):
                continue
            if isinstance(value, dict):
                value = value.get("value") or value.get("name") or _person(value) or value
            elif isinstance(value, list):
                value = [(v.get("value") or v.get("name") or _person(v)) if isinstance(v, dict) else v for v in value]
            custom[names.get(fid, fid)] = value
        out["custom"] = custom

    return out


def compact_search(raw: dict[str, Any]) -> dict[str, Any]:
    """The compact view of one page of a JQL search: a row per issue plus the
    paging facts (``total``, ``truncated``, ``next_start``)."""
    issues = raw.get("issues") or []
    start = int(raw.get("startAt") or 0)
    total = raw.get("total")
    rows = []
    for issue in issues:
        fields = issue.get("fields") or {}
        rows.append(
            _drop_empty(
                {
                    "key": issue.get("key"),
                    "summary": fields.get("summary"),
                    "status": _name(fields.get("status")),
                    "type": _name(fields.get("issuetype")),
                    "priority": _name(fields.get("priority")),
                    "assignee": _person(fields.get("assignee")),
                    "updated": fields.get("updated"),
                }
            )
        )
    out: dict[str, Any] = {"total": total, "start": start, "count": len(rows), "issues": rows}
    more = total is not None and start + len(rows) < int(total)
    out["truncated"] = more
    if more:
        out["next_start"] = start + len(rows)
    return out


@translate_errors
def get_issue_compact(
    config: JiraConfig,
    key: str,
    *,
    include: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Fetch one issue and return :func:`compact_issue` of it, asking the API
    only for the fields the view (plus ``include``) needs."""
    wanted = set(include)
    unknown = wanted - set(INCLUDE_OPTIONS)
    if unknown:
        raise ValueError(f"unknown include: {', '.join(sorted(unknown))} (choose from {', '.join(INCLUDE_OPTIONS)})")
    fields = "*all" if "custom" in wanted else ",".join(_ISSUE_FIELDS)
    expand = ",".join(e for e, part in (("changelog", "changelog"), ("names", "custom")) if part in wanted) or None
    raw = build_client(config).get_issue(key, fields=fields, expand=expand)
    return compact_issue(raw or {}, include=include, base_url=config.url)


@translate_errors
def search_compact(config: JiraConfig, jql: str, *, start: int = 0, limit: int = 25) -> dict[str, Any]:
    """Run a JQL search and return :func:`compact_search` of the page, fetching
    only the row fields."""
    raw = build_client(config).jql(jql, fields=",".join(SEARCH_FIELDS), start=start, limit=limit)
    return compact_search(raw or {})


def project_issues_compact(config: JiraConfig, project_key: str, *, start: int = 0, limit: int = 25) -> dict[str, Any]:
    """A project's issues, newest first, as :func:`compact_search` rows."""
    return search_compact(config, f'project = "{project_key}" ORDER BY created DESC', start=start, limit=limit)
