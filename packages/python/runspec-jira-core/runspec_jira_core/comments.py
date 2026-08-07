"""comments.py — add and edit issue comments."""

from __future__ import annotations

from typing import Any

from runspec_jira_core.client import build_client, parse_json_arg, translate_errors
from runspec_jira_core.config import JiraConfig


@translate_errors
def add_comment(
    config: JiraConfig,
    key: str,
    body: str,
    *,
    visibility_json: str | None = None,
) -> dict[str, Any]:
    """Add a comment. ``visibility_json`` optionally restricts it, e.g.
    ``{"type": "role", "value": "Administrators"}``."""
    visibility = parse_json_arg("visibility", visibility_json)
    return build_client(config).issue_add_comment(key, body, visibility=visibility)


@translate_errors
def edit_comment(config: JiraConfig, key: str, comment_id: str, body: str) -> dict[str, Any]:
    """Edit an existing comment on an issue."""
    return build_client(config).issue_edit_comment(key, comment_id, body)
