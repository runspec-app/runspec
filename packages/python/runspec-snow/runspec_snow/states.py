"""states.py — list and set a record's state choice field (Table API).

ServiceNow has no Jira-style workflow transition endpoint: "state" is a choice
field. Moving a record's state is a field update, so this module lists the valid
choices (from the ``sys_choice`` table) and resolves a label → value before
patching the record.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import runspec as rs

from runspec_snow._cli import opt
from runspec_snow.client import (
    SnowConfig,
    SnowError,
    config_from_env,
    resolve_table,
    snow_request,
)
from runspec_snow.records import resolve_sys_id


def list_states(config: SnowConfig, table: str, field: str = "state") -> list[dict[str, Any]]:
    """Return the active choices for ``table``.``field`` as ``[{label, value, sequence}]``.

    Sorted by the ServiceNow choice ``sequence`` so the list reads in workflow order.
    """
    params = {
        "sysparm_query": f"name={table}^element={field}^inactive=false^language=en",
        "sysparm_fields": "label,value,sequence",
        "sysparm_limit": "200",
    }
    body = snow_request("GET", "/api/now/table/sys_choice", config=config, params=params)
    rows = body.get("result", []) if isinstance(body, dict) else []

    def _seq(row: dict[str, Any]) -> int:
        try:
            return int(row.get("sequence") or 0)
        except (TypeError, ValueError):
            return 0

    return sorted((r for r in rows if isinstance(r, dict)), key=_seq)


def resolve_state_value(states: Any, wanted: str) -> str:
    """Resolve a state by exact value or case-insensitive label to its value.

    ``states`` is the list returned by :func:`list_states`. Raises
    :class:`SnowError` listing the available states when ``wanted`` matches none.
    """
    choices = list(states)
    for c in choices:
        if str(c.get("value")) == wanted:
            return str(c["value"])
    for c in choices:
        if str(c.get("label", "")).lower() == wanted.lower():
            return str(c["value"])
    available = ", ".join(f"{c.get('label')} (value {c.get('value')})" for c in choices) or "none"
    raise SnowError(f"State {wanted!r} not available. Available: {available}")


def update_state(config: SnowConfig, table: str, identifier: str, state: str, field: str = "state", work_note: str | None = None) -> str:
    """Set a record's state (by label or value), optionally adding a work note.

    Returns the resolved state value. ``identifier`` may be a sys_id or number.
    """
    sys_id = resolve_sys_id(config, table, identifier)
    value = resolve_state_value(list_states(config, table, field), state)
    payload: dict[str, Any] = {field: value}
    if work_note:
        payload["work_notes"] = work_note
    snow_request("PATCH", f"/api/now/table/{table}/{sys_id}", config=config, json_body=payload)
    return value


def main_list_states() -> None:
    spec = rs.parse("snow-list-states")
    try:
        config = config_from_env(env=opt(spec.env))
        table = resolve_table(config, opt(spec.table))
        field = opt(spec.field) or "state"
        states = list_states(config, table, field=field)
        print(json.dumps([{"label": s.get("label"), "value": s.get("value")} for s in states]))
    except SnowError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_update_state() -> None:
    spec = rs.parse("snow-update-state")
    try:
        config = config_from_env(env=opt(spec.env))
        table = resolve_table(config, opt(spec.table))
        field = opt(spec.field) or "state"
        value = update_state(config, table, identifier=str(spec.record), state=str(spec.state), field=field, work_note=opt(spec.comment))
        print(json.dumps({"table": table, "record": str(spec.record), "field": field, "value": value, "updated": True}))
    except SnowError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
