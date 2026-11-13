"""
test_console_context.py — the chat agent's live awareness block.

Bridge._console_context() builds a per-turn system-prompt section describing the
working-hours rota and the configured triggers/schedules, so the chat agent can
reason about them (and react when asked to act outside working hours). Bound onto
a bare stub so we dodge Bridge.__init__.
"""

from __future__ import annotations

import threading
import types

import runspec_console.config as cfg
from runspec_console.bridge import Bridge


def _stub(monkeypatch, *, rota, triggers, schedules, hosts=None, connected=None):
    monkeypatch.setattr(cfg, "read_rota", lambda: rota)
    monkeypatch.setattr(cfg, "read_triggers", lambda: triggers)
    # On Windows (pywin32 present) Outlook COM is available, so _console_context
    # folds in the real monitored-mailbox line and the context is never empty —
    # the tests here control rota/triggers/schedules, not ambient Outlook state.
    # Force it off so they're deterministic cross-platform (no test asserts the
    # mailbox line is present).
    from runspec_console.sources import outlook as _outlook

    monkeypatch.setattr(_outlook, "outlook_available", lambda: False)
    ns = types.SimpleNamespace()
    ns._read_schedules = lambda: schedules
    ns._hosts = hosts or []  # for _run_status_tool's denylist pointer
    ns._break_until = None  # for the "on break" awareness line
    # Host-group awareness block: derive the map from the supplied host entries.
    from runspec_console.hosts import groups_map as _gm

    ns._groups_map = lambda: _gm(hosts or [])
    ns._connected_cache = dict(connected or {})
    ns._lock = threading.Lock()
    ns._workspace_guidance = types.MethodType(Bridge._workspace_guidance, ns)
    ns._console_context = types.MethodType(Bridge._console_context, ns)
    return ns


_ALWAYS_ON = {
    d: {"on": True, "start": "00:00", "end": "23:59"}
    for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
}
_ALWAYS_OFF = {
    d: {"on": False, "start": "09:00", "end": "17:00"}
    for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
}


def test_empty_when_nothing_configured(monkeypatch):
    """Rota off + no triggers/schedules → no block, so the cached prefix stands."""
    ns = _stub(monkeypatch, rota={}, triggers=[], schedules=[])
    assert ns._console_context() == ""


def test_host_groups_block_listed_with_connected_counts(monkeypatch):
    ns = _stub(
        monkeypatch,
        rota={},
        triggers=[],
        schedules=[],
        hosts=[
            {"name": "web01", "group": "Production"},
            {"name": "web02", "group": "Production"},
            {"name": "db01", "group": "Production"},
        ],
        connected={"web01": True, "web02": True},  # db01 down
    )
    out = ns._console_context()
    assert "Host groups (1)" in out
    assert "Production: 2/3 connected — web01, web02, db01" in out
    # Even with rota off + no triggers, the groups block alone keeps the context.
    assert out != ""


def test_no_host_groups_keeps_cached_prefix(monkeypatch):
    # Ungrouped hosts alone are not a group → no block, empty context.
    ns = _stub(
        monkeypatch, rota={}, triggers=[], schedules=[], hosts=[{"name": "solo"}]
    )
    assert ns._console_context() == ""


def test_rota_off_still_noted_when_automation_exists(monkeypatch):
    ns = _stub(
        monkeypatch,
        rota={"enabled": False},
        triggers=[
            {"name": "vip", "source": "outlook", "event": "new_mail", "mode": "agent"}
        ],
        schedules=[],
    )
    out = ns._console_context()
    assert "rota: OFF" in out
    assert "vip: outlook:new_mail · agent" in out


def test_inside_working_hours(monkeypatch):
    ns = _stub(
        monkeypatch,
        rota={"enabled": True, "source": "console", **_ALWAYS_ON},
        triggers=[],
        schedules=[],
    )
    out = ns._console_context()
    assert "**INSIDE** working hours" in out
    assert "console grid" in out


def test_outside_working_hours_warns(monkeypatch):
    ns = _stub(
        monkeypatch,
        rota={"enabled": True, "source": "console", **_ALWAYS_OFF},
        triggers=[],
        schedules=[],
    )
    out = ns._console_context()
    assert "**OUTSIDE** working hours" in out
    assert "confirm they want to proceed" in out


def test_outlook_source_noted_as_approximate(monkeypatch):
    ns = _stub(
        monkeypatch,
        rota={"enabled": True, "source": "outlook", **_ALWAYS_ON},
        triggers=[],
        schedules=[],
    )
    out = ns._console_context()
    assert "Outlook calendar" in out
    assert "approximate" in out


def test_status_tool_returns_context(monkeypatch):
    ns = _stub(
        monkeypatch,
        rota={"enabled": True, "source": "console", **_ALWAYS_OFF},
        triggers=[],
        schedules=[],
    )
    ns._run_status_tool = types.MethodType(Bridge._run_status_tool, ns)
    out = ns._run_status_tool({})
    assert "**OUTSIDE** working hours" in out


def test_status_tool_fallback_when_unconfigured(monkeypatch):
    ns = _stub(monkeypatch, rota={}, triggers=[], schedules=[])
    ns._run_status_tool = types.MethodType(Bridge._run_status_tool, ns)
    out = ns._run_status_tool({})
    assert "rota: OFF" in out  # non-empty even though _console_context() is ""
    assert "manage_denylist" not in out  # no hosts ⇒ no denylist pointer


def test_status_tool_leads_with_current_local_time(monkeypatch):
    # The on-demand status tool is where the agent gets the exact wall-clock time
    # (the per-turn prompt carries only the date, for prompt-cache stability).
    from datetime import datetime

    ns = _stub(monkeypatch, rota={}, triggers=[], schedules=[])
    ns._run_status_tool = types.MethodType(Bridge._run_status_tool, ns)
    out = ns._run_status_tool({})
    assert "Current local date & time:" in out
    now = datetime.now()
    assert now.strftime("%Y-%m-%d") in out  # today's date
    assert now.strftime("%H:%M")[:2] in out  # the hour (wall-clock time present)


def test_status_tool_points_at_denylist_when_hosts_present(monkeypatch):
    # A host with a runspec path ⇒ the kill switch applies, so console_status
    # mentions manage_denylist (zero-cost text pointer, no fleet sweep).
    ns = _stub(
        monkeypatch,
        rota={},
        triggers=[],
        schedules=[],
        hosts=[
            {"name": "prod-1", "ssh": "x@prod-1", "runspec_paths": ["/v/bin/runspec"]}
        ],
    )
    ns._run_status_tool = types.MethodType(Bridge._run_status_tool, ns)
    out = ns._run_status_tool({})
    assert "manage_denylist" in out and "action='list'" in out


def test_status_tool_is_autonomous():
    from runspec_console.bridge_const import _STATUS_TOOL_NAMES

    ns = types.SimpleNamespace()
    ns._effective_tool_autonomy = types.MethodType(Bridge._effective_tool_autonomy, ns)
    assert "console_status" in _STATUS_TOOL_NAMES
    assert ns._effective_tool_autonomy("console_status", {}) == "autonomous"


def test_status_tool_in_schema():
    ns = types.SimpleNamespace()
    ns._status_tools = types.MethodType(Bridge._status_tools, ns)
    names = [t["name"] for t in ns._status_tools()]
    assert names == ["console_status"]


def test_lists_schedules(monkeypatch):
    ns = _stub(
        monkeypatch,
        rota={"enabled": False},
        triggers=[],
        schedules=[
            {
                "id": "nightly",
                "action": "runnable",
                "host": "db01",
                "runnable": "backup",
                "when": {"cron": "0 2 * * *"},
            },
            {
                "id": "tick",
                "action": "event",
                "event": "ping",
                "when": {"every": "5m"},
                "enabled": False,
            },
        ],
    )
    out = ns._console_context()
    assert "Configured schedules (2):" in out
    assert "nightly: run /backup on db01 · 0 2 * * *" in out
    assert "tick: emit ping · every 5m [disabled]" in out


def test_lists_prompt_and_runbook_schedules(monkeypatch):
    ns = _stub(
        monkeypatch,
        rota={"enabled": False},
        triggers=[],
        schedules=[
            {
                "id": "morning-check",
                "action": "prompt",
                "prompt": "Check disk usage and flag anything over 80%.",
                "when": {"cron": "0 8 * * *"},
            },
            {
                "id": "weekly-report",
                "action": "runbook",
                "runbook": "build-report",
                "when": {"every": "1d"},
            },
        ],
    )
    out = ns._console_context()
    assert "morning-check: agent prompt: Check disk usage" in out
    assert "weekly-report: agent runbook build-report · every 1d" in out
    # The agent is told it can schedule prompts and runbooks.
    assert "create_schedule" in out and "action='prompt'" in out


# ── saved-runs awareness block ────────────────────────────────────────────────


def _seed_ctx_saved_runs(tmp_path, monkeypatch, rows):
    from runspec_console import savedruns

    monkeypatch.setattr(
        cfg, "saved_runs_path", lambda: tmp_path / "runspec_saved_runs.toml"
    )
    savedruns.save_saved_runs(rows)


def test_saved_runs_block_listed(tmp_path, monkeypatch):
    _seed_ctx_saved_runs(
        tmp_path,
        monkeypatch,
        [
            {
                "id": "flush",
                "title": "Flush DNS",
                "runnable": "flush-dns",
                "group": "Prod",
                "params": {"reason": {"required": True}},
            }
        ],
    )
    ns = _stub(monkeypatch, rota={}, triggers=[], schedules=[])
    out = ns._console_context()
    assert "Saved runs (1)" in out
    assert "flush — Flush DNS: /flush-dns on group Prod (params: reason)" in out
    assert "run_saved_run" in out  # pointer to the tool


def test_saved_runs_alone_produce_a_block(tmp_path, monkeypatch):
    """A console whose only automation state is a saved run still gets context."""
    _seed_ctx_saved_runs(
        tmp_path,
        monkeypatch,
        [{"id": "a", "runnable": "r", "host": "h"}],
    )
    ns = _stub(monkeypatch, rota={}, triggers=[], schedules=[])
    assert ns._console_context() != ""


# ── _presence_caveat: the lean standing chat block ────────────────────────────
#
# Unlike _console_context() (the full snapshot the console_status tool serves),
# _presence_caveat() is what rides every interactive chat turn. It stays empty
# on the common turn and only speaks up when the operator is outside their
# working hours — so tokens and prompt-cache churn drop to zero on the fast path.


def _caveat_stub(monkeypatch, *, rota):
    monkeypatch.setattr(cfg, "read_rota", lambda: rota)
    ns = types.SimpleNamespace()
    ns._presence_caveat = types.MethodType(Bridge._presence_caveat, ns)
    return ns


def test_caveat_empty_when_rota_off(monkeypatch):
    ns = _caveat_stub(monkeypatch, rota={"enabled": False})
    assert ns._presence_caveat() == ""


def test_caveat_empty_inside_working_hours(monkeypatch):
    ns = _caveat_stub(
        monkeypatch, rota={"enabled": True, "source": "console", **_ALWAYS_ON}
    )
    assert ns._presence_caveat() == ""


def test_caveat_warns_outside_working_hours(monkeypatch):
    ns = _caveat_stub(
        monkeypatch, rota={"enabled": True, "source": "console", **_ALWAYS_OFF}
    )
    out = ns._presence_caveat()
    assert "**OUTSIDE**" in out
    assert "confirm they want to proceed" in out
    # No inventory / lock / break lines — that detail is on console_status now.
    assert "Configured triggers" not in out
    assert "Lock auto-suspend" not in out


def test_caveat_notes_outlook_source_as_approximate(monkeypatch):
    ns = _caveat_stub(
        monkeypatch, rota={"enabled": True, "source": "outlook", **_ALWAYS_OFF}
    )
    out = ns._presence_caveat()
    assert "**OUTSIDE**" in out
    assert "approximate" in out


def test_mcp_servers_line_points_at_use_tool_when_deferred(monkeypatch):
    """In search mode external MCP tools are deferred, so the context must not
    claim they sit in the tool list — it names describe_tool/use_tool too."""
    ns = _stub(monkeypatch, rota={}, triggers=[], schedules=[])
    ns.mcp_status = lambda: {
        "servers": [{"name": "atlassian", "connected": True, "tool_count": 40}]
    }
    out = ns._console_context()
    assert "External MCP servers connected: atlassian (40 tools)" in out
    assert "use_tool" in out and "describe_tool" in out
    assert "appear in your toolset" not in out


# ── the console MCP server's view (offered = the tools it publishes) ──────────


def _mcp_stub(monkeypatch, tmp_path, *, ai=True):
    _seed_ctx_saved_runs(
        tmp_path, monkeypatch, [{"id": "flush", "runnable": "flush-dns", "host": "h"}]
    )
    ns = _stub(
        monkeypatch,
        rota={"enabled": True, "source": "console", **_ALWAYS_ON},
        triggers=[
            {"name": "vip", "source": "outlook", "event": "new_mail", "mode": "agent"},
            {"name": "disk", "source": "webhook", "event": "alert", "mode": "rule"},
        ],
        schedules=[
            {
                "id": "morning",
                "action": "prompt",
                "prompt": "Check disk.",
                "when": {"cron": "0 8 * * *"},
            },
            {
                "id": "nightly",
                "action": "runnable",
                "host": "db01",
                "runnable": "backup",
                "when": {"cron": "0 2 * * *"},
            },
        ],
        hosts=[
            {"name": "web01", "group": "Production"},
            {"name": "web02", "group": "Production"},
        ],
    )
    ns._ai_enabled = ai
    return ns


def test_mcp_view_names_no_tool_the_server_lacks(monkeypatch, tmp_path):
    ns = _mcp_stub(monkeypatch, tmp_path)
    out = ns._console_context(offered=frozenset())
    # State is still there …
    assert "vip: outlook:new_mail · agent" in out
    assert "nightly: run /backup on db01" in out
    assert "Production: 0/2 connected" in out
    assert "**INSIDE** working hours" in out
    # … but no tool it can't call and no group fan-out.
    for absent in (
        "create_schedule",
        "run_saved_run",
        "Saved runs",
        "run_runnable",
        "fan out",
        "workspace_",
        "run_python_sandboxed",
    ):
        assert absent not in out, absent
    assert "one host per call" in out
    assert "Triggers fire inside the console" in out
    assert "Schedules fire inside the console" in out


def test_mcp_view_with_builtins_names_only_those(monkeypatch, tmp_path):
    ns = _mcp_stub(monkeypatch, tmp_path)
    out = ns._console_context(offered=frozenset({"create_schedule", "run_saved_run"}))
    assert "create_schedule" in out and "action='prompt'" in out
    assert "run on the console's own model" in out
    assert "Saved runs (1)" in out and "run_saved_run" in out
    assert "manage_saved_runs" not in out


def test_mcp_view_with_ai_off(monkeypatch, tmp_path):
    ns = _mcp_stub(monkeypatch, tmp_path, ai=False)
    out = ns._console_context(offered=frozenset({"create_schedule"}))
    assert "vip: outlook:new_mail · agent [not running: needs the console's AI]" in out
    assert "disk: webhook:alert · rule\n" in out + "\n"  # a rule trigger still runs
    assert "morning: agent prompt: Check disk. · 0 8 * * * [not running" in out
    assert "nightly: run /backup on db01 · 0 2 * * *\n" in out + "\n"
    # The rota and lock gates are inert with AI off, so they aren't described.
    assert "working hours" not in out and "Lock auto-suspend" not in out
    # create_schedule is offered, but not its AI actions.
    assert "action='prompt'" not in out and "not 'prompt' or 'runbook'" in out


def test_chat_view_unchanged_by_offered_default(monkeypatch, tmp_path):
    ns = _mcp_stub(monkeypatch, tmp_path)
    out = ns._console_context()
    assert "fan out to EVERY host" in out
    assert "create_schedule" in out and "manage_saved_runs" in out
    assert "fire inside the console" not in out


def test_status_tool_over_mcp_skips_denylist_pointer(monkeypatch):
    ns = _stub(
        monkeypatch,
        rota={},
        triggers=[],
        schedules=[],
        hosts=[
            {"name": "prod-1", "ssh": "x@prod-1", "runspec_paths": ["/v/bin/runspec"]}
        ],
    )
    ns._run_status_tool = types.MethodType(Bridge._run_status_tool, ns)
    assert "manage_denylist" not in ns._run_status_tool({}, offered=frozenset())
    assert "manage_denylist" in ns._run_status_tool(
        {}, offered=frozenset({"manage_denylist"})
    )


def test_hosts_outside_the_active_profile_are_named(monkeypatch):
    """With a profile active the tool list covers only its hosts; the agent is
    told the others exist and can be named as `host` (they connect on demand),
    so a runnable missing from a search isn't read as missing from them."""
    hosts = [
        {"name": "web", "profiles": ["lab"]},
        {"name": "little-boy", "profiles": ["Homelab"]},
        {"name": "little-girl", "profiles": ["Homelab"]},
    ]
    ns = _stub(monkeypatch, rota={}, triggers=[], schedules=[], hosts=hosts)
    ns._active_profile_name = lambda: "lab"
    ns._reload_hosts = lambda: None
    ns._out_of_profile_hosts = types.MethodType(Bridge._out_of_profile_hosts, ns)
    ctx = ns._console_context()
    assert "Host profile 'lab' is active" in ctx
    assert "Other configured hosts (2): little-boy, little-girl." in ctx
    # No profile active (All hosts) ⇒ nothing to say.
    ns._active_profile_name = lambda: ""
    assert "Other configured hosts" not in ns._console_context()
