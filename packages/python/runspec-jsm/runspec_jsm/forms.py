"""forms.py — request-type and portal-field discovery for creating requests.

Creating a JSM customer request needs ``requestFieldValues`` keyed by the real
``fieldId`` (e.g. ``customfield_10010``), not the human label — and the request
types and their fields are themselves lookups. These helpers let a wrapper
package discover the form, map friendly names → fieldIds, and validate required
fields *before* calling :func:`runspec_jsm.tickets.create_request`, so misuse
fails locally with a readable error instead of an opaque server 400.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import runspec as rs

from runspec_jsm._cli import opt
from runspec_jsm.client import JsmConfig, JsmError, config_from_env, jsm_request


def _require_service_desk_id(config: JsmConfig, service_desk_id: str | None) -> str:
    sd = service_desk_id or config.service_desk_id
    if not sd:
        raise JsmError("No service desk id — pass service_desk_id or set it on the config (JSM_SERVICE_DESK_ID)")
    return sd


def list_request_types(config: JsmConfig, service_desk_id: str | None = None) -> list[dict[str, Any]]:
    """Return the request types for a service desk (``[{id, name, ...}]``)."""
    sd = _require_service_desk_id(config, service_desk_id)
    body = jsm_request("GET", f"/rest/servicedeskapi/servicedesk/{sd}/requesttype", config=config)
    return list(body.get("values", [])) if isinstance(body, dict) else []


def get_request_type_fields(config: JsmConfig, request_type_id: str, service_desk_id: str | None = None) -> list[dict[str, Any]]:
    """Return the field metadata for a request type.

    Each entry carries ``fieldId``, ``name``, ``required``, ``jiraSchema`` and
    ``validValues`` — everything a wrapper needs to map and validate input.
    """
    sd = _require_service_desk_id(config, service_desk_id)
    body = jsm_request(
        "GET",
        f"/rest/servicedeskapi/servicedesk/{sd}/requesttype/{request_type_id}/field",
        config=config,
    )
    if isinstance(body, dict):
        return list(body.get("requestTypeFields", []))
    return []


def resolve_request_fields(fields_meta: Any, friendly: dict[str, Any]) -> dict[str, Any]:
    """Map a friendly ``{name_or_fieldId: value}`` dict to ``{fieldId: value}``.

    ``fields_meta`` is the value returned by :func:`get_request_type_fields` (or
    the raw ``requestTypeFields`` list / response dict). Matching is by exact
    ``fieldId`` or case-insensitive field ``name``. Raises :class:`JsmError` if a
    supplied key matches no field, or if a **required** field was not supplied.
    """
    fields = _normalise_fields(fields_meta)
    by_id = {f["fieldId"]: f for f in fields if f.get("fieldId")}
    by_name = {str(f.get("name", "")).lower(): f for f in fields if f.get("name")}

    resolved: dict[str, Any] = {}
    unknown: list[str] = []
    for key, value in friendly.items():
        field = by_id.get(key) or by_name.get(key.lower())
        if field is None:
            unknown.append(key)
            continue
        resolved[field["fieldId"]] = value

    if unknown:
        known = sorted({f.get("name", f["fieldId"]) for f in fields})
        raise JsmError(f"Unknown request field(s): {', '.join(unknown)}. Available: {', '.join(known)}")

    missing = [f.get("name", f["fieldId"]) for f in fields if f.get("required") and f["fieldId"] not in resolved]
    if missing:
        raise JsmError(f"Missing required field(s): {', '.join(missing)}")

    return resolved


def _normalise_fields(fields_meta: Any) -> list[dict[str, Any]]:
    """Accept either the raw list or the ``{requestTypeFields: [...]}`` dict."""
    if isinstance(fields_meta, dict):
        return list(fields_meta.get("requestTypeFields", []))
    if isinstance(fields_meta, list):
        return list(fields_meta)
    return []


def main_list_request_types() -> None:
    spec = rs.parse("jsm-list-request-types")
    try:
        config = config_from_env(env=opt(spec.env))
        types = list_request_types(config, service_desk_id=opt(spec.service_desk_id))
        print(json.dumps([{"id": t.get("id"), "name": t.get("name"), "description": t.get("description", "")} for t in types]))
    except JsmError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_list_fields() -> None:
    spec = rs.parse("jsm-list-fields")
    try:
        config = config_from_env(env=opt(spec.env))
        fields = get_request_type_fields(config, request_type_id=str(spec.request_type_id), service_desk_id=opt(spec.service_desk_id))
        print(
            json.dumps(
                [
                    {
                        "fieldId": f.get("fieldId"),
                        "name": f.get("name"),
                        "required": f.get("required", False),
                        "type": (f.get("jiraSchema") or {}).get("type"),
                    }
                    for f in fields
                ]
            )
        )
    except JsmError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
