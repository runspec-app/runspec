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

import pytest

from runspec_console.bridge import Bridge
from tests.session_stub import install_sessions


@pytest.fixture(autouse=True)
def _isolated_runbook_store(tmp_path, monkeypatch):
    """Point the runbook store at a temp dir for every test in this module.

    ``_search_runnables`` reads ``memory.load_runbooks()`` (runbooks are a second
    result kind), and the store is resolved from ``APPDATA`` at call time — without
    this, running the suite would read the developer's real
    ``runspec_runbooks.toml`` and the results would depend on their machine."""
    monkeypatch.setenv("APPDATA", str(tmp_path))


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
        install_sessions(self)

    def _dispatch(self, event, detail):
        self._dispatched.append((event, detail))

    _current_workspace_root = Bridge._current_workspace_root

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
    _defer_builtins_enabled = Bridge._defer_builtins_enabled
    _deferred_builtin_tools = Bridge._deferred_builtin_tools
    _builtin_meta_tools = Bridge._builtin_meta_tools
    _resolve_tool_target = Bridge._resolve_tool_target
    _fallback_for_host = Bridge._fallback_for_host
    _arg_hosts = staticmethod(Bridge._arg_hosts)
    _effective_tool_autonomy = Bridge._effective_tool_autonomy
    _confirm_decision = Bridge._confirm_decision
    _run_tool_sync = Bridge._run_tool_sync
    _finalize_tool_output = Bridge._finalize_tool_output
    _tool_output_cap = Bridge._tool_output_cap
    # search-mode additions
    _search_mode_tools = Bridge._search_mode_tools
    _coerce_run_args = staticmethod(Bridge._coerce_run_args)
    _runnables_for_id = Bridge._runnables_for_id
    _resolve_id_host = Bridge._resolve_id_host
    _node_at = staticmethod(Bridge._node_at)
    _preflight_validate = Bridge._preflight_validate
    _search_runnables = Bridge._search_runnables
    _runs_as_by_host = Bridge._runs_as_by_host
    _confirm_runs_as = Bridge._confirm_runs_as
    _search_host_groups = Bridge._search_host_groups
    _is_literal_host = Bridge._is_literal_host
    _command_arg_rows = staticmethod(Bridge._command_arg_rows)
    _search_runbooks = Bridge._search_runbooks
    _score_runbook = staticmethod(Bridge._score_runbook)
    _runbook_id_hint = staticmethod(Bridge._runbook_id_hint)
    _run_search_tool = Bridge._run_search_tool
    _run_describe_tool = Bridge._run_describe_tool
    _confirm_card_view = Bridge._confirm_card_view
    _register_confirm = Bridge._register_confirm
    # dispatch collaborators (no-ops with empty credentials)
    _scoped_credentials = Bridge._scoped_credentials
    _scope_matches = staticmethod(Bridge._scope_matches)
    _has_scope = staticmethod(Bridge._has_scope)
    _declared_use = staticmethod(Bridge._declared_use)
    _declared_interface = Bridge._declared_interface
    _credential_entry = Bridge._credential_entry
    _unknown_credential_error = Bridge._unknown_credential_error
    _apply_credentials = Bridge._apply_credentials
    _credential_env = Bridge._credential_env
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
    assert _names(tools) == {"ping"}


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


def test_search_returns_typed_rows_collapsed_by_name():
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
    assert row["id"] == "drain-node"
    assert row["hosts"] == ["a", "b"]
    node = next(a for a in row["args"] if a["name"] == "node")
    region = next(a for a in row["args"] if a["name"] == "region")
    assert node["required"] is True
    assert region["enum"] == ["us", "eu"] and region["default"] == "us"


def test_search_collapses_the_same_name_across_venvs():
    # Search mode's row id is the dedup tool name, so it inherited the same split:
    # `which` from the console's own venv and from runspec_linux on a fleet host
    # came back as two rows, each offering only its own host. One runnable is one
    # row whose hosts span every host that has it.
    runnables = [
        _rn("local", "which", group="console", description="Find a command."),
        _rn("web-01", "which", group="runspec_linux", description="Find a command."),
    ]
    out = _search(runnables, "which")
    assert out["count"] == 1
    assert out["results"][0]["id"] == "which"
    assert out["results"][0]["hosts"] == ["local", "web-01"]


def test_search_group_filter_matches_any_venv_of_a_collapsed_row():
    # The row spans venvs, so filtering by either one must still find it —
    # equality against a single "group" would hide it from one of them.
    runnables = [
        _rn("local", "which", group="console", description="Find a command."),
        _rn("web-01", "which", group="runspec_linux", description="Find a command."),
    ]
    for grp in ("console", "runspec_linux"):
        out = _search(runnables, "which", group=grp)
        assert out["count"] == 1, grp
        assert out["results"][0]["hosts"] == ["local", "web-01"]
    assert _search(runnables, "which", group="nope")["count"] == 0


def test_run_runnable_id_resolves_across_venvs():
    # The collapsed id must reach the host the agent picked, whichever venv that
    # host's copy lives in — and the cached record handed on carries that venv.
    runnables = [
        _rn("local", "which", group="console"),
        _rn("web-01", "which", group="runspec_linux"),
    ]
    b = _Bridge(runnables)
    host, name, cached = b._resolve_id_host("which", "web-01")
    assert (host, name) == ("web-01", "which")
    assert cached["group"] == "runspec_linux"


def test_run_runnable_legacy_group_qualified_id_still_resolves():
    # A saved eval fixture / runbook step still spelling the old group__name id
    # keeps working, scoped to that venv.
    runnables = [
        _rn("local", "which", group="console"),
        _rn("web-01", "which", group="runspec_linux"),
    ]
    host, name, cached = _Bridge(runnables)._resolve_id_host("console__which", "local")
    assert (host, name) == ("local", "which")
    assert cached["group"] == "console"


def test_search_row_lists_nested_command_paths():
    # A subcommand runnable's search row must list its runnable paths (nested
    # included, space-joined) — and exercises _search_runnables' subcommand walk.
    runnables = [
        _rn(
            "a",
            "db",
            description="db admin",
            requireCommand=True,
            commands={
                "list": {"requireCommand": False, "args": []},
                "restore": {
                    "requireCommand": True,
                    "args": [],
                    "commands": {
                        "full": {"requireCommand": False, "args": []},
                        "incremental": {"requireCommand": False, "args": []},
                    },
                },
            },
        )
    ]
    row = _search(runnables, "db")["results"][0]
    assert row["command"]["required"] is True
    assert set(row["command"]["options"]) == {
        "list",
        "restore full",
        "restore incremental",
    }
    assert "restore" not in row["command"]["options"]  # require-command node


def test_search_row_lists_per_command_args():
    # The row's flat `args` are only the inherited globals; each command's OWN
    # args must be listed under command.args, keyed by the space-joined path, so
    # a call can be built without a describe_runnable round-trip.
    runnables = [
        _rn(
            "a",
            "cron",
            description="crontab management",
            args=[{"name": "run-as", "type": "str"}],
            requireCommand=True,
            commands={
                # Discovery merges inherited globals into every node, so the
                # fixture carries `run-as` on the command too — it must not be
                # repeated per verb.
                "list": {"args": [{"name": "run-as", "type": "str"}]},
                "add": {
                    "args": [
                        {"name": "run-as", "type": "str"},
                        {"name": "schedule", "type": "str", "required": True},
                        {"name": "command", "type": "str", "required": True},
                        {"name": "secret", "type": "password"},
                    ]
                },
            },
        )
    ]
    row = _search(runnables, "cron")["results"][0]
    assert [a["name"] for a in row["args"]] == ["run-as"]
    assert row["command"]["args_are_per_command"] is True
    per = row["command"]["args"]
    assert {a["name"] for a in per["add"]} == {"schedule", "command"}  # no globals
    assert all(a["name"] != "secret" for a in per["add"])  # secrets never inline
    assert [a["required"] for a in per["add"]] == [True, True]
    assert "list" not in per  # no args of its own beyond the globals
    assert "note" not in row["command"]


def test_search_row_defers_per_command_args_when_over_budget():
    # A many-verb runnable must not dominate the result payload — the row drops
    # the detail entirely (never half of it) and names describe_runnable instead.
    commands = {
        f"verb{i}": {"args": [{"name": f"a{i}", "type": "str"}]} for i in range(80)
    }
    runnables = [_rn("a", "jira", description="jira", commands=commands)]
    row = _search(runnables, "jira")["results"][0]
    assert "args" not in row["command"]
    assert "describe_runnable" in row["command"]["note"]
    assert row["command"]["args_are_per_command"] is True


# ── credentials in search rows ────────────────────────────────────────────────


def test_search_row_lists_in_scope_credentials_without_secrets():
    # Dedup mode advertises credentials as a `credential_id` enum on the tool
    # schema; search mode has no per-runnable schema, so the row must carry them
    # or the agent can't know one exists. Id + label only — never a secret.
    creds = [
        {
            "id": "c1",
            "label": "Jira",
            "username": "svc",
            "secret_arg": "pw",
            "scope_runnables": ["ticket"],
        },
        {"id": "c2", "label": "Other", "scope_runnables": ["nothing-else"]},
    ]
    b = _Bridge([_rn("a", "ticket", description="raise a ticket")], credentials=creds)
    row = b._search_runnables("ticket")["results"][0]
    assert row["credentials"] == [{"id": "c1", "label": "Jira"}]  # out of scope gone
    assert "credential_id" in row["credentials_hint"]
    blob = json.dumps(row)
    assert "svc" not in blob and "secret" not in blob.lower()


def test_search_row_omits_credentials_key_when_none_in_scope():
    b = _Bridge([_rn("a", "ticket")], credentials=[])
    row = b._search_runnables("ticket")["results"][0]
    assert "credentials" not in row and "credentials_hint" not in row


def test_run_runnable_schema_says_credential_id_is_top_level():
    tools = {t["name"]: t for t in _Bridge([])._search_mode_tools()}
    run = tools["run_runnable"]
    assert "never put" in run["description"].lower()
    desc = run["input_schema"]["properties"]["credential_id"]["description"]
    assert "NOT an entry in 'args'" in desc


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


# ── runbooks as a second result kind ──────────────────────────────────────────
#
# A runbook is the operator's standing procedure for a recurring task. One search
# covering both kinds is deliberate: an agent only searches for what it thinks to
# search for, so a separate search_runbooks tool would need it to decide to look.


def _save_runbook(**kw):
    from runspec_console import memory

    kw.setdefault("steps", "1. do the thing")
    return memory.save_runbook(kw.pop("id", ""), **kw)


def test_search_returns_matching_runbooks_under_their_own_key():
    _save_runbook(
        id="web-502",
        title="Triage 502s",
        symptom="web nodes returning 502s after a deploy",
        tags=["web", "incident"],
        args={"host": {"type": "choice", "options": ["web-01", "web-02"]}},
    )
    out = _search(
        [_rn("a", "http-check", description="probe a web endpoint")], "502s web"
    )

    # Both kinds come back from the one call, in their own keys.
    assert [r["name"] for r in out["results"]] == ["http-check"]
    (rb,) = out["runbooks"]
    assert rb["id"] == "web-502"
    assert rb["kind"] == "runbook"
    assert rb["title"] == "Triage 502s"
    assert rb["tags"] == ["web", "incident"]
    # Args come back typed, so the model can fill run_runbook's `values` without
    # a load_runbook hop — same shape as a runnable row's args.
    assert rb["args"] == [
        {
            "name": "host",
            "required": True,
            "type": "string",
            "enum": ["web-01", "web-02"],
        }
    ]
    # And it is told which tool takes them.
    assert "run_runbook" in out["runbooks_hint"]


def test_runbooks_never_appear_in_results():
    """`results` feeds run_runnable directly (its description says so, and its `id`
    has no enum), so a runbook id there would be passed to the wrong tool."""
    _save_runbook(id="ping", title="Ping everything", symptom="ping")
    out = _search([_rn("a", "ping")], "ping")
    assert all(r["name"] == "ping" and "kind" not in r for r in out["results"])
    assert [r["id"] for r in out["runbooks"]] == ["ping"]


def test_runbook_symptom_outranks_a_title_only_match():
    """The query usually *is* a restatement of the symptom, so symptom (5) must beat
    a tag (3) — unlike a runnable, whose description is prose nobody searches for."""
    _save_runbook(id="a-first", title="Alphabetically first", tags=["latency"])
    _save_runbook(id="z-last", title="Something else", symptom="latency spike")
    out = _search([], "latency")
    assert [r["id"] for r in out["runbooks"]] == ["z-last", "a-first"]


def test_runbook_search_caps_and_flags_truncation():
    for i in range(9):
        _save_runbook(id=f"rb-{i}", title=f"Runbook {i}", symptom="disk pressure")
    out = _search([], "disk pressure")
    assert len(out["runbooks"]) == 5
    assert out["runbooks_truncated"] is True


def test_runbook_empty_query_browses_but_stays_capped():
    for i in range(7):
        _save_runbook(id=f"rb-{i}", title=f"Runbook {i}")
    out = _search([_rn("a", "ping")], "")
    assert len(out["runbooks"]) == 5


def test_host_or_group_filter_omits_runbooks():
    """A runbook belongs to no host and no package, so it cannot honour either
    filter — returning them anyway would make the filter a lie."""
    _save_runbook(id="web-502", title="Triage 502s", symptom="502s")
    assert "runbooks" not in _search([_rn("a", "ping")], "502s", host="a")
    assert "runbooks" not in _search([_rn("a", "ping")], "502s", group="fleet")
    assert "runbooks" in _search([_rn("a", "ping")], "502s")


def test_runbook_password_args_never_travel_inline():
    _save_runbook(
        id="rotate",
        title="Rotate creds",
        symptom="credential rotation",
        args={"user": {"type": "str"}, "secret": {"type": "password"}},
    )
    (rb,) = _search([], "rotation")["runbooks"]
    assert [a["name"] for a in rb["args"]] == ["user"]


def test_a_broken_runbook_never_sinks_runnable_search(monkeypatch):
    """`validate_runbook_arg_table` only guards the agent's *save* path — a
    hand-edited or Config-Sync'd runbook can carry an unparseable args table, and
    it must not take down fleet-wide runnable discovery."""
    from runspec_console import memory

    memory.save_runbooks(
        [
            {
                "id": "broken",
                "title": "Broken",
                "symptom": "disk",
                "steps": "x",
                "args": "not-a-table",
            }
        ]
    )
    out = _search([_rn("a", "df-check", description="disk usage")], "disk")
    assert [r["name"] for r in out["results"]] == ["df-check"]
    assert out["runbooks"][0]["args"] == []

    def _boom():
        raise OSError("store unreadable")

    monkeypatch.setattr("runspec_console.memory.load_runbooks", _boom)
    out = _search([_rn("a", "df-check", description="disk usage")], "disk")
    assert [r["name"] for r in out["results"]] == ["df-check"]
    assert "runbooks" not in out


def test_a_runbook_id_sent_to_a_runnable_tool_names_the_right_tool():
    """search_runnables returns both kinds, so a runbook id reaching a runnable
    tool is the likeliest mistake. Both entry points carry their own unknown-id
    error, so both need the hint."""
    _save_runbook(id="web-502", title="Triage 502s", symptom="502s")
    b = _Bridge([_rn("a", "ping")])
    for rid in ("web-502", "runbook:web-502"):
        assert "run_runbook" in b._resolve_id_host(rid, "")
        assert "run_runbook" in b._run_describe_tool({"id": rid})
    # A genuinely unknown id still gets the plain error.
    assert "unknown runnable id" in b._resolve_id_host("nope", "")
    assert "unknown runnable id" in b._run_describe_tool({"id": "nope"})


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


def test_resolve_id_host_falls_back_to_configured_cold_host():
    """A runnable known on a warm host resolves for a configured-but-cold host
    too (automation reaches any configured host; execution lazy-connects)."""
    b = _Bridge([_rn("warm", "ping")])
    b._extra_hosts = {"cold"}  # configured, but no runnable in the warm cache
    resolved = b._resolve_id_host("fleet__ping", "cold")
    assert not isinstance(resolved, str), resolved
    host, name, cached_r = resolved
    assert (host, name) == ("cold", "ping")
    assert cached_r["host"] == "cold"  # synthesized for the cold host
    # A host that isn't configured at all still errors.
    assert isinstance(b._resolve_id_host("fleet__ping", "nowhere"), str)


def test_resolve_id_host_discovered_host_without_runnable_errors():
    """A host that HAS been discovered but doesn't carry the runnable (an older
    install) must error clearly, not borrow a warm host's definition and run a
    binary that isn't there (exit 127)."""
    b = _Bridge([_rn("new", "list-autoremovable"), _rn("old", "system-info")])
    out = b._resolve_id_host("fleet__list-autoremovable", "old")
    assert isinstance(out, str)
    assert "not installed on 'old'" in out and "Available on: new" in out


def test_search_row_marks_args_only_some_hosts_have():
    """Hosts can run different versions of one package: an arg only a newer copy
    declares is marked with the hosts that take it, and a rep lacking it still
    lists it (from another host's copy)."""
    base = {"name": "filter", "type": "str"}
    reason = {"name": "reason", "type": "flag"}
    b = _Bridge(
        [
            _rn("old", "list-packages", args=[dict(base)]),
            _rn("new", "list-packages", args=[dict(base), dict(reason)]),
        ]
    )
    row = b._search_runnables("list-packages")["results"][0]
    args = {a["name"]: a for a in row["args"]}
    assert "only_on_hosts" not in args["filter"]
    assert args["reason"]["only_on_hosts"] == ["new"]


# ── args coercion (models sometimes emit `args` as a string) ──────────────────


def test_coerce_run_args_handles_string_and_junk():
    """A model that emits run_runnable `args` as a JSON string, a bare string, or
    None must not crash the gate — `dict(<string>)` raised the cryptic
    "dictionary update sequence element #0 has length 1; 2 is required"."""
    coerce = _Bridge([])._coerce_run_args
    assert coerce({"host": "web1"}) == {"host": "web1"}  # dict passes through
    assert coerce('{"host": "web1"}') == {"host": "web1"}  # JSON-string object parsed
    assert coerce("not json") == {}  # bare string → empty (preflight reports it)
    assert coerce("[1, 2]") == {}  # JSON but not an object → empty
    assert coerce(None) == {}
    assert coerce("") == {}


def test_effective_autonomy_survives_string_args():
    """The autonomy decision path coerces `args` too, so a stringified args field
    resolves the runnable's base level instead of raising."""
    b = _Bridge([_rn("a", "ping", autonomy="confirm")])
    # Would previously raise ValueError from dict("host=a") deep in the gate.
    assert (
        b._effective_tool_autonomy(
            "run_runnable", {"id": "fleet__ping", "args": "host=a"}
        )
        == "confirm"
    )


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

    monkeypatch.setattr("runspec_console.executor.run_remote", fake_run_remote)
    monkeypatch.setattr(
        "runspec_console.bridge_utils._paths", lambda e: ["/bin/runspec"]
    )
    monkeypatch.setattr("runspec_console.hosts.venv_name", lambda p: "fleet")

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


def test_run_runnable_deliver_is_top_level_not_an_arg(monkeypatch):
    """`deliver` is a top-level output-shaping directive — it must never reach the
    runnable as an argument, whether passed top-level or misplaced inside `args`."""
    for placement in ("top", "in_args"):
        captured = {}

        def fake_run_remote(
            ssh, rp, runnable, args, command_path, on_line, on_done, idf, **kw
        ):
            captured.update(args=args)
            on_done(0, 1)

        monkeypatch.setattr("runspec_console.executor.run_remote", fake_run_remote)
        monkeypatch.setattr(
            "runspec_console.bridge_utils._paths", lambda e: ["/bin/runspec"]
        )
        monkeypatch.setattr("runspec_console.hosts.venv_name", lambda p: "fleet")

        runnables = [_rn("h2", "ping", args=[{"name": "count", "type": "int"}])]
        ti = {"id": "fleet__ping", "host": "h2", "args": {"count": 3}}
        if placement == "top":
            ti["deliver"] = "inline"
        else:
            ti["args"]["deliver"] = "digest"  # misplaced — must be moved out
        out = _Bridge(runnables)._run_tool_sync("run_runnable", ti)
        # deliver is popped before preflight/dispatch: the call runs and the runnable
        # only ever sees its own args.
        assert not out.startswith("Error"), (placement, out)
        assert captured["args"] == {"count": 3}, placement


def test_run_runnable_preflight_blocks_before_dispatch(monkeypatch):
    called = {"ran": False}
    monkeypatch.setattr(
        "runspec_console.executor.run_local", lambda *a, **k: called.update(ran=True)
    )
    monkeypatch.setattr(
        "runspec_console.bridge_utils._paths", lambda e: ["/bin/runspec"]
    )
    monkeypatch.setattr("runspec_console.hosts.venv_name", lambda p: "fleet")

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


def test_run_runnable_rejects_unknown_credential_id(monkeypatch):
    # Search mode's credential_id is a free string (no enum), so a typo'd or
    # stale id used to be logged and dropped — the run went ahead unauthenticated
    # and failed confusingly inside the runnable. It must bounce in-turn instead.
    called = {"ran": False}
    monkeypatch.setattr(
        "runspec_console.executor.run_local", lambda *a, **k: called.update(ran=True)
    )
    monkeypatch.setattr(
        "runspec_console.bridge_utils._paths", lambda e: ["/bin/runspec"]
    )
    monkeypatch.setattr("runspec_console.hosts.venv_name", lambda p: "fleet")

    b = _Bridge([_rn("h1local", "ticket")], credentials=[{"id": "c1", "label": "Jira"}])
    out = b._run_tool_sync(
        "run_runnable",
        {"id": "fleet__ticket", "host": "h1local", "credential_id": "typo"},
    )
    assert out.startswith("Error:") and "unknown credential_id 'typo'" in out
    assert "c1" in out  # names what IS available
    assert called["ran"] is False  # never dispatched


def test_run_runnable_accepts_known_credential_id(monkeypatch):
    monkeypatch.setattr(
        "runspec_console.executor.run_local", lambda *a, **k: a[5](0, 1)
    )
    monkeypatch.setattr(
        "runspec_console.bridge_utils._paths", lambda e: ["/bin/runspec"]
    )
    monkeypatch.setattr("runspec_console.hosts.venv_name", lambda p: "fleet")

    b = _Bridge([_rn("h1local", "ticket")], credentials=[{"id": "c1", "label": "Jira"}])
    out = b._run_tool_sync(
        "run_runnable",
        {"id": "fleet__ticket", "host": "h1local", "credential_id": "c1"},
    )
    assert not out.startswith("Error")


def test_run_runnable_splits_password_into_secrets(monkeypatch):
    captured = {}

    def fake_run_local(rp, runnable, args, command_path, on_line, on_done, **kw):
        captured.update(args=args, secrets=kw.get("secrets"))
        on_done(0, 1)

    monkeypatch.setattr("runspec_console.executor.run_local", fake_run_local)
    monkeypatch.setattr(
        "runspec_console.bridge_utils._paths", lambda e: ["/bin/runspec"]
    )
    monkeypatch.setattr("runspec_console.hosts.venv_name", lambda p: "fleet")

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


# ── host groups in the search filters ──────────────────────────────────────


def _grouped_search(query, host_groups, literal_hosts=(), **kw):
    runnables = [
        _rn("web-1", "disk-usage", group="linux-utils"),
        _rn("db-1", "pg-dump", group="linux-utils"),
        _rn("laptop", "flush-dns", group="windows"),
    ]
    b = _Bridge(runnables)
    b._groups_map = lambda: host_groups
    b._host_entry = lambda name: {"name": name} if name in literal_hosts else None
    return [r["id"] for r in b._search_runnables(query, **kw)["results"]]


def test_host_filter_accepts_a_host_group_name():
    ids = _grouped_search(
        "", {"Linux Servers": ["web-1", "db-1"]}, host="Linux Servers"
    )
    assert sorted(ids) == ["disk-usage", "pg-dump"]


def test_group_filter_naming_a_host_group_is_read_as_one():
    # The agent's natural mix-up: `group` is the package filter, but it passed
    # the host group's name and used to get zero results.
    ids = _grouped_search(
        "", {"Linux Servers": ["web-1", "db-1"]}, group="Linux Servers"
    )
    assert sorted(ids) == ["disk-usage", "pg-dump"]


def test_package_group_filter_still_wins_over_a_same_named_host_group():
    ids = _grouped_search("", {"windows": ["web-1"]}, group="windows")
    assert ids == ["flush-dns"]


def test_a_literal_host_wins_over_a_same_named_host_group():
    ids = _grouped_search(
        "", {"web-1": ["db-1"]}, literal_hosts=("web-1",), host="web-1"
    )
    assert ids == ["disk-usage"]


def test_unreadable_host_groups_never_sink_search():
    b = _Bridge([_rn("web-1", "disk-usage")])

    def boom():
        raise RuntimeError("registry unreadable")

    b._groups_map = boom
    assert [r["id"] for r in b._search_runnables("disk")["results"]] == ["disk-usage"]


def test_wiki_facts_ride_search_rows_and_describe():
    b = _Bridge([_rn("a", "confluence"), _rn("a", "ping")])
    calls = []

    def notes(names, per_entity=3):
        calls.append((list(names), per_entity))
        return {"confluence": ["Pass --keep-macros to keep macro formatting."]}

    b._wiki_notes = notes
    out = b._search_runnables("")
    rows = {r["name"]: r for r in out["results"]}
    assert rows["confluence"]["wiki"] == [
        "Pass --keep-macros to keep macro formatting."
    ]
    assert "wiki" not in rows["ping"]
    assert "wiki_hint" in out
    desc = json.loads(b._run_describe_tool({"id": "confluence"}))
    assert desc["wiki"] == ["Pass --keep-macros to keep macro formatting."]
    assert calls[-1] == (["confluence"], 8)  # describe gets a bigger budget


def test_no_wiki_key_when_nothing_is_known():
    b = _Bridge([_rn("a", "ping")])
    b._wiki_notes = lambda names, per_entity=3: {}
    out = b._search_runnables("ping")
    assert "wiki_hint" not in out and "wiki" not in out["results"][0]
