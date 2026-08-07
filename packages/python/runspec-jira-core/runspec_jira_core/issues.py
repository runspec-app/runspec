"""issues.py — issue read/create/update/assign/delete."""

from __future__ import annotations

from typing import Any

from runspec_jira_core.client import build_client, parse_json_arg, translate_errors
from runspec_jira_core.config import JiraConfig
from runspec_jira_core.errors import JiraError


@translate_errors
def get_issue(
    config: JiraConfig,
    key: str,
    *,
    fields: list[str] | None = None,
    expand: str | None = None,
) -> dict[str, Any]:
    """Fetch a single issue by key. ``fields`` restricts the returned fields."""
    fields_arg = ",".join(fields) if fields else None
    return build_client(config).get_issue(key, fields=fields_arg, expand=expand)


@translate_errors
def create_issue(
    config: JiraConfig,
    project: str,
    summary: str,
    issue_type: str,
    *,
    description: str | None = None,
    additional_fields: str | None = None,
) -> dict[str, Any]:
    """Create an issue. ``additional_fields`` is a JSON object merged into the
    create payload (e.g. ``{"labels": ["x"], "priority": {"name": "High"}}``)."""
    extra = parse_json_arg("additional-fields", additional_fields) or {}
    if not isinstance(extra, dict):
        raise JiraError("--additional-fields must be a JSON object")
    fields: dict[str, Any] = {
        "project": {"key": project},
        "summary": summary,
        "issuetype": {"name": issue_type},
    }
    if description:
        fields["description"] = description
    fields.update(extra)
    return build_client(config).create_issue(fields=fields)


@translate_errors
def update_issue(config: JiraConfig, key: str, *, fields_json: str) -> dict[str, Any]:
    """Update an issue from a JSON object of fields."""
    fields = parse_json_arg("fields", fields_json)
    if not isinstance(fields, dict):
        raise JiraError("--fields must be a JSON object")
    build_client(config).edit_issue(key, fields)
    return {"key": key, "updated": True}


@translate_errors
def assign_issue(config: JiraConfig, key: str, assignee: str) -> dict[str, Any]:
    """Assign an issue. On Cloud ``assignee`` is an accountId; on Server/DC it is a
    username."""
    client = build_client(config)
    if config.cloud:
        client.assign_issue(key, account_id=assignee)
    else:
        client.edit_issue(key, {"assignee": {"name": assignee}})
    return {"key": key, "assignee": assignee}


@translate_errors
def delete_issue(config: JiraConfig, key: str, *, delete_subtasks: bool = True) -> dict[str, Any]:
    """Delete an issue (and, by default, its subtasks)."""
    build_client(config).delete_issue(key, delete_subtasks=delete_subtasks)
    return {"key": key, "deleted": True}
