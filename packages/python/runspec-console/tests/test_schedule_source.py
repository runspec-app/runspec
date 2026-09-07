"""
Tests for the schedule event source (sources/schedule.py).

A schedule lives in the Schedules tab (runspec_schedules.toml). When it fires it
either emits a rich, matchable event (action="event") that source="schedule"
triggers pick up like mail, or runs a runnable directly (action="runnable").

The pure helpers (parse_when / parse_schedule_entry / _parse_every /
event_payload) and the _fire dispatch need no APScheduler; the when→APScheduler
conversion test importorskips it, and the routing test drives the engine via
feed() with a synthetic event.
"""

from __future__ import annotations

from concurrent.futures import Executor, Future
from datetime import datetime
from typing import Any, Callable

import pytest

from runspec_console.events import Event
from runspec_console.sources.schedule import (
    ScheduleJob,
    ScheduleSource,
    When,
    _parse_every,
    event_payload,
    parse_schedule_entry,
    parse_when,
)
from runspec_console.triggers import Trigger, TriggerEngine


class InlineExecutor(Executor):
    """Runs submitted work immediately in the calling thread (deterministic)."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        f: Future = Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except Exception as exc:  # pragma: no cover
            f.set_exception(exc)
        return f

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        pass


# ── _parse_every ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("30s", {"seconds": 30}),
        ("15m", {"minutes": 15}),
        ("1h", {"hours": 1}),
        ("2d", {"days": 2}),
        (" 5M ", {"minutes": 5}),  # trimmed + case-insensitive
    ],
)
def test_parse_every_ok(text, expected):
    assert _parse_every(text) == expected


@pytest.mark.parametrize("bad", ["", "m", "15", "15x", "x5m", "0m", "-3h", "1.5h"])
def test_parse_every_rejects_bad(bad):
    with pytest.raises(ValueError):
        _parse_every(bad)


# ── parse_when ────────────────────────────────────────────────────────────────


def test_parse_when_cron():
    w = parse_when({"cron": "30 17 * * mon-fri", "timezone": "Europe/London"})
    assert w == When(cron="30 17 * * mon-fri", every="", timezone="Europe/London")


def test_parse_when_every():
    assert parse_when({"every": "15m"}) == When(every="15m")


def test_parse_when_requires_exactly_one():
    with pytest.raises(ValueError):
        parse_when({})  # neither
    with pytest.raises(ValueError):
        parse_when({"cron": "* * * * *", "every": "5m"})  # both


def test_parse_when_validates_every_now():
    with pytest.raises(ValueError):
        parse_when({"every": "soon"})


# ── parse_schedule_entry ──────────────────────────────────────────────────────


def test_parse_entry_runnable():
    job = parse_schedule_entry(
        {
            "id": "log-rotate-weekly",
            "action": "runnable",
            "host": "prod-1",
            "runnable": "log-rotate",
            "when": {"cron": "0 3 * * 0"},
            "args": {"keep": "7"},
        }
    )
    assert job == ScheduleJob(
        id="log-rotate-weekly",
        when=When(cron="0 3 * * 0"),
        action="runnable",
        enabled=True,
        host="prod-1",
        runnable="log-rotate",
        args={"keep": "7"},
    )


def test_parse_entry_event():
    job = parse_schedule_entry(
        {
            "id": "nightly-report",
            "action": "event",
            "event": "nightly_report",
            "when": {"cron": "0 2 * * *"},
            "fields": {"scope": "prod"},
        }
    )
    assert job.action == "event"
    assert job.event == "nightly_report"
    assert job.fields == {"scope": "prod"}
    assert job.host == "" and job.runnable == ""


def test_parse_entry_action_defaults_to_runnable():
    job = parse_schedule_entry(
        {"id": "x", "host": "h", "runnable": "r", "when": {"every": "5m"}}
    )
    assert job.action == "runnable"


def test_parse_entry_requires_id():
    with pytest.raises(ValueError):
        parse_schedule_entry({"action": "runnable", "host": "h", "runnable": "r"})


def test_parse_entry_unknown_action():
    with pytest.raises(ValueError):
        parse_schedule_entry({"id": "x", "action": "explode", "when": {"every": "5m"}})


def test_parse_entry_event_requires_event_name():
    with pytest.raises(ValueError):
        parse_schedule_entry({"id": "x", "action": "event", "when": {"every": "5m"}})


def test_parse_entry_runnable_requires_host_and_runnable():
    with pytest.raises(ValueError):
        parse_schedule_entry(
            {"id": "x", "action": "runnable", "host": "h", "when": {"every": "5m"}}
        )


def test_parse_entry_runnable_hosts_list():
    job = parse_schedule_entry(
        {
            "id": "x",
            "action": "runnable",
            "hosts": ["web01", "web02"],
            "runnable": "backup",
            "when": {"every": "5m"},
        }
    )
    assert job.hosts == ["web01", "web02"] and not job.host and not job.group


def test_parse_entry_runnable_rejects_two_targets():
    with pytest.raises(ValueError):
        parse_schedule_entry(
            {
                "id": "x",
                "action": "runnable",
                "host": "h",
                "hosts": ["a", "b"],
                "runnable": "r",
                "when": {"every": "5m"},
            }
        )


def test_parse_entry_runnable_selected_hosts():
    """selected_hosts is a valid single targeting mode."""
    job = parse_schedule_entry(
        {
            "id": "x",
            "action": "runnable",
            "selected_hosts": ["web01", "web02"],
            "runnable": "backup",
            "when": {"every": "5m"},
        }
    )
    assert job.selected_hosts == ["web01", "web02"]
    assert not job.host and not job.group and not job.hosts


def test_parse_entry_runnable_selected_hosts_rejects_with_host():
    with pytest.raises(ValueError):
        parse_schedule_entry(
            {
                "id": "x",
                "action": "runnable",
                "host": "web01",
                "selected_hosts": ["web02"],
                "runnable": "r",
                "when": {"every": "5m"},
            }
        )


def test_parse_entry_runnable_selected_hosts_rejects_with_hosts():
    with pytest.raises(ValueError):
        parse_schedule_entry(
            {
                "id": "x",
                "action": "runnable",
                "hosts": ["a"],
                "selected_hosts": ["b"],
                "runnable": "r",
                "when": {"every": "5m"},
            }
        )


def test_parse_entry_runnable_selected_hosts_rejects_with_group():
    with pytest.raises(ValueError):
        parse_schedule_entry(
            {
                "id": "x",
                "action": "runnable",
                "group": "prod",
                "selected_hosts": ["b"],
                "runnable": "r",
                "when": {"every": "5m"},
            }
        )


def test_parse_entry_runnable_no_target_rejected():
    """All four targeting fields absent → ValueError (unchanged behaviour)."""
    with pytest.raises(ValueError):
        parse_schedule_entry(
            {
                "id": "x",
                "action": "runnable",
                "runnable": "r",
                "when": {"every": "5m"},
            }
        )


def test_fire_runnable_selected_hosts_passed_through():
    ran: list = []
    src = ScheduleSource(
        [
            {
                "id": "s1",
                "action": "runnable",
                "selected_hosts": ["web01", "web02"],
                "runnable": "backup",
                "when": {"every": "5m"},
            }
        ],
        run_runnable=lambda s, h, r, a, g, hs, sel, ci, cmd: ran.append(
            (h, g, hs, sel)
        ),
    )
    src._fire("s1")
    assert ran == [("", "", [], ["web01", "web02"])]


def test_parse_entry_reads_sound():
    job = parse_schedule_entry(
        {
            "id": "s1",
            "action": "runnable",
            "host": "web01",
            "runnable": "backup",
            "sound": "alarm",
            "when": {"every": "5m"},
        }
    )
    assert job.sound == "alarm"


def test_fire_plays_sound_then_runs():
    """A schedule's `sound` fires (after the rota gate) before the action runs."""
    order: list = []
    src = ScheduleSource(
        [
            {
                "id": "s1",
                "action": "runnable",
                "host": "web01",
                "runnable": "backup",
                "sound": "sound:nightly-done",
                "when": {"every": "5m"},
            }
        ],
        run_runnable=lambda *a: order.append("run"),
        play_sound=lambda ident: order.append(("sound", ident)),
    )
    src._fire("s1")
    assert order == [("sound", "sound:nightly-done"), "run"]


def test_fire_no_sound_never_plays():
    played: list = []
    src = ScheduleSource(
        [
            {
                "id": "s1",
                "action": "runnable",
                "host": "web01",
                "runnable": "backup",
                "when": {"every": "5m"},
            }
        ],
        run_runnable=lambda *a: None,
        play_sound=lambda ident: played.append(ident),
    )
    src._fire("s1")
    assert played == []


def test_fire_sound_gated_off_outside_rota():
    """Outside the rota the schedule is skipped — and so is its sound cue."""
    played: list = []
    src = ScheduleSource(
        [
            {
                "id": "s1",
                "action": "runnable",
                "host": "web01",
                "runnable": "backup",
                "sound": "alarm",
                "when": {"every": "5m"},
            }
        ],
        run_runnable=lambda *a: None,
        play_sound=lambda ident: played.append(ident),
        clock_ok=lambda: False,
    )
    src._fire("s1")
    assert played == []  # skipped before the sound cue


def test_fire_runnable_hosts_passed_through():
    ran: list = []
    src = ScheduleSource(
        [
            {
                "id": "r1",
                "action": "runnable",
                "hosts": ["web01", "web02"],
                "runnable": "backup",
                "when": {"every": "5m"},
            }
        ],
        run_runnable=lambda s, h, r, a, g, hs, sel, ci, cmd: ran.append(
            (h, g, hs, sel)
        ),
    )
    src._fire("r1")
    assert ran == [("", "", ["web01", "web02"], [])]


def test_parse_entry_prompt():
    job = parse_schedule_entry(
        {
            "id": "morning-check",
            "action": "prompt",
            "prompt": "Check disk usage and flag anything over 80%.",
            "when": {"cron": "0 8 * * *"},
        }
    )
    assert job.action == "prompt"
    assert job.prompt == "Check disk usage and flag anything over 80%."


def test_parse_entry_prompt_requires_prompt():
    with pytest.raises(ValueError):
        parse_schedule_entry({"id": "x", "action": "prompt", "when": {"every": "5m"}})


def test_parse_entry_runbook():
    job = parse_schedule_entry(
        {
            "id": "weekly-report",
            "action": "runbook",
            "runbook": "build-report",
            "values": {"scope": "prod"},
            "when": {"cron": "0 9 * * 1"},
        }
    )
    assert job.action == "runbook"
    assert job.runbook == "build-report"
    assert job.values == {"scope": "prod"}


def test_parse_entry_runbook_requires_runbook_id():
    with pytest.raises(ValueError):
        parse_schedule_entry({"id": "x", "action": "runbook", "when": {"every": "5m"}})


# ── event_payload ─────────────────────────────────────────────────────────────


def test_event_payload_builtins_and_fields():
    # 2026-06-19 is a Friday.
    job = ScheduleJob(
        id="nightly",
        when=When(cron="0 2 * * *"),
        action="event",
        event="nightly_report",
        fields={"scope": "prod"},
    )
    p = event_payload(job, datetime(2026, 6, 19, 17, 30, 5))
    assert p["schedule"] == "nightly"
    assert p["event"] == "nightly_report"
    assert p["scope"] == "prod"  # user field present
    assert p["weekday"] == "Fri"
    assert p["date"] == "2026-06-19"
    assert p["time"] == "17:30"
    assert p["fired_at"].startswith("2026-06-19T17:30")


def test_event_payload_user_field_overrides_builtin():
    job = ScheduleJob(
        id="x",
        when=When(every="5m"),
        action="event",
        event="e",
        fields={"weekday": "custom"},
    )
    assert event_payload(job, datetime(2026, 6, 19))["weekday"] == "custom"


# ── _when_to_trigger (needs APScheduler) ──────────────────────────────────────


def test_when_to_trigger_cron_and_interval():
    pytest.importorskip("apscheduler")
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    from runspec_console.sources.schedule import _when_to_trigger

    cron = _when_to_trigger(When(cron="30 17 * * mon-fri"))
    assert isinstance(cron, CronTrigger)

    interval = _when_to_trigger(When(every="15m"))
    assert isinstance(interval, IntervalTrigger)
    assert interval.interval.total_seconds() == 15 * 60


# ── next_fire_time (needs APScheduler) ────────────────────────────────────────


def test_next_fire_time_cron_is_tomorrow_0200():
    pytest.importorskip("apscheduler")
    from runspec_console.sources.schedule import next_fire_time

    now = datetime(2026, 6, 20, 12, 0, 0).astimezone()
    nxt = next_fire_time(When(cron="0 2 * * *"), now=now)
    assert nxt is not None
    assert (nxt.year, nxt.month, nxt.day, nxt.hour, nxt.minute) == (2026, 6, 21, 2, 0)


def test_next_fire_time_interval_is_aware():
    pytest.importorskip("apscheduler")
    from runspec_console.sources.schedule import next_fire_time

    nxt = next_fire_time(When(every="15m"))
    assert nxt is not None
    assert nxt.tzinfo is not None  # always timezone-aware


def test_next_fire_time_returns_none_without_apscheduler(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "apscheduler" or name.startswith("apscheduler."):
            raise ImportError("no apscheduler")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    from runspec_console.sources.schedule import next_fire_time

    assert next_fire_time(When(cron="0 2 * * *")) is None


# ── ScheduleSource._fire: emit an event / run a runnable ──────────────────────


def _source_with_jobs(
    run_runnable=None, *, clock_ok=None, on_event=None, ignore_rota=False
):
    return ScheduleSource(
        [
            {
                "id": "e1",
                "action": "event",
                "event": "nightly",
                "when": {"every": "5m"},
                "fields": {"scope": "prod"},
                "ignore_rota": ignore_rota,
            },
            {
                "id": "r1",
                "action": "runnable",
                "host": "h",
                "runnable": "backup",
                "when": {"cron": "0 2 * * *"},
                "args": {"db": "main"},
                "ignore_rota": ignore_rota,
            },
        ],
        run_runnable=run_runnable,
        clock_ok=clock_ok,
        on_event=on_event,
    )


def test_fire_event_emits_matchable_event():
    emitted: list[Event] = []
    src = _source_with_jobs()
    src._emit = emitted.append  # simulate a started source (no apscheduler needed)
    src._fire("e1")
    assert len(emitted) == 1
    ev = emitted[0]
    assert ev.source == "schedule" and ev.type == "nightly"
    assert ev.payload["scope"] == "prod" and ev.payload["schedule"] == "e1"


def test_fire_runnable_calls_run_runnable():
    ran: list[tuple[str, str, str, dict, str, list, list, list]] = []
    src = _source_with_jobs(
        run_runnable=lambda s, h, r, a, g, hs, sel, ci, cmd: ran.append(
            (s, h, r, a, g, hs, sel, ci)
        )
    )
    src._fire("r1")
    assert ran == [("r1", "h", "backup", {"db": "main"}, "", [], [], [])]


def test_parse_entry_credential_ids():
    job = parse_schedule_entry(
        {
            "id": "jira-nightly",
            "action": "runnable",
            "host": "h",
            "runnable": "jsm-raise",
            "when": {"every": "5m"},
            "credential_ids": ["cred-jira", " cred-extra ", ""],
        }
    )
    # Ids are trimmed and blanks dropped.
    assert job.credential_ids == ["cred-jira", "cred-extra"]


def test_parse_entry_credential_ids_default_empty():
    job = parse_schedule_entry(
        {
            "id": "r1",
            "action": "runnable",
            "host": "h",
            "runnable": "backup",
            "when": {"every": "5m"},
        }
    )
    assert job.credential_ids == []


def test_fire_runnable_threads_credential_ids():
    ran: list = []
    src = ScheduleSource(
        [
            {
                "id": "c1",
                "action": "runnable",
                "host": "h",
                "runnable": "jsm-raise",
                "when": {"every": "5m"},
                "credential_ids": ["cred-jira"],
            }
        ],
        run_runnable=lambda s, h, r, a, g, hs, sel, ci, cmd: ran.append(ci),
    )
    src._fire("c1")
    assert ran == [["cred-jira"]]


def test_fire_prompt_calls_run_agent():
    ran: list = []
    src = ScheduleSource(
        [
            {
                "id": "p1",
                "action": "prompt",
                "prompt": "Check disk usage.",
                "when": {"every": "5m"},
            }
        ],
        run_agent=ran.append,
    )
    src._fire("p1")
    assert len(ran) == 1
    assert ran[0].action == "prompt" and ran[0].prompt == "Check disk usage."


def test_fire_runbook_calls_run_agent():
    ran: list = []
    src = ScheduleSource(
        [
            {
                "id": "s1",
                "action": "runbook",
                "runbook": "build-report",
                "when": {"every": "5m"},
            }
        ],
        run_agent=ran.append,
    )
    src._fire("s1")
    assert len(ran) == 1 and ran[0].runbook == "build-report"


def test_fire_unknown_id_is_noop():
    src = _source_with_jobs()
    src._emit = lambda ev: pytest.fail("should not emit")  # type: ignore[assignment]
    src._fire("nope")  # no such job — silently ignored


# ── ScheduleSource._fire: working-hours rota gate + event log ──────────────────


def test_fire_skipped_outside_working_hours():
    """clock_ok=False drops the fire (no emit/run) and records a skipped event."""
    emitted: list[Event] = []
    events: list[dict] = []
    src = _source_with_jobs(clock_ok=lambda: False, on_event=events.append)
    src._emit = emitted.append
    src._fire("e1")
    assert emitted == []  # event-action schedule never emitted
    assert len(events) == 1
    rec = events[0]
    assert rec["source"] == "schedule" and rec["type"] == "nightly"
    assert rec["skipped"] == ["e1"] and rec["matched"] == []


def test_fire_runnable_skipped_outside_working_hours():
    ran: list = []
    events: list[dict] = []
    src = _source_with_jobs(
        run_runnable=lambda *a: ran.append(a),
        clock_ok=lambda: False,
        on_event=events.append,
    )
    src._fire("r1")
    assert ran == []  # runnable-action schedule never ran
    assert events[0]["skipped"] == ["r1"]


def test_fire_ignore_rota_fires_outside_working_hours():
    """A schedule with ignore_rota fires even when the rota would skip it."""
    ran: list = []
    events: list[dict] = []
    src = _source_with_jobs(
        run_runnable=lambda *a: ran.append(a),
        clock_ok=lambda: False,
        on_event=events.append,
        ignore_rota=True,
    )
    src._fire("r1")
    assert len(ran) == 1
    assert events[0]["matched"] == ["r1"] and events[0]["skipped"] == []


def test_fire_within_working_hours_records_fired_event():
    emitted: list[Event] = []
    events: list[dict] = []
    src = _source_with_jobs(clock_ok=lambda: True, on_event=events.append)
    src._emit = emitted.append
    src._fire("e1")
    assert len(emitted) == 1
    assert events[0]["matched"] == ["e1"] and events[0]["skipped"] == []


# ── ScheduleSource introspection ──────────────────────────────────────────────


def test_source_event_types_lists_event_schedule_names():
    src = _source_with_jobs()
    assert src.event_types() == ["nightly"]  # only the event-action schedule


def test_source_skips_disabled_and_invalid():
    src = ScheduleSource(
        [
            {"id": "ok", "action": "event", "event": "e", "when": {"every": "5m"}},
            {
                "id": "off",
                "action": "event",
                "event": "e",
                "when": {"every": "5m"},
                "enabled": False,
            },
            {"id": "bad", "action": "runnable", "when": {"every": "5m"}},  # no host
        ]
    )
    assert [j.id for j in src._jobs] == ["ok"]


def test_source_zero_arg_construct():
    src = ScheduleSource()  # registry/UI instance — no jobs
    assert src.id == "schedule"
    assert src.event_types() == []


def test_source_available_reports_extra_when_missing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "apscheduler" or name.startswith("apscheduler."):
            raise ImportError("no apscheduler")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    ok, reason = ScheduleSource().available()
    assert ok is False
    assert "schedule" in reason


# ── engine routing: one scheduled event fans out to matching triggers ─────────


def test_scheduled_event_fans_out_by_event_type_and_match():
    fired: list[str] = []

    def handler(card, trigger, event):
        fired.append(trigger.name)

    # A and B both listen for the "nightly" event; A narrows by a match field, B
    # takes any "nightly". C listens for a different event and must not fire.
    a = Trigger.from_config(
        {
            "name": "A",
            "source": "schedule",
            "event": "nightly",
            "mode": "rule",
            "run": "h__a",
            "match": {"scope": "prod"},
        }
    )
    b = Trigger.from_config(
        {
            "name": "B",
            "source": "schedule",
            "event": "nightly",
            "mode": "rule",
            "run": "h__b",
        }
    )
    c = Trigger.from_config(
        {
            "name": "C",
            "source": "schedule",
            "event": "weekly",
            "mode": "rule",
            "run": "h__c",
        }
    )
    engine = TriggerEngine(
        [a, b, c],
        handler=handler,
        create_source=lambda name: None,
        external_sources={"schedule"},  # bridge-owned; fed via feed()
        executor=InlineExecutor(),
    )
    engine.start()

    engine.feed(Event.new("schedule", "nightly", {"scope": "prod"}))
    assert sorted(fired) == ["A", "B"]  # one emit → two triggers; C untouched

    fired.clear()
    engine.feed(Event.new("schedule", "nightly", {"scope": "dev"}))
    assert fired == ["B"]  # A's match excludes it


# ── saved_run action ──────────────────────────────────────────────────────────


def test_parse_entry_saved_run():
    job = parse_schedule_entry(
        {
            "id": "nightly-restart",
            "action": "saved_run",
            "saved_run": "restart-web",
            "values": {"reason": "nightly"},
            "when": {"cron": "0 2 * * *"},
        }
    )
    assert job.action == "saved_run"
    assert job.saved_run == "restart-web"
    assert job.values == {"reason": "nightly"}


def test_parse_entry_saved_run_requires_id():
    with pytest.raises(ValueError):
        parse_schedule_entry(
            {"id": "x", "action": "saved_run", "when": {"every": "5m"}}
        )


def test_fire_saved_run_calls_run_saved():
    fired: list[str] = []
    src = ScheduleSource(
        [
            {
                "id": "s1",
                "action": "saved_run",
                "saved_run": "restart-web",
                "values": {"reason": "nightly"},
                "when": {"every": "5m"},
            }
        ],
        run_saved=lambda job: fired.append(job.saved_run),
    )
    src._fire("s1")
    assert fired == ["restart-web"]


def test_record_saved_run_summary():
    records: list[dict] = []
    src = ScheduleSource(
        [
            {
                "id": "s1",
                "action": "saved_run",
                "saved_run": "restart-web",
                "when": {"every": "5m"},
            }
        ],
        run_saved=lambda job: None,
        on_event=records.append,
    )
    src._fire("s1")
    assert records[0]["type"] == "saved_run"
    assert records[0]["summary"] == "saved run: restart-web"
    assert records[0]["matched"] == ["s1"]


# ── runnable-action subcommand path ───────────────────────────────────────────


def test_parse_entry_command_accepts_string_and_list():
    base = {
        "id": "x",
        "action": "runnable",
        "host": "h",
        "runnable": "backup",
        "when": {"every": "5m"},
    }
    assert parse_schedule_entry({**base, "command": "db restore"}).command == [
        "db",
        "restore",
    ]
    assert parse_schedule_entry({**base, "command": ["db"]}).command == ["db"]
    assert parse_schedule_entry(base).command == []


def test_fire_runnable_threads_command():
    ran: list[list[str]] = []
    src = ScheduleSource(
        [
            {
                "id": "r1",
                "action": "runnable",
                "host": "h",
                "runnable": "backup",
                "command": ["db", "restore"],
                "when": {"cron": "0 2 * * *"},
            }
        ],
        run_runnable=lambda s, h, r, a, g, hs, sel, ci, cmd: ran.append(cmd),
    )
    src._fire("r1")
    assert ran == [["db", "restore"]]
