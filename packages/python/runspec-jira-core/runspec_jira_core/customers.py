"""customers.py — JSM customers and organizations, addressed by project.

Every helper takes a project **key or name** (resolved to its service desk), so a
caller never has to look up a service desk id first. Users are usernames on Data
Center and accountIds on Cloud. Organizations are accepted by name or id.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from runspec_jira_core.client import build_servicedesk, translate_errors
from runspec_jira_core.config import JiraConfig
from runspec_jira_core.errors import JiraError
from runspec_jira_core.resolve import suggest
from runspec_jira_core.servicedesk import SD_API, resolve_service_desk, sd_get_all


def _users_body(config: JiraConfig, users: Sequence[str]) -> dict[str, list[str]]:
    return {"accountIds" if config.cloud else "usernames": list(dict.fromkeys(users))}


def _user_row(user: dict[str, Any]) -> dict[str, Any]:
    return {k: user.get(k) for k in ("name", "key", "accountId", "displayName", "emailAddress") if user.get(k) is not None}


@translate_errors
def list_customers(config: JiraConfig, project: str, *, query: str | None = None, limit: int = 50) -> dict[str, Any]:
    """List a service desk project's customers (optionally filtered by ``query``)."""
    desk = resolve_service_desk(config, project)
    sd = build_servicedesk(config)
    params: dict[str, Any] = {"limit": limit}
    if query:
        params["query"] = query
    body = sd.get(f"{SD_API}/servicedesk/{desk['id']}/customer", params=params, headers=sd.experimental_headers) or {}
    return {"project": desk["projectKey"], "customers": [_user_row(u) for u in body.get("values") or []], "isLastPage": body.get("isLastPage", True)}


@translate_errors
def add_customers(config: JiraConfig, project: str, users: Sequence[str]) -> dict[str, Any]:
    """Add existing users as customers of a service desk project."""
    if not users:
        raise JiraError("add-customers needs at least one --user")
    desk = resolve_service_desk(config, project)
    sd = build_servicedesk(config)
    sd.post(f"{SD_API}/servicedesk/{desk['id']}/customer", data=_users_body(config, users), headers=sd.experimental_headers)
    return {"project": desk["projectKey"], "added": list(dict.fromkeys(users))}


@translate_errors
def remove_customers(config: JiraConfig, project: str, users: Sequence[str]) -> dict[str, Any]:
    """Remove customers from a service desk project."""
    if not users:
        raise JiraError("remove-customers needs at least one --user")
    desk = resolve_service_desk(config, project)
    sd = build_servicedesk(config)
    sd.delete(f"{SD_API}/servicedesk/{desk['id']}/customer", data=_users_body(config, users), headers=sd.experimental_headers)
    return {"project": desk["projectKey"], "removed": list(dict.fromkeys(users))}


@translate_errors
def list_organizations(config: JiraConfig, project: str | None = None) -> list[dict[str, Any]]:
    """List organizations — all of them, or only those linked to ``project``."""
    sd = build_servicedesk(config)
    if project:
        desk = resolve_service_desk(config, project)
        path = f"{SD_API}/servicedesk/{desk['id']}/organization"
    else:
        path = f"{SD_API}/organization"
    return [{"id": str(o.get("id")), "name": o.get("name")} for o in sd_get_all(sd, path)]


def _find_org(orgs: list[dict[str, Any]], organization: str) -> dict[str, Any] | None:
    wanted = organization.strip().casefold()
    for org in orgs:
        if wanted in (str(org.get("name") or "").casefold(), str(org.get("id"))):
            return org
    return None


@translate_errors
def add_to_organization(
    config: JiraConfig,
    organization: str,
    users: Sequence[str],
    *,
    project: str | None = None,
    create: bool = False,
) -> dict[str, Any]:
    """Add users to an organization, optionally creating it and linking it to ``project``.

    With ``project`` set, the organization is also attached to that service desk
    (a no-op when it already is), so its members can raise requests there.
    """
    if not users:
        raise JiraError("add-to-organization needs at least one --user")
    sd = build_servicedesk(config)
    orgs = [{"id": str(o.get("id")), "name": o.get("name")} for o in sd_get_all(sd, f"{SD_API}/organization")]
    org = _find_org(orgs, organization)
    created = False
    if org is None:
        if not create:
            raise JiraError(f"No organization {organization!r}{suggest(organization, [str(o['name']) for o in orgs])} — pass --create to create it")
        made = sd.post(f"{SD_API}/organization", data={"name": organization}, headers=sd.experimental_headers) or {}
        org = {"id": str(made.get("id")), "name": made.get("name") or organization}
        created = True
    linked = None
    if project:
        desk = resolve_service_desk(config, project)
        sd.post(f"{SD_API}/servicedesk/{desk['id']}/organization", data={"organizationId": int(org["id"])}, headers=sd.experimental_headers)
        linked = desk["projectKey"]
    sd.post(f"{SD_API}/organization/{org['id']}/user", data=_users_body(config, users), headers=sd.experimental_headers)
    return {"organization": org, "created": created, "linked_project": linked, "added": list(dict.fromkeys(users))}
