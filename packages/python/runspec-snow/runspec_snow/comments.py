"""comments.py — add a customer comment or an internal work note (Table API).

ServiceNow's ``comments`` field is the customer-visible "Additional comments"
journal; ``work_notes`` is the agent-only internal journal. This is the direct
analog of JSM's public vs internal comment split.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import runspec as rs

from runspec_snow._cli import flag, opt
from runspec_snow.client import (
    SnowConfig,
    SnowError,
    config_from_env,
    resolve_table,
    snow_request,
)
from runspec_snow.records import resolve_sys_id


def add_comment(config: SnowConfig, table: str, identifier: str, body: str, work_note: bool = False) -> dict[str, Any]:
    """Append a comment to a record. ``work_note=True`` makes it an internal note.

    ``identifier`` may be a sys_id or a record number.
    """
    sys_id = resolve_sys_id(config, table, identifier)
    field = "work_notes" if work_note else "comments"
    payload = {field: body}
    resp = snow_request("PATCH", f"/api/now/table/{table}/{sys_id}", config=config, json_body=payload)
    result = resp.get("result", {}) if isinstance(resp, dict) else {}
    return result if isinstance(result, dict) else {}


def main_add_comment() -> None:
    spec = rs.parse("snow-add-comment")
    try:
        config = config_from_env(env=opt(spec.env))
        table = resolve_table(config, opt(spec.table))
        work_note = flag(spec.internal)
        record = add_comment(config, table, identifier=str(spec.record.value), body=str(spec.body.value), work_note=work_note)
        print(json.dumps({"table": table, "sysId": record.get("sys_id"), "number": record.get("number"), "workNote": work_note}))
    except SnowError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
