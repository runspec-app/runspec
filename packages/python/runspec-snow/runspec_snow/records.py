"""records.py — create and read ServiceNow records (Table API)."""

from __future__ import annotations

import json
import sys
from typing import Any

import runspec as rs

from runspec_snow._cli import opt, parse_field_pairs
from runspec_snow.client import (
    SnowConfig,
    SnowError,
    config_from_env,
    resolve_table,
    snow_request,
)


def _is_sys_id(value: str) -> bool:
    """True if ``value`` looks like a 32-character hex sys_id."""
    return len(value) == 32 and all(c in "0123456789abcdefABCDEF" for c in value)


def create_record(config: SnowConfig, table: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Insert a record into ``table`` and return the created record.

    ``fields`` is a column → value map (e.g. ``{"short_description": "..."}``).
    """
    body = snow_request("POST", f"/api/now/table/{table}", config=config, json_body=fields)
    result = body.get("result", {}) if isinstance(body, dict) else {}
    return result if isinstance(result, dict) else {}


def get_record(config: SnowConfig, table: str, identifier: str, fields: list[str] | None = None) -> dict[str, Any]:
    """Fetch a single record by sys_id or by record number (e.g. ``INC0010001``).

    A 32-char hex ``identifier`` is treated as a sys_id; otherwise it is looked up
    via ``number=``. Raises :class:`SnowError` when no record matches.
    """
    params: dict[str, Any] = {}
    if fields:
        params["sysparm_fields"] = ",".join(fields)

    if _is_sys_id(identifier):
        body = snow_request("GET", f"/api/now/table/{table}/{identifier}", config=config, params=params or None)
        result = body.get("result") if isinstance(body, dict) else None
        if not result:
            raise SnowError(f"No {table} record with sys_id {identifier}")
        return result if isinstance(result, dict) else {}

    params["sysparm_query"] = f"number={identifier}"
    params["sysparm_limit"] = "1"
    body = snow_request("GET", f"/api/now/table/{table}", config=config, params=params)
    rows = body.get("result", []) if isinstance(body, dict) else []
    if not rows:
        raise SnowError(f"No {table} record with number {identifier}")
    return rows[0]


def resolve_sys_id(config: SnowConfig, table: str, identifier: str) -> str:
    """Return the sys_id for ``identifier`` (already a sys_id, or looked up by number)."""
    if _is_sys_id(identifier):
        return identifier
    record = get_record(config, table, identifier, fields=["sys_id"])
    sys_id = record.get("sys_id")
    if not sys_id:
        raise SnowError(f"Could not resolve sys_id for {table} {identifier}")
    return str(sys_id)


def main_create_record() -> None:
    spec = rs.parse("snow-create-record")
    try:
        config = config_from_env(env=opt(spec.env))
        table = resolve_table(config, opt(spec.table))
        fields = parse_field_pairs(spec.field)
        # Convenience: --short-description maps to the standard short_description column.
        if opt(spec.short_description):
            fields.setdefault("short_description", str(spec.short_description.value))
        if not fields:
            raise SnowError("No fields supplied — set --short-description or --field name=value")

        record = create_record(config, table, fields)
        print(json.dumps({"table": table, "sysId": record.get("sys_id"), "number": record.get("number"), "created": True}))
    except SnowError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_get_record() -> None:
    spec = rs.parse("snow-get-record")
    try:
        config = config_from_env(env=opt(spec.env))
        table = resolve_table(config, opt(spec.table))
        record = get_record(config, table, identifier=str(spec.record.value))
        print(json.dumps(record))
    except SnowError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
