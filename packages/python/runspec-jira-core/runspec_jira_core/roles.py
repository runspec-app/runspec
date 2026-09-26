"""roles.py — project role membership (list / members / add / remove).

Uses the platform REST API (``/rest/api/2/project/{key}/role``), which is public
and stable on Server/Data Center and Cloud. Roles are accepted by **name** (e.g.
``"Service Desk Team"``, case-insensitive) or numeric id. Users are usernames on
Data Center and accountIds on Cloud; groups are group names on both.

Adds and removes are idempotent: the result reports which users/groups changed
and which were already in (or already absent from) the role, so a retry is safe
and an agent can report exactly what happened.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from runspec_jira_core.client import build_client, translate_errors
from runspec_jira_core.config import JiraConfig
from runspec_jira_core.errors import JiraError
from runspec_jira_core.resolve import suggest

USER_ACTOR = "atlassian-user-role-actor"
GROUP_ACTOR = "atlassian-group-role-actor"


def _role_path(project: str, role_id: str | None = None) -> str:
    base = f"rest/api/2/project/{project}/role"
    return f"{base}/{role_id}" if role_id else base


def _roles_index(client: Any, project: str) -> dict[str, str]:
    """``{role name: role id}`` from the ``{name: self-url}`` map Jira returns."""
    raw = client.get(_role_path(project)) or {}
    return {name: str(url).rstrip("/").rsplit("/", 1)[-1] for name, url in raw.items()}


def _resolve_role(client: Any, project: str, role: str) -> tuple[str, str]:
    index = _roles_index(client, project)
    for name, role_id in index.items():
        if role.casefold() in (name.casefold(), role_id):
            return role_id, name
    raise JiraError(f"No role {role!r} in project {project}{suggest(role, index)} (roles: {', '.join(sorted(index))})")


def _actor_user_id(actor: dict[str, Any], cloud: bool) -> str | None:
    if cloud:
        return (actor.get("actorUser") or {}).get("accountId")
    return actor.get("name")


def _members(client: Any, config: JiraConfig, project: str, role_id: str) -> dict[str, Any]:
    body = client.get(_role_path(project, role_id)) or {}
    users, groups = [], []
    for actor in body.get("actors") or []:
        if actor.get("type") == USER_ACTOR:
            users.append({"user": _actor_user_id(actor, config.cloud), "displayName": actor.get("displayName")})
        elif actor.get("type") == GROUP_ACTOR:
            groups.append(actor.get("name") or (actor.get("actorGroup") or {}).get("name"))
    return {"id": role_id, "name": body.get("name"), "users": users, "groups": groups}


@translate_errors
def list_project_roles(config: JiraConfig, project: str) -> list[dict[str, str]]:
    """List a project's roles as ``[{id, name}]``."""
    index = _roles_index(build_client(config), project)
    return [{"id": role_id, "name": name} for name, role_id in sorted(index.items())]


@translate_errors
def get_role_members(config: JiraConfig, project: str, role: str) -> dict[str, Any]:
    """Direct members of a project role: ``{id, name, users: [{user, displayName}], groups: [..]}``.

    Users who hold the role *through a group* are not expanded — see
    :func:`runspec_jira_core.users.check_user_access` for a per-user answer.
    """
    client = build_client(config)
    role_id, _ = _resolve_role(client, project, role)
    return _members(client, config, project, role_id)


@translate_errors
def add_to_role(
    config: JiraConfig,
    project: str,
    role: str,
    *,
    users: Sequence[str] = (),
    groups: Sequence[str] = (),
) -> dict[str, Any]:
    """Add users and/or groups to a project role (idempotent)."""
    if not users and not groups:
        raise JiraError("add-to-role needs at least one --user or --group")
    client = build_client(config)
    role_id, role_name = _resolve_role(client, project, role)
    current = _members(client, config, project, role_id)
    have_users = {u["user"] for u in current["users"]}
    have_groups = set(current["groups"])
    new_users = [u for u in dict.fromkeys(users) if u not in have_users]
    new_groups = [g for g in dict.fromkeys(groups) if g not in have_groups]
    if new_users:
        client.post(_role_path(project, role_id), data={"user": new_users})
    if new_groups:
        client.post(_role_path(project, role_id), data={"group": new_groups})
    return {
        "project": project,
        "role": role_name,
        "added_users": new_users,
        "already_users": [u for u in dict.fromkeys(users) if u in have_users],
        "added_groups": new_groups,
        "already_groups": [g for g in dict.fromkeys(groups) if g in have_groups],
    }


@translate_errors
def remove_from_role(
    config: JiraConfig,
    project: str,
    role: str,
    *,
    users: Sequence[str] = (),
    groups: Sequence[str] = (),
) -> dict[str, Any]:
    """Remove users and/or groups from a project role (idempotent)."""
    if not users and not groups:
        raise JiraError("remove-from-role needs at least one --user or --group")
    client = build_client(config)
    role_id, role_name = _resolve_role(client, project, role)
    current = _members(client, config, project, role_id)
    have_users = {u["user"] for u in current["users"]}
    have_groups = set(current["groups"])
    removed_users = [u for u in dict.fromkeys(users) if u in have_users]
    removed_groups = [g for g in dict.fromkeys(groups) if g in have_groups]
    for user in removed_users:  # ?user= takes a username (DC) or accountId (Cloud)
        client.delete(_role_path(project, role_id), params={"user": user})
    for group in removed_groups:
        client.delete(_role_path(project, role_id), params={"group": group})
    return {
        "project": project,
        "role": role_name,
        "removed_users": removed_users,
        "not_member_users": [u for u in dict.fromkeys(users) if u not in have_users],
        "removed_groups": removed_groups,
        "not_member_groups": [g for g in dict.fromkeys(groups) if g not in have_groups],
    }
