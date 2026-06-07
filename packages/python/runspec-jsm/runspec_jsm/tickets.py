"""tickets.py — raise and read JSM customer requests (servicedeskapi)."""

from __future__ import annotations

import json
import sys
from typing import Any

import runspec as rs

from runspec_jsm._cli import opt, parse_field_pairs
from runspec_jsm.client import JsmConfig, JsmError, config_from_env, jsm_request


def create_request(
    config: JsmConfig,
    request_type_id: str,
    field_values: dict[str, Any],
    service_desk_id: str | None = None,
    raise_on_behalf_of: str | None = None,
) -> dict[str, Any]:
    """Create a customer request.

    ``field_values`` must already be keyed by ``fieldId`` (use
    :func:`runspec_jsm.forms.resolve_request_fields` to map friendly names).
    ``service_desk_id`` falls back to ``config.service_desk_id``.
    """
    sd = service_desk_id or config.service_desk_id
    if not sd:
        raise JsmError("No service desk id — pass service_desk_id or set it on the config (JSM_SERVICE_DESK_ID)")

    payload: dict[str, Any] = {
        "serviceDeskId": str(sd),
        "requestTypeId": str(request_type_id),
        "requestFieldValues": field_values,
    }
    if raise_on_behalf_of:
        payload["raiseOnBehalfOf"] = raise_on_behalf_of

    return jsm_request("POST", "/rest/servicedeskapi/request", config=config, json_body=payload)


def get_request(config: JsmConfig, issue_key: str) -> dict[str, Any]:
    """Fetch a customer request by issue key or id."""
    return jsm_request("GET", f"/rest/servicedeskapi/request/{issue_key}", config=config)


def main_raise_request() -> None:
    spec = rs.parse("jsm-raise-request")
    try:
        config = config_from_env(env=opt(spec.env))
        field_values = parse_field_pairs(spec.field)
        # Convenience: summary/description map to the standard request fields.
        if opt(spec.summary):
            field_values.setdefault("summary", str(spec.summary))
        if opt(spec.description):
            field_values.setdefault("description", str(spec.description))

        result = create_request(
            config,
            request_type_id=str(spec.request_type_id),
            field_values=field_values,
            service_desk_id=opt(spec.service_desk_id),
            raise_on_behalf_of=opt(spec.raise_on_behalf_of),
        )
        print(json.dumps({"issueKey": result.get("issueKey"), "requestTypeId": str(spec.request_type_id), "created": True}))
    except JsmError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_get_request() -> None:
    spec = rs.parse("jsm-get-request")
    try:
        config = config_from_env(env=opt(spec.env))
        result = get_request(config, issue_key=str(spec.issue))
        print(json.dumps(result))
    except JsmError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
