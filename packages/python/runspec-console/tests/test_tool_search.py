"""
test_tool_search.py — the search agent tool layout (``[llm] tool_mode = "search"``).

Search mode replaces the per-runnable tools with three fixed discovery meta-tools —
``search_runnables`` → ``run_runnable`` → ``describe_runnable`` — so the agent's
per-turn tool block stays ~constant no matter how many distinct runnables the fleet
installs. These tests cover the meta-tool layout, ranking + typed search rows,
``describe_runnable``, routing/unwrap, the pre-flight validator, autonomy + confirm
unwrap, secret splitting through the meta path, and a size headline.
"""

from __future__ import annotations

import json
import threading

import runspec_console.bridge as bridge_mod
from runspec_console.bridge import Bridge


class _Bridge:
    """Light harness binding the real search-mode + routing + dispatch methods."""

    def __init__(self, runnables, mode="search", credentials=None):
        self._lock = threading.Lock()
        self._runnables_cache = runnables
        self._credentials = credentials or []
        self._mode = mode
        # confirm-gate dispatch collaborators
        self._pending_confirms: dict = {}
        self._pending_confirm_meta: dict = {}
        self._dispatched: list = []

    def _dispatch(self, event, detail):
        self._dispatched.append((event, detail))

    def _notify_confirm(self, *a, **k):
        pass

    def get_config(self):
        return {"llm": {"tool_mode": self._mode}, "ssh": {}}

    def _reload_credentials(self):
        pass

    def _custom_agent_tools(self):
        return []  # isolate the runnable layout from file-transfer tools

    def _host_entry(self, host):
        return {"name": host, "ssh": None if host.endswith("local") else f"u@{host}"}

    def _become_params(self, cached, hostname):
        return "", "sudo", None

    # ── real methods under test ───────────────────────────────────────────────
    _tool_mode = Bridge._tool_mode
    _sanitize_tool_name = staticmethod(Bridge._sanitize_tool_name)
    _dedup_tool_name = Bridge._dedup_tool_name
    _declared_arg_name_set = staticmethod(Bridge._declared_arg_name_set)
    _host_selector_key = Bridge._host_selector_key
    _union_scoped_credentials = Bridge._union_scoped_credentials
    _allowed_hosts = Bridge._allowed_hosts
    _build_input_schema = Bridge._build_input_schema
    _runnables_to_tools = Bridge._runnables_to_tools
    _resolve_tool_target = Bridge._resolve_tool_target
    _effective_tool_autonomy = Bridge._effective_tool_autonomy
    _confirm_decision = Bridge._confirm_decision
    _run_tool_sync = Bridge._run_tool_sync
    # search-mode additions
    _search_mode_tools = Bridge._search_mode_tools
    _runnables_for_id = Bridge._runnables_for_id
    _resolve_id_host = Bridge._resolve_id_host
    _node_at = staticmethod(Bridge._node_at)
    _preflight_validate = Bridge._preflight_validate
    _search_runnables = Bridge._search_runnables
    _run_search_tool = Bridge._run_search_tool
    _run_describe_tool = Bridge._run_describe_tool
    _confirm_card_view = Bridge._confirm_card_view
    _register_confirm = Bridge._register_confirm
    # dispatch collaborators (no-ops with empty credentials)
    _scoped_credentials = Bridge._scoped_credentials
    _scope_matches = staticmethod(Bridge._scope_matches)
    _credential_entry = Bridge._credential_entry
    _apply_credentials = Bridge._apply_credentials
    _declared_arg_names = staticmethod(Bridge._declared_arg_names)
    _secret_arg_names = staticmethod(Bridge._secret_arg_names)
    _positionals_for = staticmethod(Bridge._positionals_for)


def _rn(host, name, group="fleet", args=None, description="", **extra):
    r = {
        "host": host,
        "name": name,
        "group": group,
        "args": args or [],
        "description": description,
    }
    r.update(extra)
    return r


def _names(tools):
    return {t["name"] for t in tools}


# ── layout ────────────────────────────────────────────────────────────────────


def test_search_mode_emits_only_meta_tools_regardless_of_fleet_size():
    small = _Bridge([_rn("a", "ping")])._runnables_to_tools()
    big = _Bridge(
        [_rn(f"h{i}", f"task-{j}") for i in range(18) for j in range(20)]
    )._runnables_to_tools()
    expected = {"search_runnables", "run_runnable", "describe_runnable"}
    assert _names(small) == expected
    assert _names(big) == expected  # constant footprint


def test_trigger_allowlist_falls_back_to_dedup_layout():
    # An allow-scoped (trigger) turn keeps the curated dedup tools, not search.
    runnables = [_rn("a", "ping"), _rn("b", "ping"), _rn("a", "restart")]
    tools = _Bridge(runnables)._runnables_to_tools(allow=["ping"])
    assert _names(tools) == {"fleet__ping"}


def test_run_runnable_schema_shape():
    tool = next(
        t
        for t in _Bridge([_rn("a", "ping")])._runnables_to_tools()
        if t["name"] == "run_runnable"
    )
    props = tool["input_schema"]["properties"]
    assert set(tool["input_schema"]["required"]) == {"id", "host"}
    assert props["args"]["type"] == "object"


# ── search ranking + typed rows ───────────────────────────────────────────────


def _search(runnables, query, **kw):
    return _Bridge(runnables)._search_runnables(query, **kw)


def test_search_returns_typed_rows_collapsed_by_group_name():
    runnables = [
        _rn(
            "a",
            "drain-node",
            description="cordon and drain",
            args=[
                {"name": "node", "type": "str", "required": True},
                {
                    "name": "region",
                    "type": "choice",
                    "options": ["us", "eu"],
                    "default": "us",
                },
            ],
        ),
        _rn(
            "b",
            "drain-node",
            description="cordon and drain",
            args=[
                {"name": "node", "type": "str", "required": True},
                {
                    "name": "region",
                    "type": "choice",
                    "options": ["us", "eu"],
                    "default": "us",
                },
            ],
        ),
    ]
    out = _search(runnables, "drain")
    assert out["count"] == 1
    row = out["results"][0]
    assert row["id"] == "fleet__drain-node"
    assert row["hosts"] == ["a", "b"]
    node = next(a for a in row["args"] if a["name"] == "node")
    region = next(a for a in row["args"] if a["name"] == "region")
    assert node["required"] is True
    assert region["enum"] == ["us", "eu"] and region["default"] == "us"


def test_search_omits_password_args():
    runnables = [
        _rn(
            "a",
            "login",
            args=[
                {"name": "user", "type": "str"},
                {"name": "pw", "type": "password"},
            ],
        )
    ]
    row = _search(runnables, "login")["results"][0]
    assert {a["name"] for a in row["args"]} == {"user"}  # secret omitted


def test_search_ranks_name_over_description_and_filters():
    runnables = [
        _rn("a", "restart-service", description="bounce a unit"),
        _rn("a", "check-memory", description="restart pressure stats"),
        _rn("b", "restart-service", description="bounce a unit"),
    ]
    out = _search(runnables, "restart")
    # name hit (restart-service) ranks above description-only hit (check-memory)
    assert out["results"][0]["name"] == "restart-service"
    # host filter narrows *which* runnables appear (restart-service is on b),
    # but the row still reports every host the runnable runs on (a and b).
    only_b = _search(runnables, "restart", host="b")
    assert {r["name"] for r in only_b["results"]} == {"restart-service"}
    assert only_b["results"][0]["hosts"] == ["a", "b"]


def test_search_empty_query_browses():
    runnables = [_rn("a", "one"), _rn("a", "two")]
    out = _search(runnables, "")
    assert {r["name"] for r in out["results"]} == {"one", "two"}


def test_search_caps_and_flags_truncation():
    runnables = [_rn("a", f"task-{i:02d}", description="job") for i in range(15)]
    out = _Bridge(runnables)._search_runnables("job", limit=10)
    assert out["count"] == 10 and out.get("truncated") is True


def test_run_search_tool_returns_json():
    payload = json.loads(
        _Bridge([_rn("a", "ping")])._run_search_tool({"query": "ping"})
    )
    assert payload["results"][0]["name"] == "ping"


# ── describe ──────────────────────────────────────────────────────────────────


def test_describe_returns_full_schema_with_host_enum():
    runnables = [
        _rn("a", "ping", args=[{"name": "count", "type": "int"}]),
        _rn("b", "ping", args=[{"name": "count", "type": "int"}]),
    ]
    out = json.loads(_Bridge(runnables)._run_describe_tool({"id": "fleet__ping"}))
    props = out["input_schema"]["properties"]
    assert props["host"]["enum"] == ["a", "b"]
    assert "count" in props


def test_describe_unknown_id_errors():
    out = _Bridge([_rn("a", "ping")])._run_describe_tool({"id": "fleet__nope"})
    assert out.startswith("Error:")


# ── routing / unwrap ──────────────────────────────────────────────────────────


def test_resolve_id_host_picks_by_host():
    b = _Bridge([_rn("a", "ping"), _rn("b", "ping")])
    host, name, cached = b._resolve_id_host("fleet__ping", "b")
    assert (host, name) == ("b", "ping") and cached["host"] == "b"


def test_resolve_id_host_defaults_single_host():
    host, name, _c = _Bridge([_rn("only", "ping")])._resolve_id_host("fleet__ping", "")
    assert (host, name) == ("only", "ping")


def test_resolve_id_host_errors():
    b = _Bridge([_rn("a", "ping"), _rn("b", "ping")])
    assert isinstance(b._resolve_id_host("fleet__ping", ""), str)  # multi, no host
    assert isinstance(b._resolve_id_host("fleet__ping", "zzz"), str)  # bad host
    assert isinstance(b._resolve_id_host("fleet__nope", "a"), str)  # unknown id


# ── pre-flight validation ─────────────────────────────────────────────────────


def _pf(args, spec_args=None, **runnable_kw):
    cached = _rn("a", "x", args=spec_args or [], **runnable_kw)
    return _Bridge([cached])._preflight_validate(cached, [], args)


def test_preflight_passes_valid():
    assert _pf({"n": 5}, [{"name": "n", "type": "int", "range": [1, 10]}]) is None


def test_preflight_missing_required():
    err = _pf({}, [{"name": "node", "type": "str", "required": True}])
    assert err and "missing required" in err


def test_preflight_unknown_arg():
    err = _pf({"bogus": 1}, [{"name": "n", "type": "int"}])
    assert err and "unknown argument" in err


def test_preflight_bad_choice():
    err = _pf({"r": "zz"}, [{"name": "r", "type": "choice", "options": ["us", "eu"]}])
    assert err and "must be one of" in err


def test_preflight_int_and_range():
    assert "must be an integer" in _pf({"n": "x"}, [{"name": "n", "type": "int"}])
    err = _pf({"n": 99}, [{"name": "n", "type": "int", "range": [1, 10]}])
    assert err and "between 1 and 10" in err


def test_preflight_require_command():
    cached = {
        "host": "a",
        "name": "x",
        "group": "fleet",
        "args": [],
        "requireCommand": True,
        "commands": {"drain": {"args": []}},
    }
    err = _Bridge([cached])._preflight_validate(cached, [], {})
    assert err and "requires a command" in err


# ── dispatch through run_runnable ─────────────────────────────────────────────


def test_run_runnable_dispatches_to_chosen_host(monkeypatch):
    captured = {}

    def fake_run_remote(
        ssh, rp, runnable, args, command_path, on_line, on_done, idf, **kw
    ):
        captured.update(where="remote", ssh=ssh, args=args, runnable=runnable)
        on_done(0, 1)

    monkeypatch.setattr(bridge_mod, "run_remote", fake_run_remote)
    monkeypatch.setattr(bridge_mod, "_paths", lambda e: ["/bin/runspec"])
    monkeypatch.setattr(bridge_mod, "venv_name", lambda p: "fleet")

    runnables = [
        _rn("h1local", "ping", args=[{"name": "count", "type": "int"}]),
        _rn("h2", "ping", args=[{"name": "count", "type": "int"}]),
    ]
    out = _Bridge(runnables)._run_tool_sync(
        "run_runnable", {"id": "fleet__ping", "host": "h2", "args": {"count": 3}}
    )
    assert captured["where"] == "remote" and captured["ssh"] == "u@h2"
    assert captured["args"] == {"count": 3} and captured["runnable"] == "ping"
    assert not out.startswith("Error")


def test_run_runnable_preflight_blocks_before_dispatch(monkeypatch):
    called = {"ran": False}
    monkeypatch.setattr(
        bridge_mod, "run_local", lambda *a, **k: called.update(ran=True)
    )
    monkeypatch.setattr(bridge_mod, "_paths", lambda e: ["/bin/runspec"])
    monkeypatch.setattr(bridge_mod, "venv_name", lambda p: "fleet")

    runnables = [
        _rn(
            "h1local", "deploy", args=[{"name": "env", "type": "str", "required": True}]
        )
    ]
    out = _Bridge(runnables)._run_tool_sync(
        "run_runnable", {"id": "fleet__deploy", "host": "h1local", "args": {}}
    )
    assert out.startswith("Error:") and "missing required" in out
    assert called["ran"] is False  # never dispatched


def test_run_runnable_splits_password_into_secrets(monkeypatch):
    captured = {}

    def fake_run_local(rp, runnable, args, command_path, on_line, on_done, **kw):
        captured.update(args=args, secrets=kw.get("secrets"))
        on_done(0, 1)

    monkeypatch.setattr(bridge_mod, "run_local", fake_run_local)
    monkeypatch.setattr(bridge_mod, "_paths", lambda e: ["/bin/runspec"])
    monkeypatch.setattr(bridge_mod, "venv_name", lambda p: "fleet")

    runnables = [
        _rn(
            "h1local",
            "login",
            args=[
                {"name": "user", "type": "str"},
                {"name": "pw", "type": "password"},
            ],
        )
    ]
    _Bridge(runnables)._run_tool_sync(
        "run_runnable",
        {
            "id": "fleet__login",
            "host": "h1local",
            "args": {"user": "svc", "pw": "s3cret"},
        },
    )
    assert captured["args"] == {"user": "svc"}  # secret kept off argv
    assert captured["secrets"] == {"pw": "s3cret"}


# ── autonomy + confirm unwrap ─────────────────────────────────────────────────


def test_autonomy_reflects_inner_runnable():
    runnables = [
        _rn("a", "ping", autonomy="autonomous"),
        _rn("b", "ping", autonomy="manual"),
    ]
    b = _Bridge(runnables)
    assert (
        b._effective_tool_autonomy("run_runnable", {"id": "fleet__ping", "host": "a"})
        == "autonomous"
    )
    assert (
        b._effective_tool_autonomy("run_runnable", {"id": "fleet__ping", "host": "b"})
        == "manual"
    )


def test_readonly_meta_tools_are_autonomous():
    b = _Bridge([_rn("a", "ping")])
    assert (
        b._effective_tool_autonomy("search_runnables", {"query": "x"}) == "autonomous"
    )
    assert (
        b._effective_tool_autonomy("describe_runnable", {"id": "fleet__ping"})
        == "autonomous"
    )


def test_autonomy_per_arg_escalation_through_meta():
    runnables = [
        _rn(
            "a",
            "wipe",
            autonomy="confirm",
            args=[{"name": "force", "type": "flag", "autonomy": "manual"}],
        )
    ]
    b = _Bridge(runnables)
    base = b._effective_tool_autonomy(
        "run_runnable", {"id": "fleet__wipe", "host": "a"}
    )
    esc = b._effective_tool_autonomy(
        "run_runnable", {"id": "fleet__wipe", "host": "a", "args": {"force": True}}
    )
    assert base == "confirm" and esc == "manual"


def test_confirm_card_unwraps_to_real_runnable():
    runnables = [_rn("a", "drain-node")]
    name, view = _Bridge(runnables)._confirm_card_view(
        "run_runnable", {"id": "fleet__drain-node", "host": "a", "args": {"node": "n1"}}
    )
    assert name == "drain-node"
    assert view == {"host": "a", "node": "n1"}


def test_run_runnable_confirm_keeps_raw_name_for_segment_match():
    # The confirm event must carry the *raw* tool_name ("run_runnable") so the UI
    # can match it to the running tool-call segment (segment.entry.name == tc.name)
    # and render Approve/Deny. The unwrapped runnable rides along as display_name
    # for the human-readable card — conflating the two (sending the unwrapped name
    # as tool_name) is what hid the buttons for search-mode calls.
    b = _Bridge([_rn("a", "drain-node")])
    b._register_confirm(
        "chat1",
        "run_runnable",
        {"id": "fleet__drain-node", "host": "a", "args": {"node": "n1"}},
        "confirm",
    )
    event, detail = b._dispatched[0]
    assert event == "runspec:tool_confirm"
    assert detail["tool_name"] == "run_runnable"  # matches the segment
    assert detail["tool_input"] == {
        "id": "fleet__drain-node",
        "host": "a",
        "args": {"node": "n1"},
    }
    assert detail["display_name"] == "drain-node"  # human-readable resolved runnable
    assert detail["display_input"] == {"host": "a", "node": "n1"}


def test_plain_tool_confirm_display_equals_raw():
    # For a non-search call, display_* mirror tool_* (no unwrapping), so the card
    # is unchanged and matching still works.
    b = _Bridge([_rn("a", "ping")])
    b._register_confirm("chat1", "fleet__ping", {"count": 3}, "confirm")
    _, detail = b._dispatched[0]
    assert detail["tool_name"] == "fleet__ping"
    assert detail["display_name"] == "fleet__ping"
    assert detail["display_input"] == {"count": 3}


# ── headline size comparison ──────────────────────────────────────────────────


def test_search_is_a_constant_small_tool_block(capsys):
    # A multi-package fleet: many distinct runnables, several hosts each.
    hosts = [f"host{i:02d}" for i in range(12)]
    names = [f"task-{i:02d}" for i in range(72)]
    runnables = [
        _rn(
            h,
            n,
            description=f"do {n}",
            args=[{"name": "region", "type": "choice", "options": ["us", "eu"]}],
        )
        for h in hosts
        for n in names
    ]
    per_host = _Bridge(runnables, mode="per_host")._runnables_to_tools()
    dedup = _Bridge(runnables, mode="dedup")._runnables_to_tools()
    search = _Bridge(runnables, mode="search")._runnables_to_tools()

    sizes = {
        k: len(json.dumps(v))
        for k, v in (("per_host", per_host), ("dedup", dedup), ("search", search))
    }
    assert len(search) == 3
    assert sizes["search"] * 10 < sizes["dedup"] < sizes["per_host"]

    with capsys.disabled():
        print(
            f"\n  tool-list across {len(hosts)} hosts × {len(names)} distinct runnables:"
            f"\n    per_host : {len(per_host):>4} tools  {sizes['per_host']:>8} json bytes"
            f"\n    dedup    : {len(dedup):>4} tools  {sizes['dedup']:>8} json bytes"
            f"\n    search   : {len(search):>4} tools  {sizes['search']:>8} json bytes\n"
        )
