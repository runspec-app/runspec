"""servicedesk.py — Jira Service Management (JSM) request operations.

These use the ``ServiceDesk`` client (the experimental ``servicedeskapi``), not the
``Jira`` client. Request-type ids and fields differ per instance, so the flow is:
``list-request-types`` → ``list-request-type-fields`` → ``raise-request`` with a
``--field-values`` JSON object keyed by fieldId.
"""

from __future__ import annotations

from typing import Any

from runspec_jira_core.client import build_servicedesk, parse_json_arg, translate_errors
from runspec_jira_core.config import JiraConfig
from runspec_jira_core.errors import JiraError
from runspec_jira_core.resolve import suggest


def _sd_id(config: JiraConfig, service_desk_id: str | None) -> str:
    sd = service_desk_id or config.service_desk_id
    if not sd:
        raise JiraError("No service desk id — pass --service-desk-id or set JIRA_SERVICE_DESK_ID")
    return sd


@translate_errors
def list_request_types(config: JiraConfig, *, service_desk_id: str | None = None) -> Any:
    """List the request types offered by a service desk."""
    return build_servicedesk(config).get_request_types(_sd_id(config, service_desk_id))


@translate_errors
def list_request_type_fields(
    config: JiraConfig,
    request_type_id: str,
    *,
    service_desk_id: str | None = None,
) -> Any:
    """List the fields a request type expects (feeds ``raise-request``).

    The ``ServiceDesk`` client has no dedicated method for this, so we call the
    experimental ``servicedeskapi`` endpoint directly through its REST session.
    """
    sd = build_servicedesk(config)
    sd_id = _sd_id(config, service_desk_id)
    path = f"rest/servicedeskapi/servicedesk/{sd_id}/requesttype/{request_type_id}/field"
    return sd.get(path, headers=sd.experimental_headers)


@translate_errors
def create_customer_request(
    config: JiraConfig,
    request_type_id: str,
    field_values_json: str,
    *,
    service_desk_id: str | None = None,
    raise_on_behalf_of: str | None = None,
) -> Any:
    """Raise a customer request. ``field_values_json`` is a JSON object keyed by
    fieldId (as returned by ``list-request-type-fields``)."""
    values = parse_json_arg("field-values", field_values_json)
    if not isinstance(values, dict):
        raise JiraError("--field-values must be a JSON object of fieldId: value")
    return build_servicedesk(config).create_customer_request(
        _sd_id(config, service_desk_id),
        request_type_id,
        values,
        raise_on_behalf_of=raise_on_behalf_of,
    )


@translate_errors
def get_request(config: JiraConfig, issue_id_or_key: str) -> Any:
    """Fetch a customer request by issue id or key."""
    return build_servicedesk(config).get_customer_request(issue_id_or_key)


# ── project → service desk resolution ────────────────────────────────────────

SD_API = "rest/servicedeskapi"


def sd_get_all(sd: Any, path: str, *, params: dict[str, Any] | None = None, page: int = 50) -> list[Any]:
    """GET every page of a paged ``servicedeskapi`` collection (``values`` + ``isLastPage``)."""
    out: list[Any] = []
    start = 0
    while True:
        body = sd.get(path, params={**(params or {}), "start": start, "limit": page}, headers=sd.experimental_headers) or {}
        if not isinstance(body, dict):
            return out
        values = body.get("values") or []
        out.extend(values)
        if body.get("isLastPage", True) or not values:
            return out
        start += len(values)


@translate_errors
def list_service_desks(config: JiraConfig) -> list[dict[str, Any]]:
    """List every service desk as ``{id, projectKey, projectName, projectId}``."""
    sd = build_servicedesk(config)
    return [{k: desk.get(k) for k in ("id", "projectId", "projectKey", "projectName")} for desk in sd_get_all(sd, f"{SD_API}/servicedesk")]


def match_service_desk(desks: list[dict[str, Any]], project: str) -> dict[str, Any]:
    """Pick the service desk for ``project`` — a project key, project name, or service desk id.

    Matching is case-insensitive. An unknown project raises :class:`JiraError`
    naming the closest keys/names so the caller can correct it in one step.
    """
    wanted = project.strip().casefold()
    for field in ("projectKey", "projectName", "id"):
        for desk in desks:
            if str(desk.get(field) or "").casefold() == wanted:
                return desk
    names = [str(d.get("projectKey")) for d in desks] + [str(d.get("projectName")) for d in desks]
    raise JiraError(f"No service desk project {project!r}{suggest(project, names)}")


@translate_errors
def resolve_service_desk(config: JiraConfig, project: str) -> dict[str, Any]:
    """Resolve a project key or name (or service desk id) to its service desk record."""
    return match_service_desk(list_service_desks(config), project)
