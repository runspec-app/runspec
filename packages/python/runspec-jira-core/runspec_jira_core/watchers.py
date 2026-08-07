"""watchers.py — read and modify issue watchers."""

from __future__ import annotations

from typing import Any

from runspec_jira_core.client import build_client, translate_errors
from runspec_jira_core.config import JiraConfig
from runspec_jira_core.errors import JiraError


@translate_errors
def get_watchers(config: JiraConfig, key: str) -> Any:
    """List the watchers of an issue."""
    return build_client(config).issue_get_watchers(key)


@translate_errors
def add_watcher(config: JiraConfig, key: str, user: str) -> dict[str, Any]:
    """Add a watcher to an issue (username on Server/DC; accountId on Cloud)."""
    build_client(config).issue_add_watcher(key, user)
    return {"key": key, "watcher": user, "added": True}


@translate_errors
def remove_watcher(
    config: JiraConfig,
    key: str,
    *,
    user: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Remove a watcher. Cloud uses ``--account-id``; Server/DC uses ``--user``."""
    client = build_client(config)
    if config.cloud:
        if not account_id:
            raise JiraError("Cloud requires --account-id for remove-watcher")
        client.issue_delete_watcher(key, account_id=account_id)
    else:
        if not user:
            raise JiraError("Server/DC requires --user for remove-watcher")
        client.issue_delete_watcher(key, user=user)
    return {"key": key, "removed": True}
