"""projects.py — project discovery and create-metadata (field discovery)."""

from __future__ import annotations

from typing import Any

from runspec_jira_core.client import build_client, translate_errors
from runspec_jira_core.config import JiraConfig


@translate_errors
def get_all_projects(config: JiraConfig) -> Any:
    """List every project visible to the configured credentials."""
    return build_client(config).get_all_projects()


@translate_errors
def search_projects(config: JiraConfig, query: str) -> list[dict[str, Any]]:
    """Filter visible projects by a case-insensitive substring of name or key."""
    needle = query.lower()
    projects = build_client(config).get_all_projects()
    return [p for p in projects if needle in str(p.get("name", "")).lower() or needle in str(p.get("key", "")).lower()]


@translate_errors
def get_project_components(config: JiraConfig, project_key: str) -> Any:
    """List a project's components."""
    return build_client(config).get_project_components(project_key)


@translate_errors
def get_project_issue_types(config: JiraConfig, project_key: str) -> Any:
    """List the issue types available when creating in a project."""
    return build_client(config).issue_createmeta_issuetypes(project_key)


@translate_errors
def get_create_fields(config: JiraConfig, project_key: str) -> Any:
    """Return the create-screen field metadata for a project (all issue types) —
    the discovery step before ``create-issue`` with ``--additional-fields``."""
    return build_client(config).issue_createmeta(project_key, expand="projects.issuetypes.fields")
