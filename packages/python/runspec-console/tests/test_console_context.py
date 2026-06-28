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
    ns = types.SimpleNamespace()
    ns._read_schedules = lambda: schedules
    ns._hosts = hosts or []  # for _run_status_tool's denylist pointer
    ns._break_until = None  # for the "on break" awareness line
    # Host-group awareness block: derive the map from the supplied host entries.
    from runspec_console.hosts import groups_map as _gm

    ns._groups_map = lambda: _gm(hosts or [])
    ns._connected_cache = dict(connected or {})
    ns._lock = threading.Lock()
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
