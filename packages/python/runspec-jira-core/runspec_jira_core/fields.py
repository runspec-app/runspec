"""fields.py — field discovery (find a custom field's id)."""

from __future__ import annotations

from typing import Any

from runspec_jira_core.client import build_client, translate_errors
from runspec_jira_core.config import JiraConfig


@translate_errors
def search_fields(config: JiraConfig, *, query: str | None = None) -> list[dict[str, Any]]:
    """List fields, optionally filtered by a case-insensitive substring of name or
    id (handy for finding a custom field's ``customfield_XXXXX`` id)."""
    fields: Any = build_client(config).get_all_fields()
    if query:
        needle = query.lower()
        return [f for f in fields if needle in str(f.get("name", "")).lower() or needle in str(f.get("id", "")).lower()]
    return list(fields)
