"""search.py — JQL search and per-project issue listing."""

from __future__ import annotations

from typing import Any

from runspec_jira_core.client import build_client, translate_errors
from runspec_jira_core.config import JiraConfig


@translate_errors
def jql_search(
    config: JiraConfig,
    jql: str,
    *,
    fields: list[str] | None = None,
    start: int = 0,
    limit: int = 50,
    expand: str | None = None,
) -> Any:
    """Search issues with JQL. Returns one page (``startAt``/``maxResults``); the
    payload carries ``total`` for the caller to page further."""
    fields_arg = ",".join(fields) if fields else "*all"
    return build_client(config).jql(jql, fields=fields_arg, start=start, limit=limit, expand=expand)


@translate_errors
def get_project_issues(config: JiraConfig, project_key: str, *, limit: int = 50, start: int = 0) -> Any:
    """List a project's issues (newest first) via JQL."""
    jql = f'project = "{project_key}" ORDER BY created DESC'
    return build_client(config).jql(jql, start=start, limit=limit)
