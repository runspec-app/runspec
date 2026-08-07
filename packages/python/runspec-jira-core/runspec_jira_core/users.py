"""users.py — user profile lookup and assignable-user search."""

from __future__ import annotations

from typing import Any

from runspec_jira_core.client import build_client, translate_errors
from runspec_jira_core.config import JiraConfig
from runspec_jira_core.errors import JiraError


@translate_errors
def get_user_profile(
    config: JiraConfig,
    *,
    username: str | None = None,
    account_id: str | None = None,
    key: str | None = None,
) -> Any:
    """Fetch a user profile. Cloud identifies users by ``--account-id``; Server/DC
    by ``--username`` (or ``--key``)."""
    client = build_client(config)
    if config.cloud:
        if not account_id:
            raise JiraError("Cloud requires --account-id for get-user-profile")
        return client.user(account_id=account_id)
    if username:
        return client.user(username=username)
    if key:
        return client.user(key=key)
    raise JiraError("Server/DC requires --username or --key for get-user-profile")


@translate_errors
def search_assignable_users(
    config: JiraConfig,
    project_key: str,
    *,
    start: int = 0,
    limit: int = 50,
) -> Any:
    """List users assignable to issues in a project (one page)."""
    return build_client(config).get_all_assignable_users_for_project(project_key, start=start, limit=limit)
