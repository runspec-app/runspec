"""
test_command_tools.py — require-command visibility.

Two surfaces must show that a runnable needs a command:
  1. discovery tags each node with ``requireCommand`` (Forms/Spec views read it);
  2. ``_runnables_to_tools`` exposes a ``command`` enum the agent can pick — and
     marks it required when the spec sets ``require-command`` — so the LLM both
     knows a command is mandatory and has a way to name it.
"""

from __future__ import annotations

import threading
from typing import Any

from runspec_console.bridge import Bridge
from runspec_console.discovery import _spec_to_runnables

# A runnable that requires a command, with one leaf command carrying its own arg.
SPEC: dict[str, Any] = {
    "description": "cluster admin",
    "require-command": True,
    "args": {"region": {"type": "choice", "options": ["us", "eu"]}},
    "commands": {
        "drain": {
            "description": "drain a node",
            "args": {"node": {"type": "str", "required": True}},
        },
        "list": {"description": "list nodes"},
    },
}


# ── discovery surfaces requireCommand ─────────────────────────────────────────


def test_discovery_tags_require_command():
    root = _spec_to_runnables("cluster", SPEC, "localhost", "grp")[0]
    assert root["requireCommand"] is True
    # leaf commands don't require a further command
    assert root["commands"]["drain"]["requireCommand"] is False
    assert root["commands"]["list"]["requireCommand"] is False


def test_discovery_require_command_underscore_key():
    # The CLI emits the normalised underscore form; discovery must accept it too.
    spec = {"require_command": True, "commands": {"a": {}}}
    root = _spec_to_runnables("x", spec, "h", "g")[0]
    assert root["requireCommand"] is True


def test_discovery_no_commands_not_required():
    flat = _spec_to_runnables("flat", {"args": {"x": {"type": "int"}}}, "h", "g")[0]
    assert flat["requireCommand"] is False


# ── tool schema exposes the command (required) ────────────────────────────────


class _ToolBridge:
    # These tests pin the per-host layout (one tool per host__runnable); the dedup
    # default layout has its own coverage in test_tool_dedup.py.
    def __init__(self, runnables: list[dict[str, Any]]):
        self._lock = threading.Lock()
        self._runnables_cache = runnables
        self._credentials: list = []

    def get_config(self):
        return {"llm": {"tool_mode": "per_host"}}

    _runnables_to_tools = Bridge._runnables_to_tools
    _visible_runnables = Bridge._visible_runnables
    _build_input_schema = Bridge._build_input_schema
    _tool_mode = Bridge._tool_mode
    _sanitize_tool_name = staticmethod(Bridge._sanitize_tool_name)
    _dedup_tool_name = Bridge._dedup_tool_name
    _union_scoped_credentials = Bridge._union_scoped_credentials
    _allowed_hosts = Bridge._allowed_hosts
    _host_selector_key = Bridge._host_selector_key
    _declared_arg_name_set = staticmethod(Bridge._declared_arg_name_set)
    _scoped_credentials = Bridge._scoped_credentials
    _scope_matches = staticmethod(Bridge._scope_matches)

    def _reload_credentials(self):
        pass


def test_tool_exposes_credential_id_when_in_scope():
    runnables = _spec_to_runnables(
        "deploy", {"args": {"u": {"type": "str"}}}, "local", "grp"
    )
    b = _ToolBridge(runnables)
    b._credentials = [
        {"id": "svc", "label": "Svc", "scope_hosts": [], "scope_runnables": ["deploy"]}
    ]
    props = b._runnables_to_tools()[0]["input_schema"]["properties"]
    assert props["credential_id"]["enum"] == ["svc"]


def test_tool_no_credential_id_when_out_of_scope():
    runnables = _spec_to_runnables(
        "deploy", {"args": {"u": {"type": "str"}}}, "local", "grp"
    )
    b = _ToolBridge(runnables)
    b._credentials = [{"id": "svc", "scope_runnables": ["something-else"]}]
    assert (
        "credential_id" not in b._runnables_to_tools()[0]["input_schema"]["properties"]
    )


def test_tool_omits_password_args():
    # A password-typed arg must not appear in the agent's tool schema (and must
    # not be required) — the agent never sees or types a secret; a bound
    # credential fills it at run time.
    runnables = _spec_to_runnables(
        "bootstrap",
        {"args": {"password": {"type": "password"}, "user": {"type": "str"}}},
        "local",
        "grp",
    )
    schema = _ToolBridge(runnables)._runnables_to_tools()[0]["input_schema"]
    assert "password" not in schema["properties"]
    assert "password" not in schema.get("required", [])
    assert "user" in schema["properties"]  # non-secret args still surface


def _tool_for(spec: dict[str, Any]) -> dict[str, Any]:
    runnables = _spec_to_runnables("cluster", spec, "local", "grp")
    tools = _ToolBridge(runnables)._runnables_to_tools()
    return next(t for t in tools if t["name"] == "local__cluster")


def test_tool_exposes_command_enum():
    tool = _tool_for(SPEC)
    props = tool["input_schema"]["properties"]
    assert props["command"]["enum"] == ["drain", "list"]
    # both command descriptions are summarised for the agent
    assert "drain a node" in props["command"]["description"]
    assert "list nodes" in props["command"]["description"]


def test_command_required_when_require_command():
    tool = _tool_for(SPEC)
    assert "command" in tool["input_schema"]["required"]
    assert "Required" in tool["input_schema"]["properties"]["command"]["description"]


def test_command_optional_when_not_required():
    spec = {**SPEC, "require-command": False}
    tool = _tool_for(spec)
    assert "command" not in tool["input_schema"].get("required", [])
    assert "Optional" in tool["input_schema"]["properties"]["command"]["description"]


def test_subcommand_own_args_merged_as_optional():
    tool = _tool_for(SPEC)
    props = tool["input_schema"]["properties"]
    # drain's own `node` arg is offered, tagged with its command, but never forced
    # at the top level (it only applies when command == "drain").
    assert "node" in props
    assert "for the 'drain' command" in props["node"]["description"]
    assert "node" not in tool["input_schema"].get("required", [])
    # the global arg is still present
    assert "region" in props


def test_flat_runnable_has_no_command_param():
    runnables = _spec_to_runnables(
        "flush", {"args": {"x": {"type": "int", "default": 1}}}, "local", "grp"
    )
    tool = _ToolBridge(runnables)._runnables_to_tools()[0]
    assert "command" not in tool["input_schema"]["properties"]


# ── runnables allowlist scopes the agent's tools ──────────────────────────────


def _allowlist_tools(allow):
    runnables = [
        {"host": "web01", "name": "check-memory", "args": []},
        {"host": "web01", "name": "restart-service", "args": []},
        {"host": "db01", "name": "check-memory", "args": []},
    ]
    tools = _ToolBridge(runnables)._runnables_to_tools(allow=allow)
    return {t["name"] for t in tools}


def test_allowlist_none_returns_every_tool():
    # No allowlist → unrestricted (the agent --> any runnable behaviour).
    assert _allowlist_tools(None) == {
        "web01__check-memory",
        "web01__restart-service",
        "db01__check-memory",
    }


def test_allowlist_matches_qualified_and_bare_names():
    # A qualified "host__name" pins one host; a bare name allows it on every host.
    assert _allowlist_tools(["web01__restart-service", "check-memory"]) == {
        "web01__restart-service",
        "web01__check-memory",
        "db01__check-memory",
    }


def test_allowlist_unknown_name_yields_no_tools():
    assert _allowlist_tools(["nope"]) == set()


# ── execution threads the chosen command ──────────────────────────────────────


def test_run_tool_sync_threads_command(monkeypatch):
    """The agent's ``command`` input becomes the run's command path, and is
    stripped from the args so it isn't emitted as a ``--command`` flag."""

    captured: dict[str, Any] = {}

    def fake_run_local(rp, runnable, args, command_path, on_line, on_done, **kw):
        captured["args"] = args
        captured["command_path"] = command_path
        captured["positionals"] = kw.get("positionals")
        on_done(0, 5)

    monkeypatch.setattr("runspec_console.executor.run_local", fake_run_local)
    monkeypatch.setattr(
        "runspec_console.bridge_utils._paths", lambda entry: ["/fake/bin/runspec"]
    )
    monkeypatch.setattr("runspec_console.hosts.venv_name", lambda p: "grp")

    runnables = _spec_to_runnables("cluster", SPEC, "local", "grp")

    class _B:
        _lock = threading.Lock()
        _runnables_cache = runnables

        def _host_entry(self, host):
            return {"ssh": None}

        # Stubbed: the real _become_params imports the runspec core package, which
        # the console test suite runs without. Escalation isn't under test here.
        def _become_params(self, cached, hostname):
            return "", "sudo", None

        _positionals_for = staticmethod(Bridge._positionals_for)
        _secret_arg_names = staticmethod(Bridge._secret_arg_names)
        _run_tool_sync = Bridge._run_tool_sync
        _tool_output_cap = Bridge._tool_output_cap
        # Routing helpers _run_tool_sync now delegates to (host__name fallback
        # resolves these per-host tool names under either layout).
        _resolve_tool_target = Bridge._resolve_tool_target
        _tool_mode = Bridge._tool_mode
        _dedup_tool_name = Bridge._dedup_tool_name
        _sanitize_tool_name = staticmethod(Bridge._sanitize_tool_name)
        _host_selector_key = Bridge._host_selector_key
        _declared_arg_name_set = staticmethod(Bridge._declared_arg_name_set)

        def get_config(self):
            return {}

        # Credential resolution is a no-op here (no credentials configured).
        _credentials: list = []
        _apply_credentials = Bridge._apply_credentials
        _credential_env = Bridge._credential_env
        _scoped_credentials = Bridge._scoped_credentials
        _scope_matches = staticmethod(Bridge._scope_matches)
        _credential_entry = Bridge._credential_entry
        _declared_arg_names = staticmethod(Bridge._declared_arg_names)

        def _reload_credentials(self):
            pass

    _B()._run_tool_sync(
        "local__cluster", {"command": "drain", "node": "n1", "region": "us"}
    )
    assert captured["command_path"] == ["drain"]
    assert "command" not in captured["args"]
    assert captured["args"]["node"] == "n1"
    assert captured["args"]["region"] == "us"


def test_run_tool_sync_no_command_is_top_level(monkeypatch):
    """No ``command`` input → empty command path (runs at the top level)."""

    captured: dict[str, Any] = {}

    def fake_run_local(rp, runnable, args, command_path, on_line, on_done, **kw):
        captured["command_path"] = command_path
        on_done(0, 1)

    monkeypatch.setattr("runspec_console.executor.run_local", fake_run_local)
    monkeypatch.setattr(
        "runspec_console.bridge_utils._paths", lambda entry: ["/fake/bin/runspec"]
    )
    monkeypatch.setattr("runspec_console.hosts.venv_name", lambda p: "grp")

    runnables = _spec_to_runnables("flush", {"args": {}}, "local", "grp")

    class _B:
        _lock = threading.Lock()
        _runnables_cache = runnables

        def _host_entry(self, host):
            return {"ssh": None}

        # Stubbed: the real _become_params imports the runspec core package, which
        # the console test suite runs without. Escalation isn't under test here.
        def _become_params(self, cached, hostname):
            return "", "sudo", None

        _positionals_for = staticmethod(Bridge._positionals_for)
        _secret_arg_names = staticmethod(Bridge._secret_arg_names)
        _run_tool_sync = Bridge._run_tool_sync
        _tool_output_cap = Bridge._tool_output_cap
        # Routing helpers _run_tool_sync now delegates to (host__name fallback
        # resolves these per-host tool names under either layout).
        _resolve_tool_target = Bridge._resolve_tool_target
        _tool_mode = Bridge._tool_mode
        _dedup_tool_name = Bridge._dedup_tool_name
        _sanitize_tool_name = staticmethod(Bridge._sanitize_tool_name)
        _host_selector_key = Bridge._host_selector_key
        _declared_arg_name_set = staticmethod(Bridge._declared_arg_name_set)

        def get_config(self):
            return {}

        # Credential resolution is a no-op here (no credentials configured).
        _credentials: list = []
        _apply_credentials = Bridge._apply_credentials
        _credential_env = Bridge._credential_env
        _scoped_credentials = Bridge._scoped_credentials
        _scope_matches = staticmethod(Bridge._scope_matches)
        _credential_entry = Bridge._credential_entry
        _declared_arg_names = staticmethod(Bridge._declared_arg_names)

        def _reload_credentials(self):
            pass

    _B()._run_tool_sync("local__flush", {})
    assert captured["command_path"] == []
