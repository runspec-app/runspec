"""Autonomy manifest shape — what the PreToolUse hook consumes."""

from __future__ import annotations

from runspec_mcp.catalog import Tool
from runspec_mcp.manifest import build_manifest


def _tool(name, autonomy, reason=None, arg_levels=None):
    props = {}
    for a, lvl in (arg_levels or {}).items():
        props[a] = {"type": "string", "x-autonomy": lvl}
    schema = {"name": name, "x-autonomy": autonomy, "inputSchema": {"type": "object", "properties": props}}
    if reason:
        schema["x-autonomy-reason"] = reason
    return Tool(name=name, schema=schema, host={"name": "h"}, runspec_path="/v/env/bin/runspec", group="env", runnable=name, command_path=[], arg_spec={}, autonomy=autonomy)


def test_manifest_shape():
    tools = [
        _tool("greeter", "autonomous"),
        _tool("deploy", "confirm", reason="restarts the service", arg_levels={"force": "supervised"}),
        _tool("rotate", "manual"),
    ]
    m = build_manifest(tools, "fleet")
    assert m["tools"]["greeter"] == {"autonomy": "autonomous"}
    assert m["tools"]["deploy"]["autonomy"] == "confirm"
    assert m["tools"]["deploy"]["reason"] == "restarts the service"
    assert m["tools"]["deploy"]["args"] == {"force": "supervised"}
    assert m["tools"]["rotate"] == {"autonomy": "manual"}
    assert m["server_info"]["name"] == "fleet"
