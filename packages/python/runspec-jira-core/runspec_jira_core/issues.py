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


_EDIT_VERBS = frozenset({"set", "add", "remove", "edit", "copy"})


def _is_operations(value: Any) -> bool:
    """True for Jira's operations form: a non-empty list of one-verb dicts,
    e.g. ``[{"set": "New title"}]`` or ``[{"add": "urgent"}]``."""
    return isinstance(value, list) and bool(value) and all(isinstance(op, dict) and len(op) == 1 and next(iter(op)) in _EDIT_VERBS for op in value)


def edit_body(fields: dict[str, Any]) -> dict[str, Any]:
    """The ``PUT /issue/{key}`` body for ``fields``.

    Plain values go under ``"fields"`` (``{"labels": ["x"]}`` replaces the
    labels); values in Jira's operations form go under ``"update"``. A dict that
    is already a body (only ``fields`` / ``update`` keys) is sent as it is."""
    if fields and set(fields) <= {"fields", "update"}:
        return fields
    plain = {k: v for k, v in fields.items() if not _is_operations(v)}
    ops = {k: v for k, v in fields.items() if _is_operations(v)}
    body: dict[str, Any] = {}
    if plain:
        body["fields"] = plain
    if ops:
        body["update"] = ops
    return body


def edit_issue_fields(client: Any, key: str, fields: dict[str, Any]) -> None:
    """Set an issue's fields. The SDK's ``edit_issue`` wraps everything in
    ``"update"`` (the operations form), which Jira rejects for plain values, so
    the body is built here and sent with a plain PUT."""
    client.put(f"rest/api/2/issue/{key}", data=edit_body(fields))


@translate_errors
def update_issue(config: JiraConfig, key: str, *, fields_json: str) -> dict[str, Any]:
    """Update an issue from a JSON object of fields. Plain values replace the
    field (``{"summary": "New title"}``); Jira's operations form
    (``{"labels": [{"add": "urgent"}]}``) is passed through."""
    fields = parse_json_arg("fields", fields_json)
    if not isinstance(fields, dict):
        raise JiraError("--fields must be a JSON object")
    edit_issue_fields(build_client(config), key, fields)
    return {"key": key, "updated": True}


@translate_errors
def assign_issue(config: JiraConfig, key: str, assignee: str) -> dict[str, Any]:
    """Assign an issue. On Cloud ``assignee`` is an accountId; on Server/DC it is a
    username."""
    client = build_client(config)
    if config.cloud:
        client.assign_issue(key, account_id=assignee)
    else:
        edit_issue_fields(client, key, {"assignee": {"name": assignee}})
    return {"key": key, "assignee": assignee}


@translate_errors
def delete_issue(config: JiraConfig, key: str, *, delete_subtasks: bool = True) -> dict[str, Any]:
    """Delete an issue (and, by default, its subtasks)."""
    build_client(config).delete_issue(key, delete_subtasks=delete_subtasks)
    return {"key": key, "deleted": True}
