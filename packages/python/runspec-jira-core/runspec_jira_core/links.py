"""links.py — issue links, epic links, and remote (web) links."""

from __future__ import annotations

from typing import Any

from atlassian import Jira

from runspec_jira_core.client import build_client, translate_errors
from runspec_jira_core.config import JiraConfig
from runspec_jira_core.errors import JiraError
from runspec_jira_core.issues import edit_issue_fields


@translate_errors
def get_link_types(config: JiraConfig) -> Any:
    """List the issue link types available on the instance (e.g. Blocks, Relates)."""
    return build_client(config).get_issue_link_types()


@translate_errors
def create_issue_link(
    config: JiraConfig,
    link_type: str,
    inward_issue: str,
    outward_issue: str,
    *,
    comment: str | None = None,
) -> dict[str, Any]:
    """Link two issues with a named link type."""
    data: dict[str, Any] = {
        "type": {"name": link_type},
        "inwardIssue": {"key": inward_issue},
        "outwardIssue": {"key": outward_issue},
    }
    if comment:
        data["comment"] = {"body": comment}
    build_client(config).create_issue_link(data)
    return {"linked": True, "type": link_type, "inwardIssue": inward_issue, "outwardIssue": outward_issue}


@translate_errors
def remove_issue_link(config: JiraConfig, link_id: str) -> dict[str, Any]:
    """Remove an issue link by its id."""
    build_client(config).remove_issue_link(link_id)
    return {"removed": True, "linkId": link_id}


def _epic_link_field_id(client: Jira) -> str | None:
    fields: Any = client.get_all_fields()
    for field in fields or []:
        if field.get("name") == "Epic Link":
            return field.get("id")
    return None


@translate_errors
def link_to_epic(
    config: JiraConfig,
    issue_key: str,
    epic_key: str,
    *,
    epic_link_field: str | None = None,
) -> dict[str, Any]:
    """Link an issue to an epic.

    There is no single cross-deployment API for this. On Cloud team-managed
    projects the epic is the issue's ``parent``; company-managed projects and
    Server/DC use the instance-specific "Epic Link" custom field. Pass
    ``--epic-link-field customfield_XXXXX`` to force a specific field; otherwise
    Cloud tries ``parent`` and Server/DC auto-discovers the Epic Link field.
    """
    client = build_client(config)
    if epic_link_field:
        edit_issue_fields(client, issue_key, {epic_link_field: epic_key})
    elif config.cloud:
        edit_issue_fields(client, issue_key, {"parent": {"key": epic_key}})
    else:
        field_id = _epic_link_field_id(client)
        if not field_id:
            raise JiraError("Could not find the 'Epic Link' field — pass --epic-link-field customfield_XXXXX")
        edit_issue_fields(client, issue_key, {field_id: epic_key})
    return {"issue": issue_key, "epic": epic_key}


@translate_errors
def create_remote_issue_link(
    config: JiraConfig,
    issue_key: str,
    url: str,
    title: str,
    *,
    relationship: str | None = None,
) -> Any:
    """Attach a remote (web) link to an issue."""
    return build_client(config).create_or_update_issue_remote_links(issue_key, link_url=url, title=title, relationship=relationship)
