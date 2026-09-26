"""users.py — user profile lookup and assignable-user search."""

from __future__ import annotations

from typing import Any

from runspec_jira_core.client import build_client, translate_errors
from runspec_jira_core.config import JiraConfig
from runspec_jira_core.errors import JiraError
from runspec_jira_core.roles import GROUP_ACTOR, USER_ACTOR, _actor_user_id, _role_path, _roles_index


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


# ── access check ─────────────────────────────────────────────────────────────

JSM_APP_ROLE = "jira-servicedesk"
AGENT_ROLE = "Service Desk Team"
CUSTOMER_ROLE = "Service Desk Customers"


def _items(block: Any) -> list[dict[str, Any]]:
    if not isinstance(block, dict):
        return []
    return block.get("items") or []


@translate_errors
def check_user_access(config: JiraConfig, user: str, *, project: str | None = None) -> dict[str, Any]:
    """One-call answer to "what can this user do (in this project)?".

    Returns the profile basics, groups, application roles (licences) and — when
    ``project`` is given — every project role the user holds, directly or through
    a group, plus ``is_agent`` / ``is_customer`` and a list of human-readable
    ``findings`` (inactive account, no JSM licence, not in the agent role, …).

    ``user`` is a username on Data Center, an accountId on Cloud. Customer access
    granted only through a JSM *organization* is not detected here (JSM grants it
    per organization, not per role) — ``findings`` says so when relevant.
    """
    client = build_client(config)
    params = {"accountId" if config.cloud else "username": user, "expand": "groups,applicationRoles"}
    profile = client.get("rest/api/2/user", params=params) or {}
    groups = sorted(str(g["name"]) for g in _items(profile.get("groups")) if g.get("name"))
    app_roles = sorted(str(r["key"]) for r in _items(profile.get("applicationRoles")) if r.get("key"))
    active = bool(profile.get("active", True))
    result: dict[str, Any] = {
        "user": user,
        "displayName": profile.get("displayName"),
        "emailAddress": profile.get("emailAddress"),
        "active": active,
        "groups": groups,
        "application_roles": app_roles,
        "jsm_licensed": JSM_APP_ROLE in app_roles,
    }
    findings: list[str] = []
    if not active:
        findings.append("account is inactive")

    if project:
        group_set = set(groups)
        held: list[dict[str, str]] = []
        for role_name, role_id in sorted(_roles_index(client, project).items()):
            detail = client.get(_role_path(project, role_id)) or {}
            for actor in detail.get("actors") or []:
                if actor.get("type") == USER_ACTOR and _actor_user_id(actor, config.cloud) == user:
                    held.append({"role": role_name, "via": "direct"})
                    break
                group = actor.get("name") or (actor.get("actorGroup") or {}).get("name")
                if actor.get("type") == GROUP_ACTOR and group in group_set:
                    held.append({"role": role_name, "via": f"group:{group}"})
                    break
        names = {h["role"].casefold() for h in held}
        in_agent_role = AGENT_ROLE.casefold() in names
        result.update(
            project=project,
            project_roles=held,
            is_agent=in_agent_role and result["jsm_licensed"] and active,
            is_customer=CUSTOMER_ROLE.casefold() in names,
        )
        if in_agent_role and not result["jsm_licensed"]:
            findings.append(f"in the {AGENT_ROLE!r} role on {project} but has no Jira Service Management licence ({JSM_APP_ROLE})")
        if not in_agent_role and result["jsm_licensed"]:
            findings.append(f"has a JSM licence but is not in the {AGENT_ROLE!r} role on {project}")
        if not held:
            findings.append(f"holds no role on {project}; customer access via an organization is not checked here")
    result["findings"] = findings
    return result
