"""
test_tool_dedup.py — the deduplicated agent tool layout (``[llm] tool_mode``).

The default ``"dedup"`` layout collapses the N identical per-host copies of a
runnable a uniform fleet produces into a single tool per ``(group, name)``, with a
required ``host`` enum naming the hosts that have it — slashing the tool-list size
the agent re-sends every turn. ``"per_host"`` restores the original
one-tool-per-(host, runnable) layout as an escape hatch.

These tests cover the builder (``_runnables_to_tools``), the routing helper
(``_resolve_tool_target``), the autonomy gate, dispatch (``_run_tool_sync``), the
allowlist host-scoping, and print the size comparison across an 18-host fleet.
"""

from __future__ import annotations

import json
import threading

from runspec_console.bridge import Bridge


class _Bridge:
    """A light harness binding the real tool-layout + routing methods."""

    def __init__(self, runnables, mode="dedup", credentials=None):
        self._lock = threading.Lock()
        self._runnables_cache = runnables
        self._credentials = credentials or []
        self._mode = mode

    def get_config(self):
        return {"llm": {"tool_mode": self._mode}}

    def _reload_credentials(self):
        pass

    def _custom_agent_tools(self):
        # Isolate the runnable-tool layout from the built-in file-transfer tools.
        return []

    def _host_entry(self, host):
        # local for "h*local", remote otherwise — enough for dispatch routing.
        return {"name": host, "ssh": None if host.endswith("local") else f"u@{host}"}

    # Configured hosts = whatever the cache carries, plus any extras a test opts
    # in (to exercise the configured-but-cold-host resolver fallback).
    _extra_hosts: set = set()

    def _configured_host_names(self):
        return {
            r.get("host", "local") for r in self._runnables_cache
        } | self._extra_hosts

    def _become_params(self, cached, hostname):
        return "", "sudo", None

    # ── real methods under test ───────────────────────────────────────────────
    _tool_mode = Bridge._tool_mode
    _sanitize_tool_name = staticmethod(Bridge._sanitize_tool_name)
    _dedup_tool_name = Bridge._dedup_tool_name
    _dedup_tool_aliases = Bridge._dedup_tool_aliases
    _declared_arg_name_set = staticmethod(Bridge._declared_arg_name_set)
    _host_selector_key = Bridge._host_selector_key
    _union_scoped_credentials = Bridge._union_scoped_credentials
    _allowed_hosts = Bridge._allowed_hosts
    _build_input_schema = Bridge._build_input_schema
    _runnables_to_tools = Bridge._runnables_to_tools
    _visible_runnables = Bridge._visible_runnables
    _resolve_tool_target = Bridge._resolve_tool_target
    _effective_tool_autonomy = Bridge._effective_tool_autonomy
    _run_tool_sync = Bridge._run_tool_sync

    _finalize_tool_output = Bridge._finalize_tool_output
    _tool_output_cap = Bridge._tool_output_cap
    # collaborators of the real methods (no-ops with empty credentials)
    _scoped_credentials = Bridge._scoped_credentials
    _scope_matches = staticmethod(Bridge._scope_matches)
    _has_scope = staticmethod(Bridge._has_scope)
    _declared_use = staticmethod(Bridge._declared_use)
    _declared_interface = Bridge._declared_interface
    _credential_entry = Bridge._credential_entry
    _apply_credentials = Bridge._apply_credentials
    _credential_env = Bridge._credential_env
    _declared_arg_names = staticmethod(Bridge._declared_arg_names)
    _secret_arg_names = staticmethod(Bridge._secret_arg_names)
    _positionals_for = staticmethod(Bridge._positionals_for)


def _rn(host, name, group="fleet", args=None, **extra):
    r = {"host": host, "name": name, "group": group, "args": args or []}
    r.update(extra)
    return r


def _names(tools):
    return {t["name"] for t in tools}


# ── layout: counts ────────────────────────────────────────────────────────────


def test_dedup_collapses_identical_runnables_to_one_tool_per_name():
    hosts = ["a", "b", "c"]
    runnables = [_rn(h, n) for h in hosts for n in ("ping", "restart")]
    tools = _Bridge(runnables, mode="dedup")._runnables_to_tools()
    # 3 hosts × 2 runnables → 2 deduped tools, named for the runnable itself
    # (no venv/package prefix — see test_same_name_across_venvs_collapses).
    assert _names(tools) == {"ping", "restart"}


def test_per_host_mode_keeps_one_tool_per_host_runnable():
    hosts = ["a", "b", "c"]
    runnables = [_rn(h, n) for h in hosts for n in ("ping", "restart")]
    tools = _Bridge(runnables, mode="per_host")._runnables_to_tools()
    assert _names(tools) == {
        "a__ping",
        "a__restart",
        "b__ping",
        "b__restart",
        "c__ping",
        "c__restart",
    }


def test_invalid_mode_falls_back_to_dedup():
    runnables = [_rn("a", "ping"), _rn("b", "ping")]
    tools = _Bridge(runnables, mode="bogus")._runnables_to_tools()
    assert _names(tools) == {"ping"}


# ── layout: the host enum ─────────────────────────────────────────────────────


def test_host_enum_lists_every_host_and_is_required():
    runnables = [_rn(h, "ping") for h in ("a", "b", "c")]
    tool = _Bridge(runnables)._runnables_to_tools()[0]
    schema = tool["input_schema"]
    assert schema["properties"]["host"]["enum"] == ["a", "b", "c"]
    assert schema["required"][0] == "host"


def test_single_host_runnable_still_carries_required_host_enum():
    # Uniform layout: even a one-host runnable gets the (single-value) host enum.
    tool = _Bridge([_rn("only", "ping")])._runnables_to_tools()[0]
    assert tool["input_schema"]["properties"]["host"]["enum"] == ["only"]
    assert "host" in tool["input_schema"]["required"]


def test_same_name_across_venvs_collapses_to_one_tool_spanning_hosts():
    # The regression this dedup key fixes. A cross-platform runnable ships in more
    # than one package, and a locally-discovered runnable's "group" is the venv
    # folder — so keying on (group, name) split `which` into console__which (local
    # only) and runspec_linux__which (web-01 only). Two single-host tools, neither
    # able to reach the other's host: host targeting broken by an accident of
    # packaging. One name is now one tool whose host enum spans every host.
    runnables = [
        _rn("local", "which", group="console"),
        _rn("web-01", "which", group="runspec_linux"),
    ]
    tools = _Bridge(runnables)._runnables_to_tools()
    assert _names(tools) == {"which"}
    assert tools[0]["input_schema"]["properties"]["host"]["enum"] == ["local", "web-01"]


def test_legacy_group_qualified_name_still_routes():
    # The qualified form is no longer advertised, but it is written down in places
    # the console does not own (a trigger allowlist, a saved runbook step, an eval
    # fixture), so it must keep resolving — scoped to that venv.
    runnables = [
        _rn("local", "which", group="console"),
        _rn("web-01", "which", group="runspec_linux"),
    ]
    b = _Bridge(runnables)
    host, name, cached, _key = b._resolve_tool_target(
        "console__which", {"host": "local"}
    )
    assert (host, name) == ("local", "which")
    assert cached["group"] == "console"


def test_dispatch_picks_the_venv_of_the_chosen_host():
    # A name-deduped tool spans venvs, so the cached record handed to the executor
    # must be the one discovered on the chosen host — that record's `group` is the
    # venv the run executes in.
    runnables = [
        _rn("local", "which", group="console"),
        _rn("web-01", "which", group="runspec_linux"),
    ]
    b = _Bridge(runnables)
    _h, _n, cached, _k = b._resolve_tool_target("which", {"host": "web-01"})
    assert cached["group"] == "runspec_linux"


def test_host_arg_collision_renames_selector_key():
    # A runnable that declares its own `host` arg must not be shadowed: the
    # injected selector falls back to `_host`, and the real arg survives.
    runnables = [
        _rn(h, "conn", args=[{"name": "host", "type": "str"}]) for h in ("a", "b")
    ]
    tool = _Bridge(runnables)._runnables_to_tools()[0]
    props = tool["input_schema"]["properties"]
    assert props["_host"]["enum"] == ["a", "b"]  # selector
    assert props["host"]["type"] == "string"  # the runnable's own arg
    assert "_host" in tool["input_schema"]["required"]


# ── routing (_resolve_tool_target) ────────────────────────────────────────────


def test_resolve_dedup_picks_runnable_by_host_arg():
    runnables = [_rn("a", "ping"), _rn("b", "ping")]
    b = _Bridge(runnables)
    host, name, cached, host_key = b._resolve_tool_target("fleet__ping", {"host": "b"})
    assert (host, name, host_key) == ("b", "ping", "host")
    assert cached["host"] == "b"


def test_resolve_dedup_missing_host_when_multi_is_an_error():
    b = _Bridge([_rn("a", "ping"), _rn("b", "ping")])
    out = b._resolve_tool_target("fleet__ping", {})
    assert isinstance(out, str) and "set the 'host' argument" in out


def test_resolve_dedup_single_host_defaults_without_host_arg():
    b = _Bridge([_rn("only", "ping")])
    host, name, _cached, _key = b._resolve_tool_target("fleet__ping", {})
    assert (host, name) == ("only", "ping")


def test_resolve_dedup_unknown_host_is_an_error():
    b = _Bridge([_rn("a", "ping"), _rn("b", "ping")])
    out = b._resolve_tool_target("fleet__ping", {"host": "zzz"})
    assert isinstance(out, str) and "does not have" in out


def test_resolve_dedup_falls_back_to_configured_cold_host():
    """A configured-but-cold host resolves by reusing a warm host's runnable
    definition (execution lazy-connects) instead of erroring."""
    b = _Bridge([_rn("warm", "ping")])
    b._extra_hosts = {"cold"}  # configured, no runnable cached for it
    host, name, cached, key = b._resolve_tool_target("fleet__ping", {"host": "cold"})
    assert (host, name, key) == ("cold", "ping", "host")
    assert cached["host"] == "cold"  # synthesized for the cold host


def test_resolve_dedup_falls_back_to_explicit_host__name():
    # Internal callers (e.g. helpdesk portal) build host__name directly; that must
    # still resolve under dedup mode even though it isn't a group__name.
    b = _Bridge([_rn("web01", "deploy", group="ops")])
    host, name, cached, host_key = b._resolve_tool_target("web01__deploy", {})
    assert (host, name, host_key) == ("web01", "deploy", None)
    assert cached is not None


def test_resolve_per_host_splits_tool_name():
    b = _Bridge([_rn("a", "ping")], mode="per_host")
    host, name, _cached, host_key = b._resolve_tool_target("a__ping", {})
    assert (host, name, host_key) == ("a", "ping", None)


# ── autonomy reflects the selected host ───────────────────────────────────────


def test_autonomy_uses_selected_host_level():
    runnables = [
        _rn("a", "ping", autonomy="autonomous"),
        _rn("b", "ping", autonomy="confirm"),
    ]
    b = _Bridge(runnables)
    assert b._effective_tool_autonomy("fleet__ping", {"host": "a"}) == "autonomous"
    assert b._effective_tool_autonomy("fleet__ping", {"host": "b"}) == "confirm"


def test_autonomy_unresolved_host_defaults_confirm():
    b = _Bridge([_rn("a", "ping"), _rn("b", "ping")])
    # multi-host with no host arg can't resolve → gated by default
    assert b._effective_tool_autonomy("fleet__ping", {}) == "confirm"


# ── dispatch (_run_tool_sync) targets the chosen host ─────────────────────────


def test_dispatch_pops_host_and_targets_chosen_host(monkeypatch):
    captured = {}

    def fake_run_local(rp, runnable, args, command_path, on_line, on_done, **kw):
        captured["where"] = "local"
        captured["args"] = args
        on_done(0, 1)

    def fake_run_remote(
        ssh, rp, runnable, args, command_path, on_line, on_done, idf, **kw
    ):
        captured["where"] = "remote"
        captured["ssh"] = ssh
        captured["args"] = args
        on_done(0, 1)

    monkeypatch.setattr("runspec_console.executor.run_local", fake_run_local)
    monkeypatch.setattr("runspec_console.executor.run_remote", fake_run_remote)
    monkeypatch.setattr(
        "runspec_console.bridge_utils._paths", lambda entry: ["/bin/runspec"]
    )
    monkeypatch.setattr("runspec_console.hosts.venv_name", lambda p: "fleet")

    runnables = [
        _rn("h1local", "ping", args=[{"name": "count", "type": "int"}]),
        _rn("h2", "ping", args=[{"name": "count", "type": "int"}]),
    ]
    b = _Bridge(runnables)
    # pick the remote host explicitly
    b._run_tool_sync("fleet__ping", {"host": "h2", "count": 3})
    assert captured["where"] == "remote"
    assert captured["ssh"] == "u@h2"
    assert "host" not in captured["args"]  # selector stripped
    assert captured["args"]["count"] == 3


def test_dispatch_renamed_selector_strips_underscore_host(monkeypatch):
    captured = {}

    def fake_run_local(rp, runnable, args, command_path, on_line, on_done, **kw):
        captured["args"] = args
        on_done(0, 1)

    monkeypatch.setattr("runspec_console.executor.run_local", fake_run_local)
    monkeypatch.setattr(
        "runspec_console.bridge_utils._paths", lambda entry: ["/bin/runspec"]
    )
    monkeypatch.setattr("runspec_console.hosts.venv_name", lambda p: "fleet")

    runnables = [_rn("h1local", "conn", args=[{"name": "host", "type": "str"}])]
    b = _Bridge(runnables)
    b._run_tool_sync("fleet__conn", {"_host": "h1local", "host": "myserver"})
    assert "_host" not in captured["args"]  # selector stripped
    assert captured["args"]["host"] == "myserver"  # real arg preserved


# ── allowlist host-scoping ────────────────────────────────────────────────────


def _allow_tool(allow):
    runnables = [_rn("web", "deploy"), _rn("db", "deploy"), _rn("web", "backup")]
    return _Bridge(runnables)._runnables_to_tools(allow=allow)


def test_allow_bare_name_admits_every_host():
    tools = {t["name"]: t for t in _allow_tool(["deploy"])}
    assert set(tools) == {"deploy"}
    assert tools["deploy"]["input_schema"]["properties"]["host"]["enum"] == [
        "web",
        "db",
    ]


def test_allow_legacy_group_qualified_name_admits_every_host():
    # An existing trigger allowlist spelling the old group__name form keeps working.
    tools = {t["name"]: t for t in _allow_tool(["fleet__deploy"])}
    assert set(tools) == {"deploy"}
    assert tools["deploy"]["input_schema"]["properties"]["host"]["enum"] == [
        "web",
        "db",
    ]


def test_allow_host_qualified_narrows_the_host_enum():
    tools = {t["name"]: t for t in _allow_tool(["web__deploy"])}
    assert set(tools) == {"deploy"}
    # scoped to the one allowed host even though the tool is shared
    assert tools["deploy"]["input_schema"]["properties"]["host"]["enum"] == ["web"]


def test_allow_unknown_yields_no_tools():
    assert _allow_tool(["nope"]) == []


# ── credentials union across hosts ────────────────────────────────────────────


def test_credential_enum_is_union_across_hosts():
    creds = [
        {"id": "web-cred", "scope_hosts": ["web"], "scope_runnables": ["deploy"]},
        {"id": "db-cred", "scope_hosts": ["db"], "scope_runnables": ["deploy"]},
    ]
    runnables = [_rn("web", "deploy"), _rn("db", "deploy")]
    tool = _Bridge(runnables, credentials=creds)._runnables_to_tools()[0]
    assert set(tool["input_schema"]["properties"]["credential_id"]["enum"]) == {
        "web-cred",
        "db-cred",
    }


# ── the headline: size comparison across an 18-host fleet ──────────────────────


def test_dedup_shrinks_the_tool_list_across_a_fleet(capsys):
    hosts = [f"host{i:02d}" for i in range(18)]
    runnable_names = [f"task-{i:02d}" for i in range(20)]
    runnables = [
        _rn(
            h,
            n,
            description=f"Run {n}",
            args=[
                {"name": "region", "type": "choice", "options": ["us", "eu"]},
                {"name": "force", "type": "flag", "description": "skip confirm"},
            ],
        )
        for h in hosts
        for n in runnable_names
    ]
    per_host = _Bridge(runnables, mode="per_host")._runnables_to_tools()
    dedup = _Bridge(runnables, mode="dedup")._runnables_to_tools()

    per_host_bytes = len(json.dumps(per_host))
    dedup_bytes = len(json.dumps(dedup))

    # 18×20 = 360 tools collapse to 20.
    assert len(per_host) == 360
    assert len(dedup) == 20
    # Far smaller payload (the per-host copies were identical bar the host).
    assert dedup_bytes * 5 < per_host_bytes

    with capsys.disabled():
        print(
            f"\n  tool-list across {len(hosts)} hosts × {len(runnable_names)} runnables:"
            f"\n    per_host : {len(per_host):>4} tools  {per_host_bytes:>7} json bytes"
            f"  (~{per_host_bytes // 4} tok)"
            f"\n    dedup    : {len(dedup):>4} tools  {dedup_bytes:>7} json bytes"
            f"  (~{dedup_bytes // 4} tok)"
            f"\n    reduction: tools -{100 - len(dedup) * 100 // len(per_host)}%, "
            f"bytes -{100 - dedup_bytes * 100 // per_host_bytes}%\n"
        )
