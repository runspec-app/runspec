"""search.py — CQL content search + text→CQL and label-search helpers."""

from __future__ import annotations

from typing import Any

from runspec_confluence_core.client import _cap_limit, build_client, translate_errors
from runspec_confluence_core.config import ConfluenceConfig

# A query already contains CQL syntax if it uses any of these operators/tokens;
# otherwise it is treated as plain text and wrapped as a siteSearch. (Ported from
# the mcp-atlassian Confluence search tool's heuristic.)
_CQL_OPERATORS = ("=", "~", ">", "<", " AND ", " OR ", "currentUser()")


def cql_from_text(query: str, *, space_key: str | None = None, content_type: str | None = None) -> str:
    """Turn a plain-text query into CQL, or pass an already-CQL query through.

    A query containing no CQL operator is wrapped as ``siteSearch ~ "query"``; a
    query that already looks like CQL is used verbatim. ``space_key`` /
    ``content_type`` append ``AND space = "..."`` / ``AND type = "..."`` clauses.
    """
    cql = query if any(op in query for op in _CQL_OPERATORS) else f'siteSearch ~ "{_escape(query)}"'
    if content_type:
        cql += f' AND type = "{_escape(content_type)}"'
    if space_key:
        cql += f' AND space = "{_escape(space_key)}"'
    return cql


def _escape(value: str) -> str:
    """Backslash-escape double quotes so a value is safe inside a CQL string."""
    return value.replace('"', '\\"')


@translate_errors
def search(config: ConfluenceConfig, cql: str, *, start: int = 0, limit: int = 25, expand: str | None = None) -> Any:
    """Search content with CQL (Confluence Query Language), e.g.
    ``type = page AND space = DOCS AND text ~ "onboarding"``. Returns one page."""
    return build_client(config).cql(cql, start=start, limit=_cap_limit(limit), expand=expand)


@translate_errors
def search_by_label(config: ConfluenceConfig, label: str, *, space_key: str | None = None, start: int = 0, limit: int = 25) -> Any:
    """Find content tagged with ``label`` (optionally restricted to ``space_key``) —
    one page of results."""
    cql = f'label = "{_escape(label)}"'
    if space_key:
        cql += f' AND space = "{_escape(space_key)}"'
    return build_client(config).cql(cql, start=start, limit=_cap_limit(limit))
