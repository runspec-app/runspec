"""transitions.py — list and apply workflow transitions."""

from __future__ import annotations

from typing import Any

from runspec_jira_core.client import build_client, parse_json_arg, translate_errors
from runspec_jira_core.config import JiraConfig
from runspec_jira_core.errors import JiraError


@translate_errors
def list_transitions(config: JiraConfig, key: str) -> Any:
    """List the workflow transitions available from an issue's current status."""
    return build_client(config).get_issue_transitions(key)


@translate_errors
def transition_issue(
    config: JiraConfig,
    key: str,
    *,
    status: str,
    fields_json: str | None = None,
) -> dict[str, Any]:
    """Move an issue to a target status by name (the SDK resolves the transition).
    ``fields_json`` supplies any fields the transition screen requires."""
    fields = parse_json_arg("fields", fields_json)
    if fields is not None and not isinstance(fields, dict):
        raise JiraError("--fields must be a JSON object")
    build_client(config).set_issue_status(key, status, fields=fields)
    return {"key": key, "status": status}
