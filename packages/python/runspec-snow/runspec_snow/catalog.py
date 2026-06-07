"""catalog.py — read-only Service Catalog discovery (Service Catalog API).

The analog of JSM's request-type / portal-field discovery: list orderable catalog
items, then inspect an item's variables (the form fields a request would carry).
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
    snow_request,
)


def list_catalog_items(config: SnowConfig, query: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Return orderable catalog items (``[{sys_id, name, short_description, ...}]``)."""
    params: dict[str, Any] = {"sysparm_limit": str(limit)}
    if query:
        params["sysparm_text"] = query
    body = snow_request("GET", "/api/sn_sc/servicecatalog/items", config=config, params=params)
    result = body.get("result", []) if isinstance(body, dict) else []
    return [r for r in result if isinstance(r, dict)]


def get_catalog_item_variables(config: SnowConfig, item_sys_id: str) -> list[dict[str, Any]]:
    """Return the variables (form fields) for a catalog item."""
    body = snow_request("GET", f"/api/sn_sc/servicecatalog/items/{item_sys_id}/variables", config=config)
    result = body.get("result", []) if isinstance(body, dict) else []
    return [r for r in result if isinstance(r, dict)]


def main_list_catalog_items() -> None:
    spec = rs.parse("snow-list-catalog-items")
    try:
        config = config_from_env(env=opt(spec.env))
        limit = int(spec.limit)
        items = list_catalog_items(config, query=opt(spec.query), limit=limit)
        print(json.dumps([{"sysId": i.get("sys_id"), "name": i.get("name"), "shortDescription": i.get("short_description", "")} for i in items]))
    except SnowError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_list_variables() -> None:
    spec = rs.parse("snow-list-variables")
    try:
        config = config_from_env(env=opt(spec.env))
        variables = get_catalog_item_variables(config, item_sys_id=str(spec.item))
        print(
            json.dumps(
                [
                    {
                        "name": v.get("name"),
                        "label": v.get("label"),
                        "type": v.get("type"),
                        "mandatory": v.get("mandatory", False),
                    }
                    for v in variables
                ]
            )
        )
    except SnowError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
