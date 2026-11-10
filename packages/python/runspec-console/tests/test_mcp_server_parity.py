"""
test_mcp_server_parity.py — the MCP server's assistant parity features:
server instructions, the ``assistant`` + runbook prompts, the context resources,
and ``[mcp_server] tool_mode = "search"`` (search/describe/run meta tools).

Server-side handlers are driven through the SDK's registered request handlers
(no socket); the Bridge side uses the gate test's FakeBridge.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import runspec_console.mcp_server as m
from runspec_console import config as config_mod
from runspec_console import memory
from runspec_console.bridge_mcp_server import _INSTRUCTIONS_MAX, _cap, _prompt_name
from runspec_console.bridge_search import SearchMixin
from tests.test_mcp_server_gate import FakeBridge

# ── server side ─────────────────────────────────────────────────────────────


def _srv(**kw):
    return m.ConsoleMcpServer(
        config_reader=dict,
        tools_provider=lambda: [],
        execute=lambda tool, args: ("", False),
        **kw,
    )


def _handler(server, method):
    return server._request_handlers[method].handler


def test_instructions_come_from_the_provider():
    pytest.importorskip("mcp")
    server = _srv(instructions_provider=lambda: "Use search_runnables first.")
    assert server._build_server().instructions == "Use search_runnables first."


def test_instructions_fall_back_when_the_provider_fails():
    pytest.importorskip("mcp")

    def boom():
        raise RuntimeError("no config")

    built = _srv(instructions_provider=boom)._build_server()
    assert "approval" in built.instructions


def test_instructions_are_rebuilt_per_connection():
    pytest.importorskip("mcp")
    calls = []

    def provider():
        calls.append(1)
        return f"version {len(calls)}"

    server = _srv(instructions_provider=provider)._build_server()
    server.create_initialization_options()
    assert server.instructions == "version 2"


def test_prompts_and_resources_absent_without_providers():
    pytest.importorskip("mcp")
    built = _srv()._build_server()
    assert "prompts/list" not in built._request_handlers
    assert "resources/list" not in built._request_handlers


def test_prompts_list_and_get():
    pytest.importorskip("mcp")
    seen = {}

    def get_prompt(name, args):
        seen["call"] = (name, args)
        if name != "assistant":
            raise KeyError(name)
        return ("desc", f"do {args.get('task')}")

    built = _srv(
        prompts_provider=lambda: [
            {
                "name": "assistant",
                "description": "the assistant",
                "arguments": [{"name": "task", "required": False}],
            },
            {"name": ""},  # skipped
        ],
        get_prompt=get_prompt,
    )._build_server()
    listed = asyncio.run(_handler(built, "prompts/list")(None, None))
    assert [p.name for p in listed.prompts] == ["assistant"]
    assert listed.prompts[0].arguments[0].name == "task"

    import mcp.types as types

    got = asyncio.run(
        _handler(built, "prompts/get")(
            None,
            types.GetPromptRequestParams(name="assistant", arguments={"task": "x"}),
        )
    )
    assert got.messages[0].content.text == "do x"
    assert seen["call"] == ("assistant", {"task": "x"})


@pytest.mark.parametrize("exc", [KeyError("nope"), ValueError("bad value")])
def test_get_prompt_errors_become_invalid_params(exc):
    pytest.importorskip("mcp")
    import mcp.types as types
    from mcp.shared.exceptions import MCPError

    def get_prompt(name, args):
        raise exc

    built = _srv(prompts_provider=lambda: [], get_prompt=get_prompt)._build_server()
    with pytest.raises(MCPError):
        asyncio.run(
            _handler(built, "prompts/get")(
                None, types.GetPromptRequestParams(name="x", arguments={})
            )
        )


def test_resources_list_and_read():
    pytest.importorskip("mcp")
    import mcp.types as types
    from mcp.shared.exceptions import MCPError

    def read(uri):
        if uri != "runspec://context":
            raise KeyError(uri)
        return "## Context"

    built = _srv(
        resources_provider=lambda: [{"uri": "runspec://context", "name": "context"}],
        read_resource=read,
    )._build_server()
    listed = asyncio.run(_handler(built, "resources/list")(None, None))
    assert [str(r.uri) for r in listed.resources] == ["runspec://context"]
    got = asyncio.run(
        _handler(built, "resources/read")(
            None, types.ReadResourceRequestParams(uri="runspec://context")
        )
    )
    assert got.contents[0].text == "## Context"
    with pytest.raises(MCPError):
        asyncio.run(
            _handler(built, "resources/read")(
                None, types.ReadResourceRequestParams(uri="runspec://nope")
            )
        )


def test_search_mode_lists_no_runnable_tools():
    srv = m.ConsoleMcpServer(
        config_reader=dict,
        tools_provider=lambda: [{"name": "df", "host": "local", "args": []}],
        execute=lambda tool, args: ("", False),
    )
    assert srv._build_registry(None)  # all mode: df is a tool
    srv._tool_mode = "search"
    assert srv._build_registry(None) == {}


def test_read_only_builtins_carry_the_hint():
    pytest.importorskip("mcp")
    srv = _srv(
        builtins_provider=lambda: [
            {"name": "search_runnables", "read_only": True},
            {"name": "run_runnable"},
        ],
        execute_builtin=lambda name, args: ("", False),
    )
    result = asyncio.run(_handler(srv._build_server(), "tools/list")(None, None))
    hints = {
        t.name: (t.annotations.read_only_hint if t.annotations else None)
        for t in result.tools
    }
    assert hints == {"search_runnables": True, "run_runnable": None}


# ── config ──────────────────────────────────────────────────────────────────


def test_config_defaults(monkeypatch):
    monkeypatch.setattr(config_mod, "read_config", dict)
    cfg = config_mod.read_mcp_server()
    assert cfg["tool_mode"] == "all"
    assert cfg["confirm_wait"] == 300.0


def test_config_reads_search_mode(monkeypatch):
    monkeypatch.setattr(
        config_mod, "read_config", lambda: {"mcp_server": {"tool_mode": "search"}}
    )
    assert config_mod.read_mcp_server()["tool_mode"] == "search"


def test_config_ignores_unknown_mode(monkeypatch):
    monkeypatch.setattr(
        config_mod, "read_config", lambda: {"mcp_server": {"tool_mode": "lots"}}
    )
    assert config_mod.read_mcp_server()["tool_mode"] == "all"


# ── bridge side ─────────────────────────────────────────────────────────────


_RUNNABLES = [
    {
        "name": "df",
        "host": "web1",
        "group": "linux",
        "description": "disk usage",
        "autonomy": "autonomous",
        "args": [{"name": "path", "type": "str", "required": False}],
    },
    {
        "name": "df",
        "host": "web2",
        "group": "linux",
        "description": "disk usage",
        "autonomy": "autonomous",
        "args": [{"name": "path", "type": "str", "required": False}],
    },
    {
        "name": "cron",
        "host": "local",
        "group": "linux",
        "description": "crontab",
        "autonomy": "confirm",
        "requireCommand": True,
        "args": [],
        "commands": {
            "list": {"name": "list", "autonomy": "autonomous", "args": []},
            "add": {
                "name": "add",
                "autonomy": "confirm",
                "args": [{"name": "schedule", "type": "str", "required": True}],
            },
        },
    },
]


class SearchBridge(FakeBridge):
    """FakeBridge + the search-mode pieces run_runnable resolves through."""

    _coerce_run_args = staticmethod(SearchMixin._coerce_run_args)
    _node_at = staticmethod(SearchMixin._node_at)
    _preflight_validate = SearchMixin._preflight_validate

    def __init__(self, groups=None, **kw):
        super().__init__(**kw)
        self.groups = groups or {}

    def _runnables_for_id(self, rid):
        return [r for r in _RUNNABLES if r["name"] == rid]

    def _runbook_id_hint(self, rid):
        return "Error: runbook" if rid == "restart-web" else ""

    def _groups_map(self):
        return self.groups

    def _search_mode_tools(self):
        return SearchMixin._search_mode_tools(self)

    def _run_search_tool(self, ti):
        return json.dumps({"results": [{"id": "df", "query": ti.get("query")}]})

    def _run_describe_tool(self, ti):
        return "Error: unknown runnable id 'x'" if ti.get("id") == "x" else "{}"


@pytest.fixture
def search_mode(monkeypatch):
    monkeypatch.setattr(
        config_mod,
        "read_mcp_server",
        lambda: {"tool_mode": "search", "confirm_wait": 300},
    )


def test_search_mode_publishes_three_meta_tools(search_mode):
    tools = SearchBridge()._mcp_server_builtins()
    assert [t["name"] for t in tools] == [
        "search_runnables",
        "describe_runnable",
        "run_runnable",
    ]
    assert [bool(t.get("read_only")) for t in tools] == [True, True, False]
    run_props = tools[2]["input_schema"]["properties"]
    assert set(run_props) == {"id", "host", "command", "args"}


def test_all_mode_publishes_no_meta_tools(monkeypatch):
    monkeypatch.setattr(config_mod, "read_mcp_server", lambda: {"tool_mode": "all"})
    assert SearchBridge()._mcp_server_builtins() == []


def test_search_and_describe_run_ungated(search_mode):
    b = SearchBridge()
    text, is_error = b._mcp_server_execute_builtin(
        "search_runnables", {"query": "disk"}
    )
    assert not is_error and json.loads(text)["results"][0]["id"] == "df"
    text, is_error = b._mcp_server_execute_builtin("describe_runnable", {"id": "x"})
    assert is_error and "unknown" in text
    assert b.registered == [] and b.runs == []


def test_run_runnable_runs_on_the_chosen_host(search_mode):
    b = SearchBridge()
    text, is_error = b._mcp_server_execute_builtin(
        "run_runnable", {"id": "df", "host": "web2", "args": {"path": "/var"}}
    )
    assert not is_error, text
    assert b.runs[0][:3] == ("web2", "df", {"path": "/var"})
    assert b.runs[0][4] == "mcp"


def test_run_runnable_needs_a_host_for_a_multi_host_runnable(search_mode):
    b = SearchBridge()
    text, is_error = b._mcp_server_execute_builtin("run_runnable", {"id": "df"})
    assert is_error and "web1, web2" in text
    assert b.runs == []


def test_run_runnable_command_gates_per_command(search_mode):
    b = SearchBridge()
    b._mcp_server_execute_builtin("run_runnable", {"id": "cron", "command": "list"})
    assert b.registered == [] and b.runs[0][3] == ("list",)
    b._mcp_server_execute_builtin(
        "run_runnable",
        {"id": "cron", "command": "add", "args": {"schedule": "@daily"}},
    )
    assert b.registered and b.registered[0][3] == "confirm"
    assert b.runs[1][3] == ("add",) and b.runs[1][2] == {"schedule": "@daily"}


def test_run_runnable_preflight_bounces_before_any_card(search_mode):
    b = SearchBridge()
    text, is_error = b._mcp_server_execute_builtin(
        "run_runnable", {"id": "cron", "command": "add"}
    )
    assert is_error and "schedule" in text
    text, is_error = b._mcp_server_execute_builtin(
        "run_runnable", {"id": "df", "host": "web1", "args": {"nope": 1}}
    )
    assert is_error and "unknown argument" in text
    assert b.runs == [] and b.cards == []


def test_run_runnable_refuses_misplaced_fields(search_mode):
    b = SearchBridge()
    text, is_error = b._mcp_server_execute_builtin(
        "run_runnable", {"id": "df", "host": "web1", "path": "/var"}
    )
    assert is_error and "inside 'args'" in text
    assert b.runs == []


def test_run_runnable_explains_host_groups(search_mode):
    b = SearchBridge(groups={"web": ["web1", "web2", "db1"]})
    text, is_error = b._mcp_server_execute_builtin(
        "run_runnable", {"id": "df", "host": "web"}
    )
    assert is_error and "once for each of web1, web2" in text


def test_run_runnable_unknown_and_runbook_ids(search_mode):
    b = SearchBridge()
    text, is_error = b._mcp_server_execute_builtin("run_runnable", {"id": "nope"})
    assert is_error and "unknown runnable id" in text
    text, is_error = b._mcp_server_execute_builtin(
        "run_runnable", {"id": "restart-web"}
    )
    assert is_error and "runbook-restart-web prompt" in text


def test_run_runnable_flat_runnable_rejects_a_command(search_mode):
    b = SearchBridge()
    text, is_error = b._mcp_server_execute_builtin(
        "run_runnable", {"id": "df", "host": "web1", "command": "x"}
    )
    assert is_error and "no subcommands" in text


def test_confirm_wait_defaults_to_300(monkeypatch):
    monkeypatch.setattr(config_mod, "read_mcp_server", lambda: {})
    assert FakeBridge()._mcp_server_confirm_wait() == 300.0


# instructions / prompts / resources ----------------------------------------


class ContextBridge(SearchBridge):
    system_append = ""

    def get_config(self):
        return {"llm": {"system_append": self.system_append}}

    def _base_system(self):
        return "BASE SYSTEM PROMPT"

    def _now_context(self):
        return "## Current date & timezone\nMonday"

    def _console_context(self):
        return "## Console context\nschedules"

    def _knowledge_header(self):
        return "## Pinned facts\n- web1 is old"


def test_instructions_explain_approval_and_search_mode(search_mode, monkeypatch):
    monkeypatch.setattr(config_mod, "theme_config", lambda: {"brand_name": "Acme"})
    b = ContextBridge(groups={"prod": ["web1", "web2"]})
    b.system_append = "Never reboot on Fridays."
    text = b._mcp_server_instructions()
    assert text.startswith("Acme runs")
    assert "call the same tool again" in text
    assert "search_runnables" in text
    assert "prod (web1, web2)" in text
    assert text.endswith("Never reboot on Fridays.")
    assert len(text) <= _INSTRUCTIONS_MAX


def test_instructions_capped_at_4096(monkeypatch):
    monkeypatch.setattr(config_mod, "read_mcp_server", lambda: {})
    monkeypatch.setattr(config_mod, "theme_config", dict)
    b = ContextBridge()
    b.system_append = "\n".join(f"rule {i} " + "x" * 60 for i in range(200))
    text = b._mcp_server_instructions()
    assert len(text) <= _INSTRUCTIONS_MAX and text.endswith("…")


def test_cap_cuts_at_a_line():
    assert _cap("short", 10) == "short"
    out = _cap("aaaa\nbbbb\ncccc", 12)
    assert out == "aaaa\nbbbb\n…"


def test_prompt_name_is_safe():
    assert _prompt_name("restart web/app") == "runbook-restart-web-app"


@pytest.fixture
def runbooks(monkeypatch):
    rows = [
        {
            "id": "restart-web",
            "title": "Restart the web tier",
            "symptom": "site down",
            "steps": "Restart nginx on {{host}} then check {{url}}.",
            "args": {"host": {"options": ["web1", "web2"]}, "url": {"default": "/"}},
        }
    ]
    monkeypatch.setattr(memory, "load_runbooks", lambda: rows)
    return rows


def test_prompts_list_assistant_and_runbooks(runbooks, monkeypatch):
    monkeypatch.setattr(config_mod, "theme_config", dict)
    prompts = ContextBridge()._mcp_server_prompts()
    assert [p["name"] for p in prompts] == ["assistant", "runbook-restart-web"]
    rb = prompts[1]
    assert rb["title"] == "Restart the web tier" and rb["description"] == "site down"
    assert {a["name"]: a["required"] for a in rb["arguments"]} == {
        "host": True,
        "url": False,
    }


def test_runbook_prompt_renders_values(runbooks):
    desc, text = ContextBridge()._mcp_server_get_prompt(
        "runbook-restart-web", {"host": "web2"}
    )
    assert desc == "site down"
    assert "Restart nginx on web2 then check {{url}}." in text
    assert "Not filled in" not in text  # url has a default, so not required


def test_runbook_prompt_flags_missing_and_bad_values(runbooks):
    _, text = ContextBridge()._mcp_server_get_prompt("runbook-restart-web", {})
    assert "Not filled in: host" in text
    with pytest.raises(ValueError):
        ContextBridge()._mcp_server_get_prompt("runbook-restart-web", {"host": "db9"})
    with pytest.raises(KeyError):
        ContextBridge()._mcp_server_get_prompt("runbook-nope", {})


def test_assistant_prompt_has_system_prompt_context_and_task():
    _, text = ContextBridge()._mcp_server_get_prompt(
        "assistant", {"task": "check disk"}
    )
    assert text.startswith("BASE SYSTEM PROMPT")
    for part in ("Monday", "schedules", "web1 is old"):
        assert part in text
    assert text.endswith("## Task\ncheck disk")


def test_resources_read_context_and_system_prompt():
    b = ContextBridge()
    assert [r["uri"] for r in b._mcp_server_resources()] == [
        "runspec://context",
        "runspec://system-prompt",
    ]
    assert "Pinned facts" in b._mcp_server_read_resource("runspec://context")
    assert b._mcp_server_read_resource("runspec://system-prompt") == (
        "BASE SYSTEM PROMPT"
    )
    with pytest.raises(KeyError):
        b._mcp_server_read_resource("runspec://nope")


def test_save_validates_tool_mode(monkeypatch):
    written = {}
    monkeypatch.setattr(config_mod, "update_config", lambda fn: fn(written))

    class Saver(FakeBridge):
        def __init__(self):
            super().__init__()

            class _Srv:
                def restart(self):
                    pass

                def status(self):
                    return {}

            self._mcp_server = _Srv()

        def _write_mcp_server_discovery(self):
            pass

    Saver().save_mcp_server_config({"enabled": True, "tool_mode": "search"})
    assert written["mcp_server"]["tool_mode"] == "search"
    Saver().save_mcp_server_config({"enabled": True, "tool_mode": "all"})
    assert "tool_mode" not in written["mcp_server"]
    with pytest.raises(ValueError):
        Saver().save_mcp_server_config({"tool_mode": "lots"})


# ── Claude Code plugin ──────────────────────────────────────────────────────


def test_plugin_name_is_valid():
    from runspec_console.claude_plugin import plugin_name

    assert plugin_name("runspec-console") == "runspec-console"
    assert plugin_name("Acme Console!") == "acme-console"
    assert plugin_name("") == "runspec-console"


def test_plugin_render_without_token(tmp_path):
    from runspec_console import claude_plugin

    info = claude_plugin.render(
        tmp_path / "claude-plugin",
        slug="acme-console",
        brand="Acme",
        url="http://127.0.0.1:8765/mcp",
        token_required=False,
        version="1.2.3",
    )
    root = tmp_path / "claude-plugin"
    market = json.loads((root / ".claude-plugin" / "marketplace.json").read_text())
    assert market["name"] == "acme-console"
    assert market["plugins"] == [
        {
            "name": "acme-console",
            "source": "./plugin",
            "description": market["plugins"][0]["description"],
        }
    ]
    manifest = json.loads(
        (root / "plugin" / ".claude-plugin" / "plugin.json").read_text()
    )
    assert manifest["mcpServers"] == {
        "console": {"type": "http", "url": "http://127.0.0.1:8765/mcp"}
    }
    assert "userConfig" not in manifest and manifest["version"] == "1.2.3"
    agent = (root / "plugin" / "agents" / "runspec-operator.md").read_text()
    assert (
        "tools: mcp__plugin_acme-console_console__*, ListMcpResourcesTool, "
        "ReadMcpResourceTool"
    ) in agent
    assert "runspec://system-prompt" in agent and "runspec://context" in agent
    assert info["agent"] == "acme-console:runspec-operator"
    assert info["commands"][1] == "claude plugin install acme-console@acme-console"


def test_plugin_render_with_token_never_writes_it(tmp_path):
    from runspec_console import claude_plugin

    dest = tmp_path / "p"
    (dest / "stale").mkdir(parents=True)  # replaced, not merged
    claude_plugin.render(
        dest,
        slug="runspec-console",
        brand="runspec console",
        url="http://127.0.0.1:9000/mcp",
        token_required=True,
        version="1",
    )
    assert not (dest / "stale").exists()
    manifest = json.loads(
        (dest / "plugin" / ".claude-plugin" / "plugin.json").read_text()
    )
    server = manifest["mcpServers"]["console"]
    assert server["headers"] == {"Authorization": "Bearer ${user_config.token}"}
    token = manifest["userConfig"]["token"]
    assert token["sensitive"] is True and token["required"] is True


def test_bridge_plugin_needs_a_running_server():
    b = ContextBridge()

    class _Down:
        def status(self):
            return {"running": False}

    b._mcp_server = _Down()
    assert b.mcp_server_claude_plugin()["ok"] is False


def test_bridge_plugin_written_to_app_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(config_mod, "app_slug", lambda: "runspec-console")
    monkeypatch.setattr(config_mod, "theme_config", dict)
    monkeypatch.setattr(config_mod, "read_mcp_server", lambda: {"token": "s3cret"})
    b = ContextBridge()

    class _Up:
        def status(self):
            return {"running": True, "url": "http://127.0.0.1:8765/mcp"}

    b._mcp_server = _Up()
    info = b.mcp_server_claude_plugin()
    assert info["ok"] and info["token_required"]
    assert info["path"] == str(tmp_path / "claude-plugin")
    written = "".join(
        p.read_text() for p in (tmp_path / "claude-plugin").rglob("*") if p.is_file()
    )
    assert "s3cret" not in written
