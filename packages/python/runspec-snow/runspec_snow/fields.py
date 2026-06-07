"""fields.py — set arbitrary fields on a record (Table API)."""

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
from runspec_snow.records import resolve_sys_id


def set_fields(config: SnowConfig, table: str, identifier: str, fields: dict[str, Any]) -> dict[str, Any]:
    """PATCH one or more columns on a record. ``identifier`` may be a sys_id or number."""
    if not fields:
        raise SnowError("No fields to set — pass at least one field=value")
    sys_id = resolve_sys_id(config, table, identifier)
    resp = snow_request("PATCH", f"/api/now/table/{table}/{sys_id}", config=config, json_body=fields)
    result = resp.get("result", {}) if isinstance(resp, dict) else {}
    return result if isinstance(result, dict) else {}


def main_set_field() -> None:
    spec = rs.parse("snow-set-field")
    try:
        config = config_from_env(env=opt(spec.env))
        table = resolve_table(config, opt(spec.table))
        fields = parse_field_pairs(spec.field)
        record = set_fields(config, table, identifier=str(spec.record), fields=fields)
        print(json.dumps({"table": table, "sysId": record.get("sys_id"), "number": record.get("number"), "set": sorted(fields)}))
    except SnowError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
