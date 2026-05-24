"""
Tests for runspec serve — MCP stdio server.

Covers arg translation, protocol handlers, dispatch routing, and
script discovery. Subprocess execution is tested via mocking.
"""

from __future__ import annotations

import pytest

from runspec.serve import (
    MCP_PROTOCOL_VERSION,
    _args_to_argv,
    _args_to_runspec_env,
    _dispatch,
    _expand_tools,
    _find_script,
    _handle_initialize,
    _handle_tools_call,
    _handle_tools_list,
    _is_serve_match,
    _serve_context,
    _server_name,
    _validate_serve,
    serve,
)

# ── _args_to_argv ─────────────────────────────────────────────────────────────


def test_argv_str_arg():
    assert _args_to_argv({"env": "prod"}, {"env": {"type": "str"}}) == ["--env", "prod"]


def test_argv_int_arg():
    assert _args_to_argv({"workers": 4}, {"workers": {"type": "int"}}) == ["--workers", "4"]


def test_argv_flag_true():
    assert _args_to_argv({"dry-run": True}, {"dry-run": {"type": "flag"}}) == ["--dry-run"]


def test_argv_flag_false_omitted():
    assert _args_to_argv({"dry-run": False}, {"dry-run": {"type": "flag"}}) == []


def test_argv_missing_arg_omitted():
    assert _args_to_argv({}, {"env": {"type": "str"}}) == []


def test_argv_multiple():
    result = _args_to_argv({"tag": ["a", "b", "c"]}, {"tag": {"type": "str", "multiple": True}})
    assert result == ["--tag", "a", "--tag", "b", "--tag", "c"]


def test_argv_hyphen_name():
    result = _args_to_argv({"api-key": "secret"}, {"api-key": {"type": "str"}})
    assert result == ["--api-key", "secret"]


def test_argv_underscore_form_accepted():
    # Caller passes underscore form, spec has hyphen form
    result = _args_to_argv({"dry_run": True}, {"dry-run": {"type": "flag"}})
    assert result == ["--dry-run"]


def test_argv_ordering_follows_spec():
    specs = {"env": {"type": "str"}, "workers": {"type": "int"}, "dry-run": {"type": "flag"}}
    result = _args_to_argv({"workers": 2, "env": "dev", "dry-run": True}, specs)
    assert result == ["--env", "dev", "--workers", "2", "--dry-run"]


def test_argv_choice_arg():
    result = _args_to_argv({"format": "json"}, {"format": {"type": "choice", "options": ["json", "csv"]}})
    assert result == ["--format", "json"]


# ── _args_to_runspec_env ──────────────────────────────────────────────────────


def test_runspec_env_explicit_args_injected():
    specs = {"quality": {"type": "int", "default": 7}}
    result = _args_to_runspec_env({"quality": 95}, specs)
    assert result == {"RUNSPEC_ARG_QUALITY": "95"}


def test_runspec_env_spec_defaults_not_injected():
    """Spec defaults must NOT be injected — they would overwrite RUNSPEC_ARG_* already in os.environ."""
    specs = {"quality": {"type": "int", "default": 7}}
    result = _args_to_runspec_env({}, specs)
    assert "RUNSPEC_ARG_QUALITY" not in result


def test_runspec_env_flag_encoding():
    specs = {"delete": {"type": "flag", "default": False}}
    assert _args_to_runspec_env({"delete": True}, specs) == {"RUNSPEC_ARG_DELETE": "1"}
    assert _args_to_runspec_env({"delete": False}, specs) == {"RUNSPEC_ARG_DELETE": "0"}


def test_runspec_env_hyphen_normalised():
    specs = {"dry-run": {"type": "flag"}}
    result = _args_to_runspec_env({"dry-run": True}, specs)
    assert result == {"RUNSPEC_ARG_DRY_RUN": "1"}


# ── _handle_initialize ────────────────────────────────────────────────────────


def test_initialize_shape():
    resp = _handle_initialize(1, "analytics-pipeline")
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    result = resp["result"]
    assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "analytics-pipeline"
    assert "tools" in result["capabilities"]


def test_initialize_version_string():
    resp = _handle_initialize(42, "my-env")
    assert resp["result"]["protocolVersion"] == "2024-11-05"


# ── _handle_tools_list ────────────────────────────────────────────────────────


def test_tools_list_empty():
    resp = _handle_tools_list(2, {})
    assert resp["result"]["tools"] == []


def test_tools_list_returns_tool_schemas():
    tools = {
        "deploy": {"name": "deploy", "description": "Deploy", "inputSchema": {"type": "object", "properties": {}}},
    }
    resp = _handle_tools_list(2, tools)
    assert len(resp["result"]["tools"]) == 1
    assert resp["result"]["tools"][0]["name"] == "deploy"


def test_tools_list_shape():
    resp = _handle_tools_list(99, {})
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 99
    assert "tools" in resp["result"]


# ── _dispatch ─────────────────────────────────────────────────────────────────


DUMMY_TOOLS: dict = {"greet": {"name": "greet", "inputSchema": {"type": "object", "properties": {}}}}
DUMMY_SPECS: dict = {"greet": {}}
DUMMY_EXEC_SPECS: dict = {"greet": {"command": None}}


def test_dispatch_initialize():
    req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    resp = _dispatch(req, DUMMY_TOOLS, DUMMY_SPECS, DUMMY_EXEC_SPECS, "test-env")
    assert resp is not None
    assert "protocolVersion" in resp["result"]


def test_dispatch_tools_list():
    req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    resp = _dispatch(req, DUMMY_TOOLS, DUMMY_SPECS, DUMMY_EXEC_SPECS, "test-env")
    assert resp is not None
    assert "tools" in resp["result"]


def test_dispatch_notification_returns_none():
    req = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    resp = _dispatch(req, DUMMY_TOOLS, DUMMY_SPECS, DUMMY_EXEC_SPECS, "test-env")
    assert resp is None


def test_dispatch_unknown_method():
    req = {"jsonrpc": "2.0", "id": 5, "method": "unknown/method"}
    resp = _dispatch(req, DUMMY_TOOLS, DUMMY_SPECS, DUMMY_EXEC_SPECS, "test-env")
    assert resp is not None
    assert resp["error"]["code"] == -32601


# ── _handle_tools_call ────────────────────────────────────────────────────────


def test_tools_call_unknown_tool():
    resp = _handle_tools_call(3, {"name": "missing", "arguments": {}}, DUMMY_TOOLS, DUMMY_SPECS, DUMMY_EXEC_SPECS)
    assert resp["error"]["code"] == -32602
    assert "missing" in resp["error"]["message"]


def test_tools_call_script_not_found():
    tools = {"greet": {"name": "greet", "inputSchema": {"type": "object", "properties": {}}}}
    exec_specs = {"greet": {"command": None}}
    resp = _handle_tools_call(3, {"name": "greet", "arguments": {}}, tools, {}, exec_specs)
    assert resp["result"]["isError"] is True
    assert "greet" in resp["result"]["content"][0]["text"]


def test_tools_call_success(tmp_path):
    script = tmp_path / "greet"
    script.write_text("#!/bin/sh\necho hello", encoding="utf-8")
    script.chmod(0o755)

    tools = {"greet": {"name": "greet", "inputSchema": {"type": "object", "properties": {}}}}
    exec_specs = {"greet": {"command": [str(script)]}}

    resp = _handle_tools_call(3, {"name": "greet", "arguments": {}}, tools, {}, exec_specs)
    assert resp["result"]["isError"] is False
    assert "hello" in resp["result"]["content"][0]["text"]
    # MCP _meta envelope present on success
    meta = resp["result"]["_meta"]["runspec"]
    assert meta["tool"] == "greet"
    assert meta["exit_code"] == 0
    assert isinstance(meta["duration_ms"], int)
    assert meta["duration_ms"] >= 0


def test_tools_call_failure(tmp_path):
    script = tmp_path / "deploy"
    script.write_text("#!/bin/sh\necho 'bad things' >&2\nexit 1", encoding="utf-8")
    script.chmod(0o755)

    tools = {"deploy": {"name": "deploy", "inputSchema": {"type": "object", "properties": {}}}}
    exec_specs = {"deploy": {"command": [str(script)]}}

    resp = _handle_tools_call(4, {"name": "deploy", "arguments": {}}, tools, {}, exec_specs)
    assert resp["result"]["isError"] is True
    text = resp["result"]["content"][0]["text"]
    assert "exit_code: 1" in text
    assert "bad things" in text
    # MCP _meta envelope present on failure too
    meta = resp["result"]["_meta"]["runspec"]
    assert meta["tool"] == "deploy"
    assert meta["exit_code"] == 1
    assert isinstance(meta["duration_ms"], int)


def test_tools_call_sets_runspec_agent_env(tmp_path):
    script = tmp_path / "check_env"
    script.write_text("#!/bin/sh\necho $RUNSPEC_AGENT", encoding="utf-8")
    script.chmod(0o755)

    tools = {"check_env": {"name": "check_env", "inputSchema": {"type": "object", "properties": {}}}}
    exec_specs = {"check_env": {"command": [str(script)]}}

    resp = _handle_tools_call(5, {"name": "check_env", "arguments": {}}, tools, {}, exec_specs)
    assert "1" in resp["result"]["content"][0]["text"]


def test_tools_call_sets_runspec_config_env(tmp_path):
    """The subprocess inherits RUNSPEC_CONFIG pointing at the source TOML."""
    script = tmp_path / "show_config"
    script.write_text("#!/bin/sh\necho $RUNSPEC_CONFIG", encoding="utf-8")
    script.chmod(0o755)

    tools = {"show_config": {"name": "show_config", "inputSchema": {"type": "object", "properties": {}}}}
    exec_specs = {
        "show_config": {
            "command": [str(script)],
            "config_path": "/some/pkg/runspec.toml",
        }
    }

    resp = _handle_tools_call(6, {"name": "show_config", "arguments": {}}, tools, {}, exec_specs)
    assert "/some/pkg/runspec.toml" in resp["result"]["content"][0]["text"]


def test_tools_call_omits_runspec_config_when_unset(tmp_path):
    """When exec_spec has no config_path, RUNSPEC_CONFIG is not injected."""
    script = tmp_path / "show_config"
    script.write_text('#!/bin/sh\necho "[$RUNSPEC_CONFIG]"', encoding="utf-8")
    script.chmod(0o755)

    tools = {"show_config": {"name": "show_config", "inputSchema": {"type": "object", "properties": {}}}}
    exec_specs = {"show_config": {"command": [str(script)]}}  # no config_path

    resp = _handle_tools_call(7, {"name": "show_config", "arguments": {}}, tools, {}, exec_specs)
    out = resp["result"]["content"][0]["text"]
    # If RUNSPEC_CONFIG wasn't set, $RUNSPEC_CONFIG expands to empty string → "[]"
    assert out.strip() == "[]"


# ── _find_script ──────────────────────────────────────────────────────────────


def test_find_script_in_scripts_dir(tmp_path):
    script = tmp_path / "deploy"
    script.touch()
    assert _find_script("deploy", tmp_path) == [str(script)]


def test_find_script_exe_in_scripts_dir(tmp_path):
    script = tmp_path / "deploy.exe"
    script.touch()
    assert _find_script("deploy", tmp_path) == [str(script)]


def test_find_script_missing(tmp_path):
    assert _find_script("missing", tmp_path) is None


# ── _expand_tools ─────────────────────────────────────────────────────────────

_LEAF_INFERRED = {
    "description": "Do a thing",
    "autonomy": "confirm",
    "output": "text",
    "args": {"env": {"type": "str", "required": True}},
    "commands": {},
}


def test_expand_tools_no_commands():
    entries = _expand_tools("greet", _LEAF_INFERRED, ["/bin/greet"], None, "sudo", None)
    assert len(entries) == 1
    flat_name, schema, arg_spec, exec_spec = entries[0]
    assert flat_name == "greet"
    assert schema["name"] == "greet"
    assert exec_spec["command"] == ["/bin/greet"]


def test_expand_tools_one_level():
    inferred = {
        "description": "Pipeline tool",
        "autonomy": "confirm",
        "output": "text",
        "args": {},
        "commands": {
            "run": _LEAF_INFERRED,
            "validate": {**_LEAF_INFERRED, "description": "Validate"},
        },
    }
    entries = _expand_tools("pipeline", inferred, ["/bin/pipeline"], None, "sudo", None)
    names = [e[0] for e in entries]
    assert names == ["pipeline_run", "pipeline_validate"]
    _, _, _, exec_spec_run = entries[0]
    assert exec_spec_run["command"] == ["/bin/pipeline", "run"]
    _, _, _, exec_spec_val = entries[1]
    assert exec_spec_val["command"] == ["/bin/pipeline", "validate"]


def test_expand_tools_two_levels():
    get_list = {**_LEAF_INFERRED, "description": "List orders", "args": {"limit": {"type": "int"}}}
    post_sale = {**_LEAF_INFERRED, "description": "Post a sale", "args": {"amount": {"type": "int", "required": True}}}
    orders_endpoint = {
        "description": "Orders endpoint",
        "autonomy": "confirm",
        "output": "text",
        "args": {},
        "commands": {"get_list": get_list, "post_sale": post_sale},
    }
    portal_api = {
        "description": "Portal API",
        "autonomy": "confirm",
        "output": "text",
        "args": {},
        "commands": {"orders_endpoint": orders_endpoint},
    }
    entries = _expand_tools("portal_api", portal_api, ["/bin/portal_api"], None, "sudo", None)
    names = [e[0] for e in entries]
    assert "portal_api_orders_endpoint_get_list" in names
    assert "portal_api_orders_endpoint_post_sale" in names
    # Command prefix includes both subcommand levels
    by_name = {e[0]: e for e in entries}
    _, _, _, exec_get = by_name["portal_api_orders_endpoint_get_list"]
    assert exec_get["command"] == ["/bin/portal_api", "orders_endpoint", "get_list"]
    _, _, arg_spec_post, exec_post = by_name["portal_api_orders_endpoint_post_sale"]
    assert exec_post["command"] == ["/bin/portal_api", "orders_endpoint", "post_sale"]
    assert "amount" in arg_spec_post


def test_expand_tools_none_command_propagated():
    entries = _expand_tools("greet", _LEAF_INFERRED, None, None, "sudo", None)
    _, _, _, exec_spec = entries[0]
    assert exec_spec["command"] is None


def test_expand_tools_run_as_propagated_to_leaves():
    inferred = {
        "description": "root",
        "autonomy": "confirm",
        "output": "text",
        "args": {},
        "commands": {"sub": _LEAF_INFERRED},
    }
    entries = _expand_tools("tool", inferred, ["/bin/tool"], "oracle", "sudo", "-H")
    _, _, _, exec_spec = entries[0]
    assert exec_spec["run_as"] == "oracle"
    assert exec_spec["become_flags"] == "-H"


def test_expand_tools_leaf_schema_has_args():
    entries = _expand_tools("greet", _LEAF_INFERRED, ["/bin/greet"], None, "sudo", None)
    _, schema, arg_spec, _ = entries[0]
    assert "env" in schema["inputSchema"]["properties"]
    assert "env" in arg_spec


# ── _server_name ──────────────────────────────────────────────────────────────


def test_server_name_from_config():
    assert _server_name({"name": "analytics-pipeline"}) == "analytics-pipeline"


def test_server_name_falls_back_to_venv():
    name = _server_name({})
    assert isinstance(name, str)
    assert len(name) > 0


def test_server_name_ignores_non_string():
    name = _server_name({"name": 42})
    assert isinstance(name, str)


# ── serve() — importlib.metadata discovery ────────────────────────────────────


def test_serve_exits_when_no_installed_packages(capsys):
    from unittest.mock import patch

    with patch("runspec.cli._discover_installed", return_value=[]), pytest.raises(SystemExit) as exc:
        serve()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "no runspec-aware packages" in captured.err


def test_serve_warns_on_duplicate_runnable_name(tmp_path, capsys):
    from unittest.mock import patch

    toml1 = tmp_path / "pkg1" / "runspec.toml"
    toml1.parent.mkdir()
    toml1.write_text("[greet]\ndescription = 'From pkg1'\n", encoding="utf-8")

    toml2 = tmp_path / "pkg2" / "runspec.toml"
    toml2.parent.mkdir()
    toml2.write_text("[greet]\ndescription = 'From pkg2'\n", encoding="utf-8")

    discovered = [
        {"source": str(toml1), "runnable": "greet", "spec": {"description": "From pkg1"}},
        {"source": str(toml2), "runnable": "greet", "spec": {"description": "From pkg2"}},
    ]

    captured_tools: dict = {}

    def fake_mcp_loop(tools, arg_specs, exec_specs, name):
        captured_tools.update(tools)

    with (
        patch("runspec.cli._discover_installed", return_value=discovered),
        patch("runspec.serve._mcp_loop", fake_mcp_loop),
        patch("runspec.serve._find_script", return_value=None),
    ):
        serve()

    captured = capsys.readouterr()
    assert "warning" in captured.err
    assert "greet" in captured.err
    # First definition wins
    assert "greet" in captured_tools


def test_serve_aggregates_runnables_from_multiple_installed_packages(tmp_path):
    from unittest.mock import patch

    toml1 = tmp_path / "pkg1" / "runspec.toml"
    toml1.parent.mkdir()
    toml1.write_text("[greet]\ndescription = 'Greet'\n", encoding="utf-8")

    toml2 = tmp_path / "pkg2" / "runspec.toml"
    toml2.parent.mkdir()
    toml2.write_text("[deploy]\ndescription = 'Deploy'\n", encoding="utf-8")

    discovered = [
        {"source": str(toml1), "runnable": "greet", "spec": {"description": "Greet"}},
        {"source": str(toml2), "runnable": "deploy", "spec": {"description": "Deploy"}},
    ]

    captured_tools: dict = {}

    def fake_mcp_loop(tools, arg_specs, exec_specs, name):
        captured_tools.update(tools)

    with (
        patch("runspec.cli._discover_installed", return_value=discovered),
        patch("runspec.serve._mcp_loop", fake_mcp_loop),
        patch("runspec.serve._find_script", return_value=None),
    ):
        serve()

    assert "greet" in captured_tools
    assert "deploy" in captured_tools


def test_serve_single_package_uses_its_config_name(tmp_path):
    """Single-package venv: the [config] name from the TOML is used for the MCP server."""
    from unittest.mock import patch

    toml = tmp_path / "pkg" / "runspec.toml"
    toml.parent.mkdir()
    toml.write_text("[config]\nname = 'custom-server'\n\n[greet]\ndescription = 'Greet'\n", encoding="utf-8")

    discovered = [
        {"source": str(toml), "runnable": "greet", "spec": {"description": "Greet"}},
    ]
    captured: dict = {}

    def fake_mcp_loop(tools, arg_specs, exec_specs, name):
        captured["server_name"] = name

    with (
        patch("runspec.cli._discover_installed", return_value=discovered),
        patch("runspec.serve._mcp_loop", fake_mcp_loop),
        patch("runspec.serve._find_script", return_value=None),
    ):
        serve()

    assert captured["server_name"] == "custom-server"


def test_serve_multi_package_falls_back_to_venv_name(tmp_path):
    """Multi-package venv: [config] name is ambiguous, so fall back to venv dir name."""
    from unittest.mock import patch

    toml1 = tmp_path / "pkg1" / "runspec.toml"
    toml1.parent.mkdir()
    toml1.write_text("[config]\nname = 'first'\n\n[greet]\n", encoding="utf-8")

    toml2 = tmp_path / "pkg2" / "runspec.toml"
    toml2.parent.mkdir()
    toml2.write_text("[config]\nname = 'second'\n\n[deploy]\n", encoding="utf-8")

    discovered = [
        {"source": str(toml1), "runnable": "greet", "spec": {}},
        {"source": str(toml2), "runnable": "deploy", "spec": {}},
    ]
    captured: dict = {}

    def fake_mcp_loop(tools, arg_specs, exec_specs, name):
        captured["server_name"] = name

    with (
        patch("runspec.cli._discover_installed", return_value=discovered),
        patch("runspec.serve._mcp_loop", fake_mcp_loop),
        patch("runspec.serve._find_script", return_value=None),
    ):
        serve()

    # Should not match either of the two config names — falls back to venv directory
    assert captured["server_name"] not in ("first", "second")
    assert isinstance(captured["server_name"], str)
    assert len(captured["server_name"]) > 0


def test_serve_skips_runnable_with_serve_false(tmp_path):
    from unittest.mock import patch

    toml = tmp_path / "pkg" / "runspec.toml"
    toml.parent.mkdir()
    toml.write_text(
        "[launcher]\nserve = false\ndescription = 'UI launcher'\n\n[worker]\ndescription = 'Agent tool'\n",
        encoding="utf-8",
    )

    discovered = [
        {"source": str(toml), "runnable": "launcher", "spec": {"serve": False, "description": "UI launcher"}},
        {"source": str(toml), "runnable": "worker", "spec": {"description": "Agent tool"}},
    ]

    captured_tools: dict = {}

    def fake_mcp_loop(tools, arg_specs, exec_specs, name):
        captured_tools.update(tools)

    with (
        patch("runspec.cli._discover_installed", return_value=discovered),
        patch("runspec.serve._mcp_loop", fake_mcp_loop),
        patch("runspec.serve._find_script", return_value=None),
    ):
        serve()

    assert "launcher" not in captured_tools, "serve=false runnable must not be exposed as MCP tool"
    assert "worker" in captured_tools


def test_serve_includes_runnable_with_serve_true_explicitly(tmp_path):
    from unittest.mock import patch

    toml = tmp_path / "pkg" / "runspec.toml"
    toml.parent.mkdir()
    toml.write_text("[worker]\nserve = true\ndescription = 'Agent tool'\n", encoding="utf-8")

    discovered = [
        {"source": str(toml), "runnable": "worker", "spec": {"serve": True, "description": "Agent tool"}},
    ]

    captured_tools: dict = {}

    def fake_mcp_loop(tools, arg_specs, exec_specs, name):
        captured_tools.update(tools)

    with (
        patch("runspec.cli._discover_installed", return_value=discovered),
        patch("runspec.serve._mcp_loop", fake_mcp_loop),
        patch("runspec.serve._find_script", return_value=None),
    ):
        serve()

    assert "worker" in captured_tools


# ── serve context helpers ─────────────────────────────────────────────────────


def test_serve_context_local_when_no_ssh(monkeypatch):
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("RUNSPEC_SERVE_CONTEXT", raising=False)
    assert _serve_context() == "local"


def test_serve_context_remote_when_ssh(monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 12345 10.0.0.2 22")
    monkeypatch.delenv("RUNSPEC_SERVE_CONTEXT", raising=False)
    assert _serve_context() == "remote"


def test_serve_context_override_wins(monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 12345 10.0.0.2 22")
    monkeypatch.setenv("RUNSPEC_SERVE_CONTEXT", "local")
    assert _serve_context() == "local"


def test_validate_serve_none():
    assert _validate_serve(None) == []


def test_validate_serve_true():
    assert _validate_serve(True) == []


def test_validate_serve_false():
    assert _validate_serve(False) == []


def test_validate_serve_local_list():
    assert _validate_serve(["local"]) == []


def test_validate_serve_remote_list():
    assert _validate_serve(["remote"]) == []


def test_validate_serve_both_list():
    assert _validate_serve(["local", "remote"]) == []


def test_validate_serve_empty_list():
    errors = _validate_serve([])
    assert len(errors) == 1
    assert "non-empty" in errors[0]


def test_validate_serve_unknown_context():
    errors = _validate_serve(["cloud"])
    assert len(errors) == 1
    assert "cloud" in errors[0]


def test_validate_serve_bad_type():
    errors = _validate_serve(42)
    assert len(errors) == 1
    assert "int" in errors[0]


def test_is_serve_match_none_always_true():
    assert _is_serve_match(None, "local") is True
    assert _is_serve_match(None, "remote") is True


def test_is_serve_match_true_always_true():
    assert _is_serve_match(True, "local") is True
    assert _is_serve_match(True, "remote") is True


def test_is_serve_match_false_always_false():
    assert _is_serve_match(False, "local") is False
    assert _is_serve_match(False, "remote") is False


def test_is_serve_match_local_list():
    assert _is_serve_match(["local"], "local") is True
    assert _is_serve_match(["local"], "remote") is False


def test_is_serve_match_remote_list():
    assert _is_serve_match(["remote"], "remote") is True
    assert _is_serve_match(["remote"], "local") is False


def test_is_serve_match_both_list():
    assert _is_serve_match(["local", "remote"], "local") is True
    assert _is_serve_match(["local", "remote"], "remote") is True


# ── serve() integration: list form ───────────────────────────────────────────


def _run_serve_with_context(tmp_path, monkeypatch, serve_value, context):
    """Run serve() with a given serve field value and explicit context override."""
    from unittest.mock import patch

    toml = tmp_path / "pkg" / "runspec.toml"
    toml.parent.mkdir()
    toml.write_text("[config]\nname = 'test'\n\n[mytool]\ndescription = 'tool'\n", encoding="utf-8")

    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.setenv("RUNSPEC_SERVE_CONTEXT", context)

    discovered = [
        {"source": str(toml), "runnable": "mytool", "spec": {"serve": serve_value, "description": "tool"}},
    ]
    captured_tools: dict = {}

    def fake_mcp_loop(tools, arg_specs, exec_specs, name):
        captured_tools.update(tools)

    with (
        patch("runspec.cli._discover_installed", return_value=discovered),
        patch("runspec.serve._mcp_loop", fake_mcp_loop),
        patch("runspec.serve._find_script", return_value=None),
    ):
        serve()

    return captured_tools


def test_serve_list_local_in_local_context(tmp_path, monkeypatch):
    tools = _run_serve_with_context(tmp_path, monkeypatch, ["local"], "local")
    assert "mytool" in tools


def test_serve_list_local_in_remote_context(tmp_path, monkeypatch):
    tools = _run_serve_with_context(tmp_path, monkeypatch, ["local"], "remote")
    assert "mytool" not in tools


def test_serve_list_remote_in_remote_context(tmp_path, monkeypatch):
    tools = _run_serve_with_context(tmp_path, monkeypatch, ["remote"], "remote")
    assert "mytool" in tools


def test_serve_list_remote_in_local_context(tmp_path, monkeypatch):
    tools = _run_serve_with_context(tmp_path, monkeypatch, ["remote"], "local")
    assert "mytool" not in tools


def test_serve_list_both_in_local_context(tmp_path, monkeypatch):
    tools = _run_serve_with_context(tmp_path, monkeypatch, ["local", "remote"], "local")
    assert "mytool" in tools


def test_serve_list_both_in_remote_context(tmp_path, monkeypatch):
    tools = _run_serve_with_context(tmp_path, monkeypatch, ["local", "remote"], "remote")
    assert "mytool" in tools
