"""search.py — CQL content search + text→CQL and label-search helpers."""

from __future__ import annotations

from typing import Any

from runspec_confluence_core.client import _cap_limit, build_client, translate_errors
from runspec_confluence_core.config import ConfluenceConfig

# A query already contains CQL syntax if it uses any of these operators/tokens
# (the AND/OR tokens are space-padded, so plain English like "alerts and
# dashboards" stays plain text); otherwise it is wrapped as a free-text match.
# (Ported from the mcp-atlassian Confluence search tool's heuristic.)
_CQL_OPERATORS = ("=", "~", ">", "<", " AND ", " OR ", "currentUser()")


def cql_from_text(
    query: str,
    *,
    space_key: str | None = None,
    content_type: str | None = None,
    text_field: str = "text",
) -> str:
    """Turn a plain-text query into CQL, or pass an already-CQL query through.

    A query containing no CQL operator is wrapped as ``<text_field> ~ "query"``; a
    query that already looks like CQL is used verbatim. ``text_field`` defaults to
    ``"text"``, which works on **both** Cloud and Server/Data Center — pass
    ``"siteSearch"`` for the Cloud-only relevance-ranked field (Server/DC has no
    ``siteSearch`` field, so defaulting to it silently breaks plain-text search
    there). ``space_key`` / ``content_type`` append ``AND space = "..."`` /
    ``AND type = "..."`` clauses.

    A passed-through CQL query is **parenthesised** before clauses are appended,
    so an ``OR`` in the user's query is not re-scoped by the higher-binding
    ``AND`` (``a OR b`` + ``type = page`` becomes ``(a OR b) AND type = "page"``,
    not ``a OR (b AND type = "page")``).
    """
    passthrough = any(op in query for op in _CQL_OPERATORS)
    base = query if passthrough else f'{text_field} ~ "{_escape(query)}"'
    clauses = []
    if content_type:
        clauses.append(f'type = "{_escape(content_type)}"')
    if space_key:
        clauses.append(f'space = "{_escape(space_key)}"')
    if clauses and passthrough:
        base = f"({base})"
    return " AND ".join([base, *clauses])


def _escape(value: str) -> str:
    """Backslash-escape double quotes so a value is safe inside a CQL string."""
    return value.replace('"', '\\"')


@translate_errors
def search(config: ConfluenceConfig, cql: str, *, start: int = 0, limit: int = 25, expand: str | None = None) -> Any:
    """Search content with CQL (Confluence Query Language), e.g.
    ``type = page AND space = DOCS AND text ~ "onboarding"``. Returns one page."""
    return build_client(config).cql(cql, start=start, limit=_cap_limit(limit), expand=expand)


@translate_errors
def search_by_label(config: ConfluenceConfig, label: str, *, space_key: str | None = None, start: int = 0, limit: int = 25, expand: str | None = None) -> Any:
    """Find content tagged with ``label`` (optionally restricted to ``space_key``) —
    one page of results. ``expand`` requests extra sections (e.g. ``body.view``)."""
    cql = f'label = "{_escape(label)}"'
    if space_key:
        cql += f' AND space = "{_escape(space_key)}"'
    return build_client(config).cql(cql, start=start, limit=_cap_limit(limit), expand=expand)
