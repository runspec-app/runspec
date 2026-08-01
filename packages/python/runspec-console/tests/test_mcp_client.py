"""
test_mcp_client.py — the external-MCP client: connecting the console's chat
agent to outside MCP servers and folding their tools into its toolset.

Layers, mirroring the rest of the suite:

* **pure helpers** (no SDK): display-name namespacing, schema conversion, result
  flattening, autonomy resolution, ``config.read_mcp``;
* **manager, offline**: registry/tools/autonomy built from injected fake state,
  and the never-connected / bad-server error paths;
* **bridge wiring**: the autonomy gate + dispatch + tool-list + headless filter
  route an MCP tool correctly, exercised on a light stub;
* **end-to-end** (skipped unless the ``mcp`` SDK is installed): a real stdio
  ``MCPServer`` subprocess is connected, listed, and called through the manager.
"""

from __future__ import annotations

import sys
import textwrap
from types import SimpleNamespace

import pytest

from runspec_console import config, mcp_client
from runspec_console.bridge import Bridge
from runspec_console.bridge_const import _MCP_PREFIX


# ── pure: display names ──────────────────────────────────────────────────────


def test_display_name_is_namespaced_and_prefixed():
    name = mcp_client.tool_display_name("github", "search_issues")
    assert name == "mcp_github_search_issues"
    assert name.startswith(_MCP_PREFIX)


def test_display_name_sanitises_illegal_characters():
    # Anthropic tool names are ^[a-zA-Z0-9_-]{1,64}$ — dots/slashes/colons out.
    name = mcp_client.tool_display_name("acme.io", "list/all:things")
    assert name == "mcp_acme_io_list_all_things"
    assert all(c.isalnum() or c in "_-" for c in name)


def test_display_name_capped_at_64_chars():
    name = mcp_client.tool_display_name("s" * 40, "t" * 40)
    assert len(name) <= 64
    assert name.startswith("mcp_")


def test_sanitise_empty_component_becomes_x():
    assert mcp_client._sanitize_component("!!!") == "x"
    assert mcp_client._sanitize_component("") == "x"


# ── pure: schema conversion ──────────────────────────────────────────────────


def test_to_anthropic_schema_preserves_object_schema():
    schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }
    out = mcp_client.to_anthropic_schema("mcp_x_search", "Find things", schema)
    assert out["name"] == "mcp_x_search"
    assert out["description"] == "Find things"
    assert out["input_schema"]["required"] == ["q"]
    assert out["input_schema"]["properties"]["q"]["type"] == "string"


def test_to_anthropic_schema_defaults_missing_schema_to_object():
    out = mcp_client.to_anthropic_schema("mcp_x_ping", "", None)
    assert out["input_schema"] == {"type": "object", "properties": {}}
    # An empty description falls back to a non-empty stand-in.
    assert out["description"]


def test_to_anthropic_schema_fills_missing_type():
    out = mcp_client.to_anthropic_schema("mcp_x_y", "d", {"properties": {"a": {}}})
    assert out["input_schema"]["type"] == "object"


# ── pure: result flattening ──────────────────────────────────────────────────


def test_flatten_result_joins_text_blocks():
    result = SimpleNamespace(
        is_error=False,
        content=[
            SimpleNamespace(type="text", text="line one"),
            SimpleNamespace(type="text", text="line two"),
        ],
        structured_content=None,
    )
    assert mcp_client.flatten_result(result) == "line one\nline two"


def test_flatten_result_prefixes_errors():
    result = {"isError": True, "content": [{"type": "text", "text": "boom"}]}
    assert mcp_client.flatten_result(result) == "Error: boom"


def test_flatten_result_appends_structured_content_as_json():
    result = {"is_error": False, "content": [], "structured_content": {"ok": True}}
    assert mcp_client.flatten_result(result) == '{"ok": true}'


def test_flatten_result_notes_non_text_blocks():
    result = {"is_error": False, "content": [{"type": "image", "data": "…"}]}
    assert "[image content omitted]" in mcp_client.flatten_result(result)


def test_flatten_result_handles_empty_content():
    assert mcp_client.flatten_result({"is_error": False, "content": []}) == (
        "(tool returned no content)"
    )


def test_flatten_result_caps_long_text():
    result = {"is_error": False, "content": [{"type": "text", "text": "x" * 100}]}
    out = mcp_client.flatten_result(result, cap=10)
    assert out.startswith("x" * 10) and out.endswith("[truncated]")


# ── pure: autonomy resolution ────────────────────────────────────────────────


def test_autonomy_defaults_to_confirm():
    assert mcp_client.autonomy_for_server({}) == "confirm"
    assert mcp_client.autonomy_for_server({"autonomy": ""}) == "confirm"
    assert mcp_client.autonomy_for_server({"autonomy": "bogus"}) == "confirm"


def test_autonomy_honours_explicit_override():
    assert mcp_client.autonomy_for_server({"autonomy": "autonomous"}) == "autonomous"
    assert mcp_client.autonomy_for_server({"autonomy": "manual"}) == "manual"


# ── pure: proxy normalisation ────────────────────────────────────────────────


def test_normalize_proxy_adds_scheme_and_passes_through():
    assert mcp_client.normalize_proxy("proxy:8080") == "http://proxy:8080"
    assert mcp_client.normalize_proxy("http://p:3128") == "http://p:3128"
    assert mcp_client.normalize_proxy("https://p:3128") == "https://p:3128"
    assert mcp_client.normalize_proxy("") == ""
    assert mcp_client.normalize_proxy(None) == ""


# ── pure: credential header application ──────────────────────────────────────


def test_credential_header_bearer_for_token():
    headers: dict[str, str] = {}
    mcp_client.apply_credential_header(headers, {}, {"kind": "token", "secret": "abc"})
    assert headers["Authorization"] == "Bearer abc"


def test_credential_header_basic_for_userpass():
    import base64

    headers: dict[str, str] = {}
    mcp_client.apply_credential_header(
        headers, {}, {"kind": "userpass", "username": "u", "secret": "p"}
    )
    expected = "Basic " + base64.b64encode(b"u:p").decode()
    assert headers["Authorization"] == expected


def test_credential_header_custom_carries_raw_secret():
    headers: dict[str, str] = {}
    mcp_client.apply_credential_header(
        headers, {"credential_header": "X-API-Key"}, {"kind": "token", "secret": "k"}
    )
    assert headers == {"X-API-Key": "k"}


def test_credential_header_does_not_override_explicit():
    headers = {"Authorization": "Bearer preset"}
    mcp_client.apply_credential_header(headers, {}, {"kind": "token", "secret": "new"})
    assert headers["Authorization"] == "Bearer preset"  # operator's header wins


def test_credential_header_noop_without_secret():
    headers: dict[str, str] = {}
    mcp_client.apply_credential_header(headers, {}, None)
    mcp_client.apply_credential_header(headers, {}, {"kind": "token", "secret": ""})
    assert headers == {}


# ── manager: credential resolution ───────────────────────────────────────────


def test_resolve_credential_uses_injected_resolver():
    seen = {}

    def resolver(cid):
        seen["id"] = cid
        return {"kind": "token", "secret": "S", "env": {"DEMO_TOKEN": "S"}}

    mgr = mcp_client.McpManager(credential_resolver=resolver)
    cred = mgr._resolve_credential({"name": "x", "credential": "cred-1"})
    assert seen["id"] == "cred-1"
    assert cred["secret"] == "S"


def test_resolve_credential_none_without_id_or_resolver():
    mgr = mcp_client.McpManager(credential_resolver=lambda cid: {"secret": "S"})
    assert mgr._resolve_credential({"name": "x"}) is None  # no credential id
    mgr2 = mcp_client.McpManager()  # no resolver
    assert mgr2._resolve_credential({"credential": "c"}) is None


def test_resolve_credential_swallows_resolver_errors():
    def boom(cid):
        raise RuntimeError("keychain locked")

    mgr = mcp_client.McpManager(credential_resolver=boom)
    assert mgr._resolve_credential({"name": "x", "credential": "c"}) is None


# ── config: read_mcp ─────────────────────────────────────────────────────────


def _patch_config(monkeypatch, raw):
    monkeypatch.setattr(config, "read_config", lambda: {"mcp": raw})


def test_read_mcp_empty_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "read_config", lambda: {})
    cfg = config.read_mcp()
    assert cfg["enabled"] is True and cfg["servers"] == []


def test_read_mcp_parses_a_stdio_server(monkeypatch):
    _patch_config(
        monkeypatch,
        {
            "servers": [
                {
                    "name": "fs",
                    "command": "runspec-fs-server",
                    "args": ["--root", "/data"],
                    "env": {"TOKEN": "x"},
                }
            ]
        },
    )
    (s,) = config.read_mcp()["servers"]
    assert s["name"] == "fs"
    assert s["transport"] == "stdio"  # inferred from command
    assert s["args"] == ["--root", "/data"]
    assert s["env"] == {"TOKEN": "x"}
    assert s["enabled"] is True


def test_read_mcp_infers_http_for_url_only(monkeypatch):
    _patch_config(monkeypatch, {"servers": [{"name": "api", "url": "https://h/mcp"}]})
    (s,) = config.read_mcp()["servers"]
    assert s["transport"] == "http" and s["url"] == "https://h/mcp"


def test_read_mcp_normalises_autonomy_and_drops_nameless(monkeypatch):
    _patch_config(
        monkeypatch,
        {
            "servers": [
                {"name": "good", "url": "https://h", "autonomy": "AUTONOMOUS"},
                {"name": "bad", "url": "https://h", "autonomy": "nope"},
                {"url": "https://h"},  # no name — dropped
            ]
        },
    )
    servers = config.read_mcp()["servers"]
    assert [s["name"] for s in servers] == ["good", "bad"]
    assert servers[0]["autonomy"] == "autonomous"
    assert servers[1]["autonomy"] == ""  # unknown level cleared → default confirm


def test_read_mcp_clamps_timeouts(monkeypatch):
    _patch_config(monkeypatch, {"call_timeout": 0, "connect_timeout": -5})
    cfg = config.read_mcp()
    assert cfg["call_timeout"] >= 1.0 and cfg["connect_timeout"] >= 1.0


def test_read_mcp_parses_credential_and_proxy(monkeypatch):
    _patch_config(
        monkeypatch,
        {
            "servers": [
                {
                    "name": "api",
                    "url": "https://h/mcp",
                    "credential": "cred-42",
                    "credential_header": "X-API-Key",
                    "proxy": "corp-proxy:8080",
                }
            ]
        },
    )
    (s,) = config.read_mcp()["servers"]
    assert s["credential"] == "cred-42"
    assert s["credential_header"] == "X-API-Key"
    assert s["proxy"] == "corp-proxy:8080"


def test_read_mcp_credential_and_proxy_default_empty(monkeypatch):
    _patch_config(monkeypatch, {"servers": [{"name": "x", "url": "https://h"}]})
    (s,) = config.read_mcp()["servers"]
    assert s["credential"] == "" and s["proxy"] == "" and s["credential_header"] == ""


def test_mcp_config_and_credentials_are_seed_and_sync_covered():
    # [mcp] rides config.toml and a bound credential's metadata rides
    # runspec_credentials.toml — both must be in the Config Sync / seed known-set
    # so a bundled/synced deployment carries MCP servers (secrets stay keychain).
    from runspec_console.gitsync import KNOWN_FILES

    assert "config.toml" in KNOWN_FILES
    assert "runspec_credentials.toml" in KNOWN_FILES


# ── manager: offline registry / tools / autonomy ─────────────────────────────


def _fake_tool(name, desc="", schema=None):
    return SimpleNamespace(
        name=name, description=desc, input_schema=schema or {"type": "object"}
    )


def _manager_with_state(server_cfg, tools):
    mgr = mcp_client.McpManager(config_reader=lambda: {"enabled": True, "servers": []})
    st = mcp_client._ServerState(server_cfg)
    st.connected = True
    st.tools = tools
    mgr._servers[st.name] = st
    mgr._register(st)
    return mgr


def test_register_builds_registry_and_tools():
    mgr = _manager_with_state(
        {"name": "gh", "autonomy": ""},
        [
            _fake_tool(
                "search_issues",
                "Find issues",
                {"type": "object", "properties": {"q": {"type": "string"}}},
            )
        ],
    )
    assert mgr.is_mcp_tool("mcp_gh_search_issues")
    (tool,) = mgr.tools()
    assert tool["name"] == "mcp_gh_search_issues"
    assert "Find issues" in tool["description"]
    assert "MCP server 'gh'" in tool["description"]  # provenance in the description
    assert tool["input_schema"]["properties"]["q"]["type"] == "string"


def test_register_disambiguates_colliding_names():
    # Two distinct bare names that sanitise to the same base get unique displays.
    mgr = _manager_with_state(
        {"name": "s"},
        [_fake_tool("a-b"), _fake_tool("a_b")],
    )
    names = {t["name"] for t in mgr.tools()}
    assert names == {"mcp_s_a_b", "mcp_s_a_b_2"}


def test_autonomy_for_reads_server_override():
    mgr = _manager_with_state(
        {"name": "safe", "autonomy": "autonomous"}, [_fake_tool("go")]
    )
    assert mgr.autonomy_for("mcp_safe_go") == "autonomous"


def test_autonomy_for_unknown_tool_is_confirm():
    mgr = mcp_client.McpManager(config_reader=lambda: {"enabled": True, "servers": []})
    assert mgr.autonomy_for("mcp_nope_x") == "confirm"


def test_call_tool_when_not_connected_returns_error():
    mgr = mcp_client.McpManager(config_reader=lambda: {"enabled": True, "servers": []})
    out = mgr.call_tool("mcp_nope_x", {})
    assert out.startswith("Error:") and "not connected" in out


def test_status_reports_servers():
    mgr = _manager_with_state({"name": "gh"}, [_fake_tool("x"), _fake_tool("y")])
    st = mgr.status()
    assert st["tool_count"] == 2
    (server,) = st["servers"]
    assert server["name"] == "gh" and server["connected"] and server["tool_count"] == 2


def test_status_surfaces_credential_id_and_proxy_no_secret():
    mgr = _manager_with_state(
        {"name": "api", "credential": "cred-9", "proxy": "p:8080"}, [_fake_tool("go")]
    )
    (server,) = mgr.status()["servers"]
    assert server["credential"] == "cred-9"  # id only — never the secret
    assert server["proxy"] == "http://p:8080"  # normalised for display


def test_start_is_inert_without_servers():
    mgr = mcp_client.McpManager(config_reader=lambda: {"enabled": True, "servers": []})
    mgr.start()  # must not spin up a loop or raise
    assert mgr._loop is None
    assert mgr.status()["servers"] == []


def test_start_respects_section_kill_switch():
    mgr = mcp_client.McpManager(
        config_reader=lambda: {
            "enabled": False,
            "servers": [{"name": "x", "transport": "stdio", "command": "true"}],
        }
    )
    mgr.start()
    assert mgr._loop is None  # disabled ⇒ never connects


# ── bridge wiring (light stub, fake manager) ─────────────────────────────────


class _FakeMgr:
    def __init__(self):
        self._names = {"mcp_demo_echo"}
        self.calls: list = []

    def is_mcp_tool(self, name):
        return name in self._names

    def reload(self):
        self.reloaded = True

    def autonomy_for(self, name):
        return "confirm"

    def call_tool(self, name, args):
        self.calls.append((name, args))
        return f"echoed:{args.get('text')}"

    def tools(self):
        return [
            {
                "name": "mcp_demo_echo",
                "description": "d",
                "input_schema": {"type": "object"},
            }
        ]

    def status(self):
        return {
            "available": True,
            "servers": [
                {
                    "name": "demo",
                    "transport": "stdio",
                    "connected": True,
                    "tool_count": 1,
                    "error": "",
                }
            ],
            "tool_count": 1,
        }


class _WiringStub:
    _is_mcp_tool = Bridge._is_mcp_tool
    _mcp_tools = Bridge._mcp_tools
    _run_mcp_tool = Bridge._run_mcp_tool
    _effective_tool_autonomy = Bridge._effective_tool_autonomy
    _run_tool_sync = Bridge._run_tool_sync
    mcp_status = Bridge.mcp_status

    def __init__(self):
        self._mcp = _FakeMgr()


def test_is_mcp_tool_reflects_registry():
    stub = _WiringStub()
    assert stub._is_mcp_tool("mcp_demo_echo") is True
    assert stub._is_mcp_tool("fetch_url") is False


def test_effective_autonomy_for_mcp_tool_is_confirm():
    assert _WiringStub()._effective_tool_autonomy("mcp_demo_echo", {}) == "confirm"


def test_dispatch_routes_mcp_tool_to_manager():
    stub = _WiringStub()
    out = stub._run_tool_sync("mcp_demo_echo", {"text": "hi"})
    assert out == "echoed:hi"
    assert stub._mcp.calls == [("mcp_demo_echo", {"text": "hi"})]


def test_mcp_tools_surfaced_in_toolset():
    names = {t["name"] for t in _WiringStub()._mcp_tools()}
    assert "mcp_demo_echo" in names


def test_mcp_status_passthrough():
    assert _WiringStub().mcp_status()["tool_count"] == 1


class _SearchStub:
    """Exercises the search-mode deferral path — MCP tools land in the deferred
    tail (not the hot kernel), reached via describe_tool / use_tool."""

    _unwrap_use_tool = Bridge._unwrap_use_tool
    _preflight_builtin_input = staticmethod(Bridge._preflight_builtin_input)

    def _deferred_builtin_tools(self):
        return [
            {
                "name": "mcp_demo_echo",
                "description": "d",
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }
        ]


def test_use_tool_resolves_deferred_mcp_tool():
    resolved = _SearchStub()._unwrap_use_tool(
        {"name": "mcp_demo_echo", "input": {"text": "hi"}}
    )
    assert resolved == ("mcp_demo_echo", {"text": "hi"})


def test_use_tool_preflights_mcp_tool_schema():
    out = _SearchStub()._unwrap_use_tool(
        {"name": "mcp_demo_echo", "input": {"bogus": 1}}
    )
    assert isinstance(out, str) and "unknown argument" in out


def test_no_manager_is_safe():
    # A stub with no _mcp (the additive-safe path) reports nothing and dispatches
    # nothing MCP-related.
    class _Bare:
        _is_mcp_tool = Bridge._is_mcp_tool
        _mcp_tools = Bridge._mcp_tools
        mcp_status = Bridge.mcp_status

    bare = _Bare()
    assert bare._is_mcp_tool("mcp_demo_echo") is False
    assert bare._mcp_tools() == []
    assert bare.mcp_status()["available"] is False


class _CredStub:
    _mcp_credential = Bridge._mcp_credential
    mcp_credential_options = Bridge.mcp_credential_options

    def __init__(self, entries):
        self._credentials = entries

    def _reload_credentials(self):
        pass

    def _credential_entry(self, cid):
        return next((c for c in self._credentials if c.get("id") == cid), None)


def test_mcp_credential_resolver_reads_secret_and_env(monkeypatch):
    from runspec_console import credentials as cred

    monkeypatch.setattr(cred, "get_secret", lambda cid: "S3CRET")
    stub = _CredStub([{"id": "c1", "label": "Deploy", "kind": "token", "username": ""}])
    resolved = stub._mcp_credential("c1")
    assert resolved["secret"] == "S3CRET"
    assert resolved["kind"] == "token"
    # name-derived env vars for a stdio server (Deploy + token → DEPLOY_TOKEN).
    assert resolved["env"].get("DEPLOY_TOKEN") == "S3CRET"


def test_mcp_credential_resolver_unknown_id_is_none():
    assert _CredStub([])._mcp_credential("missing") is None
    assert _CredStub([])._mcp_credential("") is None


def test_mcp_credential_options_lists_id_label_kind_no_secret():
    stub = _CredStub(
        [
            {"id": "c1", "label": "Deploy", "kind": "token", "username": "x"},
            {"id": "c2", "label": "", "kind": "userpass"},
            {"label": "no-id"},  # dropped — no id
        ]
    )
    opts = stub.mcp_credential_options()
    assert opts == [
        {"id": "c1", "label": "Deploy", "kind": "token"},
        {"id": "c2", "label": "c2", "kind": "userpass"},  # label falls back to id
    ]
    assert all("secret" not in o and "username" not in o for o in opts)


class _ConfigStub:
    """Backs get_mcp_config / save_mcp_config against an in-memory config.toml
    and a fake manager, so the Settings-tab save flow is testable end to end."""

    get_mcp_config = Bridge.get_mcp_config
    save_mcp_config = Bridge.save_mcp_config
    mcp_reload = Bridge.mcp_reload

    def __init__(self):
        self._mcp = _FakeMgr()


def _patch_config_store(monkeypatch):
    """Route read_config/write_config through an in-memory dict."""
    store: dict = {}
    monkeypatch.setattr(config, "read_config", lambda: dict(store))

    def _write(data):
        store.clear()
        store.update(data)

    monkeypatch.setattr(config, "write_config", _write)
    return store


def test_save_mcp_config_round_trips(monkeypatch):
    _patch_config_store(monkeypatch)
    stub = _ConfigStub()
    stub.save_mcp_config(
        {
            "servers": [
                {
                    "name": "search",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "credential": "cred-1",
                    "proxy": "corp:8080",
                }
            ]
        }
    )
    (s,) = stub.get_mcp_config()["servers"]
    assert s["name"] == "search" and s["transport"] == "http"
    assert s["url"] == "https://h/mcp"
    assert s["credential"] == "cred-1" and s["proxy"] == "corp:8080"


def test_save_mcp_config_drops_all_default_section(monkeypatch):
    store = _patch_config_store(monkeypatch)
    _ConfigStub().save_mcp_config({"servers": []})
    assert "mcp" not in store  # nothing worth persisting → no [mcp] block


def test_save_mcp_config_rejects_nameless_server(monkeypatch):
    _patch_config_store(monkeypatch)
    with pytest.raises(ValueError, match="needs a name"):
        _ConfigStub().save_mcp_config(
            {"servers": [{"transport": "stdio", "command": "x"}]}
        )


def test_save_mcp_config_rejects_duplicate_names(monkeypatch):
    _patch_config_store(monkeypatch)
    with pytest.raises(ValueError, match="Duplicate"):
        _ConfigStub().save_mcp_config(
            {
                "servers": [
                    {"name": "a", "command": "x"},
                    {"name": "a", "command": "y"},
                ]
            }
        )


def test_save_mcp_config_requires_url_for_http(monkeypatch):
    _patch_config_store(monkeypatch)
    with pytest.raises(ValueError, match="needs a url"):
        _ConfigStub().save_mcp_config({"servers": [{"name": "a", "transport": "http"}]})


def test_save_mcp_config_requires_command_for_stdio(monkeypatch):
    _patch_config_store(monkeypatch)
    with pytest.raises(ValueError, match="needs a command"):
        _ConfigStub().save_mcp_config(
            {"servers": [{"name": "a", "transport": "stdio"}]}
        )


def test_save_mcp_config_reloads_manager(monkeypatch):
    _patch_config_store(monkeypatch)
    stub = _ConfigStub()
    out = stub.save_mcp_config({"servers": [{"name": "a", "command": "x"}]})
    # save returns the manager status (reconnected).
    assert out["tool_count"] == 1  # from _FakeMgr.status()


def test_headless_filter_governs_mcp_tools_by_prefix():
    tools = [
        {"name": "fetch_url"},
        {"name": "mcp_demo_echo"},
        {"name": "mcp_demo_write"},
    ]
    # A scoped trigger naming only one MCP tool drops the other; ungoverned
    # built-ins (fetch_url) always pass.
    kept = {t["name"] for t in Bridge._filter_headless_tools(tools, ["mcp_demo_echo"])}
    assert kept == {"fetch_url", "mcp_demo_echo"}


# ── end-to-end over a real stdio MCPServer (skipped without the mcp extra) ────


_SERVER_SRC = textwrap.dedent(
    """
    import os
    from mcp.server import MCPServer

    srv = MCPServer("demo")

    @srv.tool()
    def echo(text: str) -> str:
        return f"echo: {text}"

    @srv.tool()
    def read_env() -> str:
        # Echoes an env var so a test can assert credential/proxy env injection.
        return os.environ.get("DEMO_TOKEN", "") + "|" + os.environ.get("HTTPS_PROXY", "")

    if __name__ == "__main__":
        srv.run("stdio")
    """
)


@pytest.mark.skipif(
    not mcp_client.available()[0], reason="mcp SDK not installed (mcp extra)"
)
def test_stdio_server_end_to_end(tmp_path):
    server = tmp_path / "srv.py"
    server.write_text(_SERVER_SRC)
    mgr = mcp_client.McpManager(
        config_reader=lambda: {
            "enabled": True,
            "servers": [
                {
                    "name": "demo",
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(server)],
                    "env": {},
                    "cwd": "",
                    "url": "",
                    "headers": {},
                    "enabled": True,
                    "autonomy": "",
                }
            ],
            "call_timeout": 30.0,
            "connect_timeout": 20.0,
        }
    )
    try:
        mgr.start()
        status = mgr.status()
        assert status["available"] is True
        (server_status,) = status["servers"]
        assert server_status["connected"] is True, server_status.get("error")
        assert mgr.is_mcp_tool("mcp_demo_echo")
        assert mgr.autonomy_for("mcp_demo_echo") == "confirm"
        assert "mcp_demo_echo" in {t["name"] for t in mgr.tools()}
        out = mgr.call_tool("mcp_demo_echo", {"text": "hi"})
        assert "echo: hi" in out
    finally:
        mgr.stop()
    # The registry is cleared once stopped — the subprocess is gone.
    assert not mgr.is_mcp_tool("mcp_demo_echo")


@pytest.mark.skipif(
    not mcp_client.available()[0], reason="mcp SDK not installed (mcp extra)"
)
def test_bad_stdio_server_is_recorded_not_raised():
    mgr = mcp_client.McpManager(
        config_reader=lambda: {
            "enabled": True,
            "servers": [
                {
                    "name": "broken",
                    "transport": "stdio",
                    "command": "/nonexistent/definitely-not-a-binary",
                    "args": [],
                    "env": {},
                    "cwd": "",
                    "url": "",
                    "headers": {},
                    "enabled": True,
                    "autonomy": "",
                }
            ],
            "call_timeout": 5.0,
            "connect_timeout": 8.0,
        }
    )
    try:
        mgr.start()  # a broken server must never raise into bring-up
        (server_status,) = mgr.status()["servers"]
        assert server_status["connected"] is False
        assert not mgr.is_mcp_tool("mcp_broken_anything")
    finally:
        mgr.stop()


@pytest.mark.skipif(
    not mcp_client.available()[0], reason="mcp SDK not installed (mcp extra)"
)
def test_stdio_credential_and_proxy_env_injected(tmp_path):
    # The resolved credential's env vars and the per-server proxy reach the
    # subprocess's environment (verified by a tool that echoes them back).
    server = tmp_path / "srv.py"
    server.write_text(_SERVER_SRC)
    mgr = mcp_client.McpManager(
        config_reader=lambda: {
            "enabled": True,
            "servers": [
                {
                    "name": "demo",
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(server)],
                    "env": {},
                    "cwd": "",
                    "url": "",
                    "headers": {},
                    "enabled": True,
                    "autonomy": "",
                    "credential": "c1",
                    "proxy": "corp-proxy:8080",
                }
            ],
            "call_timeout": 30.0,
            "connect_timeout": 20.0,
        },
        credential_resolver=lambda cid: {
            "kind": "token",
            "secret": "S3CRET",
            "env": {"DEMO_TOKEN": "S3CRET"},
        },
    )
    try:
        mgr.start()
        (server_status,) = mgr.status()["servers"]
        assert server_status["connected"] is True, server_status.get("error")
        out = mgr.call_tool("mcp_demo_read_env", {})
        assert "S3CRET|http://corp-proxy:8080" in out
    finally:
        mgr.stop()
