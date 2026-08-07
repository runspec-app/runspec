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
