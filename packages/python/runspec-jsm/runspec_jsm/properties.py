"""properties.py — set issue entity properties (core Jira API)."""

from __future__ import annotations

import json
import sys
from typing import Any

import runspec as rs

from runspec_jsm._cli import opt
from runspec_jsm.client import JsmConfig, JsmError, config_from_env, jsm_request


def set_issue_property(config: JsmConfig, issue_key: str, property_key: str, value: Any) -> None:
    """Set (PUT) an entity property on an issue. ``value`` is any JSON value."""
    jsm_request(
        "PUT",
        f"/rest/api/2/issue/{issue_key}/properties/{property_key}",
        config=config,
        json_body=value,
    )


def main_set_property() -> None:
    spec = rs.parse("jsm-set-property")
    try:
        config = config_from_env(env=opt(spec.env))
        raw = str(spec.value.value)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            # Not valid JSON — store the raw string as the property value.
            value = raw
        set_issue_property(config, issue_key=str(spec.issue.value), property_key=str(spec.property_key.value), value=value)
        print(json.dumps({"issue": str(spec.issue.value), "propertyKey": str(spec.property_key.value), "set": True}))
    except JsmError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
