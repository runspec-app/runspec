"""
test_mcp_server.py — pure-helper tests for the console-as-MCP-server module
(schema flattening, autonomy escalation, name dedup). No SDK / socket needed.
"""

from __future__ import annotations

import pytest

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


def test_numeric_range_becomes_minimum_maximum():
    r = _runnable(
        "svc",
        autonomy="autonomous",
        args=[{"name": "workers", "type": "int", "default": 4, "range": [1, 32]}],
    )
    prop = m.build_tools([r])[0][0].input_schema["properties"]["workers"]
    assert prop["minimum"] == 1 and prop["maximum"] == 32


def test_string_range_is_coerced_not_left_as_string():
    # A mis-authored range = ["1", "9"] (or a range on a non-numeric arg) must not
    # emit string minimum/maximum — that is invalid JSON Schema draft 2020-12, and
    # the Anthropic API rejects the WHOLE published tool list over one bad schema
    # (`tools.N.custom.input_schema: ... must match JSON Schema draft 2020-12`),
    # so a connecting agent (Claude Code) sees no tools at all. Numeric strings are
    # coerced so the bound still applies.
    r = _runnable(
        "svc",
        autonomy="autonomous",
        args=[{"name": "level", "type": "int", "range": ["1", "9"]}],
    )
    prop = m.build_tools([r])[0][0].input_schema["properties"]["level"]
    assert prop["minimum"] == 1 and prop["maximum"] == 9
    assert isinstance(prop["minimum"], int) and isinstance(prop["maximum"], int)


def test_schema_invalidity_flags_string_bounds_and_bad_pattern():
    # Delegates to the stdlib runspec.schemacheck (core is a hard dep, so no extra
    # needed) — catches the same constructs the Anthropic API rejects: string
    # numeric bounds, an invalid regex pattern, a draft-04-style tuple `items`.
    # A valid schema returns "".
    assert m.schema_invalidity({"type": "object", "properties": {}}) == ""
    assert m.schema_invalidity(
        {"type": "object", "properties": {"n": {"type": "integer", "minimum": "9"}}}
    )
    assert m.schema_invalidity(
        {"type": "object", "properties": {"x": {"type": "string", "pattern": "[a-z"}}}
    )
    assert m.schema_invalidity(
        {"type": "object", "properties": {"x": {"type": "array", "items": [{}]}}}
    )


def test_drop_invalid_tools_partitions_and_records(caplog):
    # The publish-time backstop: an invalid tool schema is dropped (and named, both
    # in a warning AND in the returned records the status surfaces) so one bad
    # runnable can't 400 the WHOLE published tool list.
    import logging

    candidates = [
        ("ok", "d", {"type": "object", "properties": {}}),
        (
            "bad",
            "d",
            {
                "type": "object",
                "properties": {"n": {"type": "integer", "minimum": "9"}},
            },
        ),
    ]
    with caplog.at_level(logging.WARNING):
        valid, invalid = m.drop_invalid_tools(candidates)
    assert [c[0] for c in valid] == ["ok"]
    assert invalid == [{"name": "bad", "reason": invalid[0]["reason"]}]
    assert "bad" in invalid[0]["reason"] or "9" in invalid[0]["reason"]
    assert "bad" in caplog.text and "draft 2020-12" in caplog.text


def test_non_numeric_range_drops_the_bounds():
    # A range whose bounds aren't numbers at all can't constrain a schema; drop
    # them rather than emit an invalid one that breaks the whole tool list.
    r = _runnable(
        "svc",
        autonomy="autonomous",
        args=[{"name": "tag", "type": "str", "range": ["a", "z"]}],
    )
    prop = m.build_tools([r])[0][0].input_schema["properties"]["tag"]
    assert "minimum" not in prop and "maximum" not in prop


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


def test_leaf_names_do_not_depend_on_discovery_order():
    # The cache lists hosts in the order their discovery finished. Tool names (what
    # Claude Code keys "don't ask again" on) must be the same whichever came first.
    local = _runnable("which", host="local")
    web = _runnable("which", host="web-01")
    db = _runnable("which", host="db-01")
    want = {"which": "local", "db-01__which": "db-01", "web-01__which": "web-01"}
    for order in ([local, web, db], [web, db, local], [db, local, web]):
        _, registry = m.build_tools(order)
        assert {n: t.host for n, t in registry.items()} == want


def test_runnable_style_host_options_are_stable():
    a = _runnable("which", host="web-01")
    b = _runnable("which", host="local")
    for order in ([a, b], [b, a]):
        tools, _ = m.build_tools(order, style="runnable")
        assert tools[0].host_options == ["local", "web-01"]


def test_runs_unattended_only_when_every_call_is_autonomous():
    (auto,), _ = m.build_tools([_runnable("df", autonomy="autonomous")])
    (gated,), _ = m.build_tools([_runnable("rm", autonomy="confirm")])
    assert auto.runs_unattended() and not gated.runs_unattended()
    mixed = _runnable(
        "cron",
        autonomy="autonomous",
        requireCommand=True,
        commands={
            "list": {"name": "list", "autonomy": "autonomous", "args": []},
            "add": {"name": "add", "autonomy": "confirm", "args": []},
        },
    )
    (tool,), _ = m.build_tools([mixed], style="runnable")
    assert not tool.runs_unattended()  # one confirm verb gates the whole tool
    leaves, _ = m.build_tools([mixed])
    assert {t.name: t.runs_unattended() for t in leaves} == {
        "cron_list": True,
        "cron_add": False,
    }


@pytest.mark.parametrize(
    ("plan_mode_autonomous", "want"),
    [(False, {"df": None, "rm": None}), (True, {"df": True, "rm": None})],
)
def test_list_tools_read_only_hint_is_opt_in(plan_mode_autonomous, want):
    # Claude Code's plan mode asks on every call, saved allow rules included,
    # unless the tool carries readOnlyHint. Autonomous isn't read-only, so the
    # hint is published only with [mcp_server] plan_mode_autonomous.
    pytest.importorskip("mcp")
    import asyncio

    srv = m.ConsoleMcpServer(
        config_reader=dict,
        tools_provider=lambda: [
            _runnable("df", autonomy="autonomous"),
            _runnable("rm", autonomy="confirm"),
        ],
        execute=lambda tool, args: ("", False),
    )
    srv._plan_mode_autonomous = plan_mode_autonomous  # as start() reads it
    handler = srv._build_server()._request_handlers["tools/list"].handler
    result = asyncio.run(handler(None, None))
    hints = {
        t.name: (t.annotations.read_only_hint if t.annotations else None)
        for t in result.tools
    }
    assert hints == want


def test_available_returns_tuple():
    ok, reason = m.available()
    assert isinstance(ok, bool)
    assert ok or reason  # a reason when unavailable


def _server(brand_name=None):
    return m.ConsoleMcpServer(
        config_reader=lambda: {},
        tools_provider=lambda: [],
        execute=lambda tool, args: ("", False),
        brand_name=brand_name,
    )


def test_drain_loop_cancels_pending_tasks_and_closes():
    # The shutdown-noise fix: a stopped server's background loop must be drained +
    # closed so pending tasks aren't GC'd while live ("Task was destroyed but it is
    # pending!"). _drain_loop cancels everything and closes the loop.
    import asyncio

    loop = asyncio.new_event_loop()

    async def _never():
        await asyncio.sleep(3600)

    task = loop.create_task(_never())  # scheduled but never run → pending
    m._drain_loop(loop)
    assert task.done()  # cancelled
    assert loop.is_closed()


def test_drain_loop_tolerates_empty_and_double_close():
    import asyncio

    loop = asyncio.new_event_loop()
    m._drain_loop(loop)  # no tasks
    assert loop.is_closed()
    m._drain_loop(loop)  # already closed — must not raise


def test_shutdown_is_noop_without_a_running_thread():
    # Never started (no thread) → returns immediately, no error.
    _server().shutdown(timeout=0.1)


def test_shutdown_signals_uvicorn_and_joins():
    import threading

    srv = _server()

    class _Uv:
        should_exit = False

    srv._uvicorn = _Uv()
    joined: dict = {}

    class _FakeThread:
        def is_alive(self):
            return True

        def join(self, timeout=None):
            joined["timeout"] = timeout

    srv._thread = _FakeThread()  # type: ignore[assignment]
    srv.shutdown(timeout=1.5)
    assert srv._uvicorn.should_exit is True  # stop() signalled uvicorn
    assert joined["timeout"] == 1.5  # …and we joined the loop thread, bounded
    assert isinstance(threading.current_thread(), threading.Thread)  # sanity


def test_resolve_brand_name_defaults_to_runspec_console():
    # no resolver, empty brand, and a failing resolver all fall back
    assert _server()._resolve_brand_name() == "runspec-console"
    assert _server(brand_name=lambda: "")._resolve_brand_name() == "runspec-console"
    assert _server(brand_name=lambda: "  ")._resolve_brand_name() == "runspec-console"

    def boom() -> str:
        raise RuntimeError("nope")

    assert _server(brand_name=boom)._resolve_brand_name() == "runspec-console"


def test_resolve_brand_name_uses_white_label_brand():
    assert _server(brand_name=lambda: "Acme Ops")._resolve_brand_name() == "Acme Ops"
    # whitespace is trimmed
    assert _server(brand_name=lambda: " Acme ")._resolve_brand_name() == "Acme"


# ── built-in agent tools published alongside runnables ───────────────────────────


def test_builtin_schemas_empty_without_provider():
    # default server (no builtins_provider) publishes no built-ins
    assert _server()._builtin_schemas() == []


def _server_with_builtins():
    return m.ConsoleMcpServer(
        config_reader=lambda: {},
        tools_provider=lambda: [
            _runnable("greet", host="local", autonomy="autonomous")
        ],
        execute=lambda tool, args: ("", False),
        builtins_provider=lambda: [
            {
                "name": "console_status",
                "description": "status",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        execute_builtin=lambda name, args: (f"ran {name}", False),
    )


def test_resolve_call_target_routes_builtin_runnable_unknown():
    srv = _server_with_builtins()
    # seed state as on_list_tools would
    srv._state.registry = m.build_tools(
        [_runnable("greet", host="local", autonomy="autonomous")], style="runnable"
    )[1]
    srv._state.builtin_names = {b["name"] for b in srv._builtin_schemas()}
    assert srv._resolve_call_target("console_status")[0] == "builtin"
    kind, tool = srv._resolve_call_target("greet")
    assert kind == "runnable" and tool is not None and tool.name == "greet"
    assert srv._resolve_call_target("nope")[0] == "unknown"


def test_resolve_call_target_rebuilds_on_cold_miss():
    # a call arriving before any tools/list still resolves (rebuilds once)
    srv = _server_with_builtins()
    assert srv._state.builtin_names == set()  # nothing listed yet
    assert srv._resolve_call_target("console_status")[0] == "builtin"
    assert srv._resolve_call_target("greet")[0] == "runnable"


# ── runnable (collapsed) tool style ──────────────────────────────────────────────


def _greeter(require=True):
    """A multi-verb runnable: greeter { hello (autonomous, needs name), bye (confirm) }
    plus a top-level `verbose` flag."""
    hello = _runnable(
        "hello",
        host="local",
        autonomy="autonomous",
        args=[{"name": "name", "type": "str", "required": True}],
    )
    bye = _runnable("bye", host="local", autonomy="confirm")
    return _runnable(
        "greeter",
        host="local",
        requireCommand=require,
        args=[{"name": "verbose", "type": "flag", "required": False, "default": False}],
        commands={"hello": hello, "bye": bye},
    )


def test_runnable_style_one_tool_with_command_enum():
    tools, registry = m.build_tools([_greeter()], style="runnable")
    # one tool for the whole runnable — not one per verb
    assert len(tools) == 1
    t = tools[0]
    assert t.name == "greeter" and t.runnable == "greeter" and t.command_path == []
    assert sorted(t.command_options) == ["bye", "hello"]
    assert t.command_required is True
    props = t.input_schema["properties"]
    assert "command" in props and sorted(props["command"]["enum"]) == ["bye", "hello"]
    assert "command" in t.input_schema["required"]
    assert "verbose" in props  # the runnable's own top-level arg is still typed
    # per-verb autonomy is preserved for call-time gating
    assert t.command_autonomy["hello"] == "autonomous"
    assert t.command_autonomy["bye"] == "confirm"


def test_runnable_style_resolve_call_picks_command_and_autonomy():
    t = m.build_tools([_greeter()], style="runnable")[0][0]
    host, path, level, args, err = t.resolve_call({"command": "hello", "name": "x"})
    assert err == ""
    assert host == "local" and path == ["hello"] and level == "autonomous"
    assert args == {"name": "x"}  # `command` is popped before running


def test_runnable_style_missing_and_unknown_command_error():
    t = m.build_tools([_greeter()], style="runnable")[0][0]
    *_, err_missing = t.resolve_call({})
    assert "requires a command" in err_missing
    *_, err_unknown = t.resolve_call({"command": "nope"})
    assert "unknown command" in err_unknown


def test_runnable_style_optional_command_root_is_callable():
    # requireCommand=False → the runnable itself is callable (empty command)
    t = m.build_tools([_greeter(require=False)], style="runnable")[0][0]
    assert t.command_required is False
    assert "" in t.command_autonomy
    host, path, level, args, err = t.resolve_call({})
    assert err == "" and path == [] and "command" not in args


def test_runnable_style_default_autonomy_floors_each_command():
    t = m.build_tools([_greeter()], style="runnable", default_autonomy="confirm")[0][0]
    # the floor lifts the autonomous verb to confirm too (most-restrictive wins)
    assert t.command_autonomy["hello"] == "confirm"
    assert t.command_autonomy["bye"] == "confirm"


def test_runnable_style_host_selector_renamed_when_runnable_has_host_arg():
    # A runnable that declares its own `host` arg, discovered on >1 host, must NOT
    # collide with the injected fleet-host selector: colliding overwrote the real
    # arg AND put "host" in `required` twice — invalid draft 2020-12
    # (`["host","host"] has non-unique elements`), which 400s the whole tool list.
    a = _runnable(
        "cert",
        host="web1",
        autonomy="autonomous",
        args=[{"name": "host", "type": "str", "required": True}],
    )
    b = _runnable(
        "cert",
        host="web2",
        autonomy="autonomous",
        args=[{"name": "host", "type": "str", "required": True}],
    )
    t = m.build_tools([a, b], style="runnable")[0][0]
    props = t.input_schema["properties"]
    # the runnable's own `host` arg is preserved (a plain string, no fleet enum)…
    assert "enum" not in props["host"]
    # …and the fleet selector lives under a renamed, non-colliding key.
    assert t.host_key == "_host"
    assert sorted(props["_host"]["enum"]) == ["web1", "web2"]
    # `required` has no duplicate → the schema is valid draft 2020-12.
    req = t.input_schema.get("required", [])
    assert len(req) == len(set(req))
    assert m.schema_invalidity(t.input_schema) == ""
    # resolve_call reads the fleet target from `_host` and leaves the real arg.
    host, _path, _lvl, args, err = t.resolve_call(
        {"_host": "web2", "host": "example.com"}
    )
    assert err == "" and host == "web2" and args == {"host": "example.com"}


def test_runnable_style_command_selector_renamed_when_runnable_has_command_arg():
    # Same collision for a runnable that declares its own `command` arg + subcommands.
    child = _runnable("run", host="local", autonomy="confirm")
    parent = _runnable(
        "tool",
        host="local",
        requireCommand=True,
        args=[{"name": "command", "type": "str", "required": True}],
        commands={"run": child},
    )
    t = m.build_tools([parent], style="runnable")[0][0]
    assert t.command_key == "_command"
    assert "command" in t.input_schema["properties"]  # the real arg survives
    req = t.input_schema.get("required", [])
    assert len(req) == len(set(req))
    assert m.schema_invalidity(t.input_schema) == ""
    _host, path, _lvl, args, err = t.resolve_call({"_command": "run", "command": "x"})
    assert err == "" and path == ["run"] and args == {"command": "x"}


def test_leaf_style_resolve_call_is_passthrough():
    # a plain (no-subcommand) runnable in leaf style: resolve_call returns its fixed
    # host/command_path/autonomy unchanged, args untouched.
    tool = m.build_tools(
        [_runnable("greet", host="local", autonomy="autonomous")], style="leaf"
    )[0][0]
    host, path, level, args, err = tool.resolve_call({"x": 1})
    assert err == "" and host == "local" and path == [] and level == "autonomous"
    assert args == {"x": 1}


def test_runnable_style_collapses_count_vs_leaf():
    # the whole point: N verbs → 1 tool in runnable style, N (+parent) in leaf style
    leaf, _ = m.build_tools([_greeter()], style="leaf")
    runnable, _ = m.build_tools([_greeter()], style="runnable")
    assert (
        len(leaf) == 2 and len(runnable) == 1
    )  # greeter_hello + greeter_bye vs greeter


# ── cross-host dedup (runnable style collapses the host dimension too) ────────────


def test_runnable_style_dedups_same_runnable_across_hosts():
    # the reported bug: 5 hosts × a runnable should be ONE tool, not five.
    hosts = ["web1", "web2", "web3"]
    entries = [
        _runnable("df", host=h, autonomy="autonomous", group="linux") for h in hosts
    ]
    tools, registry = m.build_tools(entries, style="runnable")
    assert len(tools) == 1  # deduped across hosts
    t = tools[0]
    assert t.name == "df"
    assert t.host_options == hosts and t.host_required is True
    # a `host` enum (required) is injected so the agent picks the target
    assert t.input_schema["properties"]["host"]["enum"] == hosts
    assert "host" in t.input_schema["required"]


def test_runnable_style_leaf_keeps_per_host():
    # contrast: leaf style keeps one tool per host (serve-identical)
    entries = [_runnable("df", host=h, group="linux") for h in ["web1", "web2"]]
    leaf, _ = m.build_tools(entries, style="leaf")
    assert len(leaf) == 2 and {t.host for t in leaf} == {"web1", "web2"}


def test_runnable_style_host_arg_resolves_and_is_required_when_multi():
    entries = [_runnable("df", host=h, autonomy="autonomous") for h in ["web1", "web2"]]
    t = m.build_tools(entries, style="runnable")[0][0]
    # no host → error naming the options
    *_, err = t.resolve_call({})
    assert "multiple hosts" in err
    # a valid host resolves; the `host` key is popped from args
    host, path, level, args, err2 = t.resolve_call({"host": "web2"})
    assert err2 == "" and host == "web2" and "host" not in args
    # an unknown host errors
    *_, err3 = t.resolve_call({"host": "nope"})
    assert "unknown host" in err3


def test_runnable_style_single_host_has_no_host_arg():
    t = m.build_tools([_runnable("df", host="web1")], style="runnable")[0][0]
    assert t.host_options == [] and t.host_required is False
    assert "host" not in t.input_schema.get("properties", {})
    host, *_ = t.resolve_call({})
    assert host == "web1"


def test_runnable_style_dedups_across_venvs_no_venv_prefix():
    # the reported bug: a cross-platform runnable ships in the console's own venv
    # (local, group=the venv name) AND on remote hosts (another group) — it must be
    # ONE tool spanning both, not `which` + `console__which`.
    entries = [
        _runnable("which", host="local", group="console", autonomy="autonomous"),
        _runnable("which", host="web1", group="linux", autonomy="autonomous"),
        _runnable("which", host="web2", group="linux", autonomy="autonomous"),
    ]
    tools, registry = m.build_tools(entries, style="runnable")
    assert len(tools) == 1  # collapsed across venvs + hosts
    t = tools[0]
    assert t.name == "which"  # no `console__` / `linux__` prefix
    assert set(registry) == {"which"}
    assert t.host_options == ["local", "web1", "web2"]  # every host, incl. local
    assert t.group == ""  # no pinned venv — resolved per host at run time
    # picking the local host resolves it; the venv is chosen by the executor
    host, _path, _lvl, _args, err = t.resolve_call({"host": "local"})
    assert err == "" and host == "local"


# ── runnable-style per-command arg detail (0.280.5) ──────────────────────────────


def test_runnable_style_advertises_per_command_args():
    # The reported gap: in `runnable` style a subcommand runnable's tool schema was
    # just the globals + a `command` enum — the model couldn't see that `hello`
    # takes `name`, so it dropped the arg. Now each verb's own args are folded into
    # `properties` (optional, tagged with the command) and the description names them.
    t = m.build_tools([_greeter()], style="runnable")[0][0]
    props = t.input_schema["properties"]
    # `hello`'s own arg is now discoverable…
    assert "name" in props
    assert "(command: hello)" in props["name"]["description"]
    # …but stays OPTIONAL at the top level (it's command-scoped, not global).
    assert "name" not in t.input_schema.get("required", [])
    # globals + selector are still present and unchanged
    assert "verbose" in props and "command" in props
    # the description spells out which verb takes what
    assert "Set `command`" in t.description
    assert "hello — name (required)" in t.description
    assert "bye — (no arguments)" in t.description
    # still valid draft 2020-12 (no duplicate required, real schema)
    assert m.schema_invalidity(t.input_schema) == ""


def test_runnable_style_flat_runnable_has_no_command_block():
    # A runnable with no subcommands is unaffected: all its args are already in the
    # schema, so there's nothing to fold and no "Set `command`" block appears.
    flat = _runnable(
        "echo",
        host="local",
        description="Echo a value.",
        args=[{"name": "value", "type": "str", "required": True}],
    )
    t = m.build_tools([flat], style="runnable")[0][0]
    assert "value" in t.input_schema["properties"]
    assert "Set `command`" not in t.description
    assert t.description == "Echo a value."


def test_runnable_style_shared_arg_lists_both_commands():
    # An arg two verbs share is folded once, its description naming both commands.
    a = _runnable(
        "add", host="local", args=[{"name": "path", "type": "str", "required": True}]
    )
    b = _runnable(
        "rm", host="local", args=[{"name": "path", "type": "str", "required": True}]
    )
    parent = _runnable(
        "fsx", host="local", requireCommand=True, commands={"add": a, "rm": b}
    )
    t = m.build_tools([parent], style="runnable")[0][0]
    desc = t.input_schema["properties"]["path"]["description"]
    assert "add" in desc and "rm" in desc


def test_runnable_style_command_arg_does_not_shadow_global():
    # A subcommand arg that shares a name with a real global must NOT overwrite the
    # global's property (first-seen/global wins); only the description is unaffected.
    child = _runnable(
        "go",
        host="local",
        args=[
            {"name": "verbose", "type": "str", "required": True}
        ],  # clashes w/ global flag
    )
    parent = _runnable(
        "gr",
        host="local",
        requireCommand=True,
        args=[{"name": "verbose", "type": "flag", "required": False, "default": False}],
        commands={"go": child},
    )
    t = m.build_tools([parent], style="runnable")[0][0]
    # the global `verbose` (boolean flag) is preserved, not replaced by the str arg
    assert t.input_schema["properties"]["verbose"]["type"] == "boolean"


# ── shutdown-noise log filter (0.280.5) ──────────────────────────────────────────


def _record(msg, exc=None):
    import logging

    return logging.LogRecord(
        name="uvicorn.error",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=(type(exc), exc, None) if exc is not None else None,
    )


def test_shutdown_noise_filter_drops_only_while_stopping():
    import asyncio

    stopping = {"v": False}
    flt = m._ShutdownNoiseFilter(lambda: stopping["v"])

    cancel_line = _record(
        "Cancel 1 running task(s), timeout graceful shutdown exceeded"
    )
    asgi_line = _record(
        "Exception in ASGI application",
        exc=asyncio.CancelledError(
            "Task cancelled, timeout graceful shutdown exceeded"
        ),
    )
    # Not stopping → everything passes (a real ASGI error is never hidden).
    assert flt.filter(cancel_line) is True
    assert flt.filter(asgi_line) is True

    # Stopping → the two expected force-cancel records are dropped…
    stopping["v"] = True
    assert flt.filter(cancel_line) is False
    assert flt.filter(asgi_line) is False
    # …but an unrelated error still logs, even mid-stop.
    assert flt.filter(_record("something actually broke")) is True
    # …and an ASGI record whose cause is NOT a cancellation still logs.
    assert (
        flt.filter(_record("Exception in ASGI application", exc=ValueError("x")))
        is True
    )


def test_stop_sets_stopping_flag():
    srv = _server()
    assert srv._stopping is False
    srv.stop()  # no uvicorn/thread — just flips the flag + marks not-running
    assert srv._stopping is True
