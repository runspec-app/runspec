"""
test_ai_off.py — the `[console] ai = false` off-switch (backend gate).

One flag, read once at bring-up into ``Bridge._ai_enabled``, refuses every path
that would run a model turn — and *refuses*, never silently degrades:

  - ``config.ai_enabled()`` — absent / non-bool ⇒ True, ``false`` ⇒ False.
  - ``_get_adapter`` never builds an adapter (nor imports plugins).
  - chat / runbook / evals / improve-synthesis / test-trace return AI_OFF_MESSAGE.
  - ``Trigger.from_config`` rejects agent-mode triggers and a rule ``pre_turn``.
  - ``parse_schedule_entry`` rejects prompt / runbook / eval schedules, and the
    bridge's create/update refuse them too (the ``create_schedule`` action tool
    is reachable over the MCP server).
  - the ``create_schedule`` tool's action enum shrinks; room runbooks aren't
    published.

Everything a non-AI console keeps — rule triggers, runnable/saved-run/event
schedules, the MCP server — is asserted to still parse / build.

See docs/design/console-ai-off-switch.md.
"""

from __future__ import annotations

import time

import pytest

from runspec_console import config as config_mod
from runspec_console.bridge import Bridge
from runspec_console.bridge_const import AI_OFF_MESSAGE
from runspec_console.sources.schedule import (
    AI_SCHEDULE_ACTIONS,
    ScheduleSource,
    parse_schedule_entry,
)
from runspec_console.triggers import Trigger, load_triggers

# ── config accessor ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cfg, expected",
    [
        ({}, True),
        ({"console": {}}, True),
        ({"console": {"ai": True}}, True),
        ({"console": {"ai": False}}, False),
        ({"console": {"ai": "no"}}, True),  # non-bool ⇒ default (enabled)
        ({"console": {"ai": 0}}, True),
        ({"console": "garbage"}, True),
    ],
)
def test_ai_enabled_reads_console_flag(monkeypatch, cfg, expected):
    monkeypatch.setattr(config_mod, "read_config", lambda: cfg)
    assert config_mod.ai_enabled() is expected


# ── bridge flag ──────────────────────────────────────────────────────────────


@pytest.fixture
def off_bridge(monkeypatch):
    """A real Bridge brought up with the switch off — the flag is read in
    ``__init__`` (after the seed apply), so patching the accessor first is the
    same thing a seeded ``ai = false`` does on first launch."""
    monkeypatch.setattr(config_mod, "ai_enabled", lambda: False)
    b = Bridge()
    # A provider *is* configured — the switch must win over it.
    monkeypatch.setattr(b, "get_config", lambda: {"llm": {"provider": "anthropic"}})
    return b


def test_bridge_reads_flag_at_bring_up(monkeypatch):
    monkeypatch.setattr(config_mod, "ai_enabled", lambda: False)
    assert Bridge().ai_enabled() is False
    monkeypatch.setattr(config_mod, "ai_enabled", lambda: True)
    assert Bridge().ai_enabled() is True


def test_get_adapter_is_none_and_imports_no_plugins(off_bridge, monkeypatch):
    called = []
    monkeypatch.setattr(
        off_bridge, "_ensure_plugins_imported", lambda cfg: called.append(cfg)
    )
    assert off_bridge._get_adapter() is None
    assert off_bridge._get_adapter(system_override="x") is None
    assert called == []


def _wait_for_end(events, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(name == "runspec:run_end" for name, _ in events):
            return
        time.sleep(0.02)
    raise AssertionError("chat turn never ended")


def test_send_chat_returns_ai_off_message(off_bridge, monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        off_bridge,
        "_dispatch",
        lambda name, payload=None: events.append((name, payload)),
    )
    chat_id = off_bridge.send_chat("hello")
    _wait_for_end(events)
    tokens = [
        p["token"] for n, p in events if n == "runspec:token" and p["id"] == chat_id
    ]
    assert tokens and AI_OFF_MESSAGE in tokens[0]
    assert "No model provider" not in tokens[0]
    end = next(p for n, p in events if n == "runspec:run_end")
    assert end["exit_code"] == 1


def test_run_runbook_refuses(off_bridge):
    assert off_bridge.run_runbook("anything") == {"error": AI_OFF_MESSAGE}


def test_run_evals_refuses(off_bridge):
    res = off_bridge.run_evals(fixture_toml='[[eval]]\nid="x"\nprompt="p"\n')
    assert res == {"ok": False, "reason": AI_OFF_MESSAGE}


def test_synthesize_improve_report_refuses(off_bridge, monkeypatch):
    monkeypatch.setattr(off_bridge, "_load_improve", lambda rid: {"id": rid})
    monkeypatch.setattr(config_mod, "read_improve", lambda: {"llm_synthesis": True})
    report = off_bridge.synthesize_improve_report("r1")
    assert report["synthesis_error"] == AI_OFF_MESSAGE


def test_send_test_trace_refuses(off_bridge):
    assert off_bridge.send_test_trace() == {"ok": False, "reason": AI_OFF_MESSAGE}


def test_room_runbooks_not_published(off_bridge):
    assert off_bridge._build_room_runbooks() == ([], {})


def test_create_schedule_tool_enum_drops_agent_actions(off_bridge, monkeypatch):
    monkeypatch.setattr(off_bridge, "_reload_credentials", lambda: None)
    tool = next(t for t in off_bridge._action_tools() if t["name"] == "create_schedule")
    enum = tool["input_schema"]["properties"]["action"]["enum"]
    assert set(enum).isdisjoint(AI_SCHEDULE_ACTIONS)
    assert {"event", "runnable", "saved_run"} <= set(enum)


def test_create_schedule_tool_enum_full_when_on(monkeypatch):
    monkeypatch.setattr(config_mod, "ai_enabled", lambda: True)
    b = Bridge()
    monkeypatch.setattr(b, "_reload_credentials", lambda: None)
    tool = next(t for t in b._action_tools() if t["name"] == "create_schedule")
    assert (
        set(tool["input_schema"]["properties"]["action"]["enum"]) >= AI_SCHEDULE_ACTIONS
    )


def test_bridge_create_and_update_schedule_refuse_agent_actions(
    off_bridge, monkeypatch, tmp_path
):
    monkeypatch.setattr(config_mod, "hosts_path", lambda: tmp_path / "hosts.toml")
    monkeypatch.setattr(off_bridge, "restart_triggers", lambda: None)
    for action in ("prompt", "runbook", "skill", "eval"):
        with pytest.raises(ValueError, match="AI features are not available"):
            off_bridge.create_schedule(
                {"id": "j", "action": action, "when": {"every": "5m"}}
            )
        with pytest.raises(ValueError, match="AI features are not available"):
            off_bridge.update_schedule("j", {"action": action, "when": {"every": "5m"}})
    assert not (tmp_path / "runspec_schedules.toml").exists()
    # A runnable schedule still writes.
    off_bridge.create_schedule(
        {
            "id": "ok",
            "action": "runnable",
            "host": "h",
            "runnable": "r",
            "when": {"every": "5m"},
        }
    )
    assert (tmp_path / "runspec_schedules.toml").exists()


def test_action_create_schedule_tool_refuses(off_bridge, monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "hosts_path", lambda: tmp_path / "hosts.toml")
    monkeypatch.setattr(off_bridge, "restart_triggers", lambda: None)
    out = off_bridge._action_create_schedule(
        {
            "id": "p",
            "action": "prompt",
            "prompt": "check disks",
            "when": {"every": "1h"},
        }
    )
    assert (
        out.startswith("Error: invalid schedule")
        and "AI features are not available" in out
    )


def test_scheduled_agent_and_eval_callbacks_are_inert(off_bridge, monkeypatch):
    class Job:
        id = "j"
        action = "prompt"
        eval_fixture = "f"

    invoked = []
    monkeypatch.setattr(
        off_bridge, "_invoke_scheduled_agent", lambda job: invoked.append(job)
    )
    off_bridge._run_scheduled_agent(Job())
    assert invoked == []
    assert off_bridge._run_scheduled_eval(Job()) == {
        "ok": False,
        "reason": AI_OFF_MESSAGE,
    }


def test_trigger_agent_turn_and_pre_turn_are_inert(off_bridge):
    out = off_bridge._trigger_run_agent_turn("c1", "sys", "msg", draft=False)
    assert AI_OFF_MESSAGE in out
    assert off_bridge._trigger_run_pre_turn("c1", "extract", "event text") == {}


def test_rota_gate_is_inert_when_ai_off(off_bridge, monkeypatch):
    """With AI off the rota gates go no-op — even a rota that *would* suppress
    (enabled + working hours that exclude now, screen locked) can't hold back a
    plain runnable schedule, since there is no agent behaviour to govern and the
    Rota tab is hidden."""
    from runspec_console import config as cfg
    from runspec_console import winlock

    # A rota that an AI-on console would honour: enabled, lock-suspend on.
    monkeypatch.setattr(
        cfg, "read_rota", lambda: {"enabled": True, "lock_suspend": True}
    )
    # Present the most suppressive live state: screen locked, break not relevant.
    monkeypatch.setattr(winlock, "screen_lock_state", lambda: "locked")
    monkeypatch.setattr(winlock, "is_screen_locked", lambda: True)

    assert off_bridge._build_clock_ok()() is True
    assert off_bridge._build_lock_ok()() is True

    # Contrast: an AI-on bridge honours lock-suspend and reports the locked state.
    monkeypatch.setattr(config_mod, "ai_enabled", lambda: True)
    on_bridge = Bridge()
    assert on_bridge._build_lock_ok()() is False


# ── pure validators: triggers ────────────────────────────────────────────────


def _trigger(**over):
    data = {"name": "t", "source": "outlook", "event": "new_mail"}
    data.update(over)
    return data


def test_trigger_agent_mode_refused_when_ai_off():
    # The refusal is actionable (use rule mode) but never names the [console] ai
    # switch or how to re-enable it — a locked-down console doesn't advertise it.
    with pytest.raises(ValueError, match="agent-mode trigger.*not available"):
        Trigger.from_config(_trigger(), ai_enabled=False)  # mode defaults to agent
    with pytest.raises(ValueError, match="agent-mode trigger"):
        Trigger.from_config(_trigger(mode="agent"), ai_enabled=False)


def test_trigger_pre_turn_refused_when_ai_off():
    data = _trigger(mode="rule", run="h__r", pre_turn="extract the ticket id")
    with pytest.raises(ValueError, match="pre_turn.*not available"):
        Trigger.from_config(data, ai_enabled=False)
    # Same trigger is fine with AI on, and a rule trigger without pre_turn loads.
    assert Trigger.from_config(data).pre_turn == "extract the ticket id"
    t = Trigger.from_config(_trigger(mode="rule", run="h__r"), ai_enabled=False)
    assert t.mode == "rule"


def test_load_triggers_reports_agent_triggers_as_errors_when_ai_off():
    triggers, errors = load_triggers(
        [
            _trigger(name="agentic"),
            _trigger(name="rule", mode="rule", run="h__r"),
        ],
        ai_enabled=False,
    )
    assert [t.name for t in triggers] == ["rule"]
    assert len(errors) == 1 and "agentic" in errors[0] and "not available" in errors[0]
    # Default keeps today's behaviour: both load.
    triggers, errors = load_triggers(
        [_trigger(name="agentic"), _trigger(name="rule", mode="rule", run="h__r")]
    )
    assert len(triggers) == 2 and errors == []


# ── pure validators: schedules ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "entry",
    [
        {"id": "p", "action": "prompt", "prompt": "check"},
        {"id": "r", "action": "runbook", "runbook": "rb"},
        {"id": "s", "action": "skill", "runbook": "rb"},  # legacy spelling
        {"id": "e", "action": "eval", "eval_fixture": "f"},
    ],
)
def test_schedule_agent_actions_refused_when_ai_off(entry):
    entry = {**entry, "when": {"every": "5m"}}
    with pytest.raises(ValueError, match="runs an agent turn.*not available"):
        parse_schedule_entry(entry, ai_enabled=False)
    # Unchanged with AI on.
    assert parse_schedule_entry(entry).action in AI_SCHEDULE_ACTIONS


@pytest.mark.parametrize(
    "entry",
    [
        {"id": "r", "action": "runnable", "host": "h", "runnable": "r"},
        {"id": "e", "action": "event", "event": "nightly"},
        {"id": "s", "action": "saved_run", "saved_run": "preset"},
    ],
)
def test_schedule_non_agent_actions_still_parse_when_ai_off(entry):
    job = parse_schedule_entry({**entry, "when": {"every": "5m"}}, ai_enabled=False)
    assert job.id == entry["id"]


def test_schedule_source_skips_agent_jobs_when_ai_off():
    src = ScheduleSource(
        [
            {"id": "p", "action": "prompt", "prompt": "x", "when": {"every": "5m"}},
            {
                "id": "r",
                "action": "runnable",
                "host": "h",
                "runnable": "r",
                "when": {"every": "5m"},
            },
        ],
        ai_enabled=False,
    )
    assert [j.id for j in src._jobs] == ["r"]
