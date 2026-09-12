"""
test_mcp_server.py — pure-helper tests for the console-as-MCP-server module
(schema flattening, autonomy escalation, name dedup). No SDK / socket needed.
"""

from __future__ import annotations

import runspec_console.mcp_server as m


def _runnable(name, **kw):
    r = {
        "name": name,
        "host": kw.get("host", "local"),
        "group": kw.get("group", "venv"),
        "description": kw.get("description", ""),
        "args": kw.get("args", []),
        "autonomy": kw.get("autonomy", "confirm"),
        "requireCommand": kw.get("requireCommand", False),
    }
    if "commands" in kw:
        r["commands"] = kw["commands"]
    return r


def test_escalate_autonomy_most_restrictive():
    assert m.escalate_autonomy(["autonomous", "confirm"]) == "confirm"
    assert m.escalate_autonomy(["confirm", "manual"]) == "manual"
    assert m.escalate_autonomy(["autonomous", "supervised"]) == "supervised"
    # empty / all-blank → confirm (fail-closed); unknown → confirm
    assert m.escalate_autonomy([]) == "confirm"
    assert m.escalate_autonomy(["", None]) == "confirm"
    assert m.escalate_autonomy(["autonomous", "weird"]) == "confirm"


def test_sanitize_tool_name_keeps_hyphen_underscore():
    assert m.sanitize_tool_name("get-issue") == "get-issue"
    assert m.sanitize_tool_name("a b/c.d") == "a_b_c_d"
    assert m.sanitize_tool_name("") == "tool"


def test_flat_runnable_one_tool_password_omitted_required_set():
    r = _runnable(
        "greet",
        description="say hi",
        autonomy="autonomous",
        args=[
            {"name": "who", "type": "str", "required": True},
            {"name": "secret", "type": "password", "required": True},
            {"name": "loud", "type": "flag", "required": False, "default": False},
        ],
    )
    tools, registry = m.build_tools([r])
    assert len(tools) == 1
    t = tools[0]
    assert t.name == "greet"
    assert t.autonomy == "autonomous"
    assert t.command_path == []
    props = t.input_schema["properties"]
    assert "who" in props and "loud" in props
    assert "secret" not in props  # password omitted
    assert t.input_schema["required"] == ["who"]
    assert props["loud"]["type"] == "boolean"
    assert registry["greet"] is t


def test_per_arg_autonomy_escalates():
    r = _runnable(
        "svc",
        autonomy="autonomous",
        args=[{"name": "x", "type": "str", "required": True, "autonomy": "confirm"}],
    )
    tools, _ = m.build_tools([r])
    assert tools[0].autonomy == "confirm"


def test_subcommands_flatten_to_leaf_tools():
    child_hello = _runnable(
        "hello",
        host="local",
        autonomy="autonomous",
        args=[{"name": "name", "type": "str", "required": True}],
    )
    child_bye = _runnable("bye", host="local", autonomy="confirm")
    parent = _runnable(
        "greeter",
        host="local",
        requireCommand=True,
        commands={"hello": child_hello, "bye": child_bye},
    )
    tools, registry = m.build_tools([parent])
    names = sorted(t.name for t in tools)
    # require-command parent is NOT itself callable — only its leaves
    assert names == ["greeter_bye", "greeter_hello"]
    hello = registry["greeter_hello"]
    assert hello.command_path == ["hello"]
    assert hello.runnable == "greeter"
    assert hello.autonomy == "autonomous"


def test_runnable_parent_with_commands_also_callable_when_not_require():
    parent = _runnable(
        "tool",
        requireCommand=False,
        commands={"sub": _runnable("sub")},
    )
    tools, _ = m.build_tools([parent])
    assert sorted(t.name for t in tools) == ["tool", "tool_sub"]


def test_default_autonomy_floor_applies():
    r = _runnable("x", autonomy="autonomous")
    tools, _ = m.build_tools([r], default_autonomy="confirm")
    assert tools[0].autonomy == "confirm"
    # floor never *loosens* a stricter tool
    r2 = _runnable("y", autonomy="manual")
    tools2, _ = m.build_tools([r2], default_autonomy="confirm")
    assert tools2[0].autonomy == "manual"


def test_same_leaf_on_two_hosts_is_host_disambiguated():
    a = _runnable("df", host="web1")
    b = _runnable("df", host="web2")
    tools, registry = m.build_tools([a, b])
    names = sorted(registry)
    assert "df" in names  # first wins the bare name
    assert any(n != "df" for n in names)  # second host-prefixed
    hosts = {t.host for t in tools}
    assert hosts == {"web1", "web2"}  # both reachable


def test_available_returns_tuple():
    ok, reason = m.available()
    assert isinstance(ok, bool)
    assert ok or reason  # a reason when unavailable
