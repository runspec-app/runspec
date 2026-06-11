"""
Tests for the trigger engine (triggers.py).

Pure logic — sources are FakeSource, the action is a fake handler, and most
tests use a synchronous executor so routing is deterministic without sleeps.
One test exercises real per-trigger serialisation with a thread pool.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Executor, Future
from typing import Any, Callable

import pytest

from datetime import datetime

from runspec_console.events import Event, FakeSource
from runspec_console.triggers import (
    Column,
    Trigger,
    TriggerCard,
    TriggerEngine,
    load_triggers,
    within_rota,
)


_ALL_ON = {
    "enabled": True,
    **{
        d: {"on": True, "start": "09:00", "end": "17:00"}
        for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    },
}


class InlineExecutor(Executor):
    """Runs submitted work immediately in the calling thread (deterministic)."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        f: Future = Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except Exception as exc:  # pragma: no cover - mirrored onto the future
            f.set_exception(exc)
        return f

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        pass


def _mail(**payload: Any) -> Event:
    return Event.new("outlook", "new_mail", payload)


# ── Trigger.from_config ───────────────────────────────────────────────────────


def test_from_config_minimal_defaults():
    t = Trigger.from_config({"name": "t", "source": "outlook", "event": "new_mail"})
    assert t.mode == "agent"
    assert t.enabled is True
    assert t.autonomy == "confirm"
    assert t.post_to_chat is False
    assert t.match == {}
    assert t.reply.draft is False


def test_from_config_full():
    t = Trigger.from_config(
        {
            "name": "vip",
            "source": "outlook",
            "event": "new_mail",
            "mode": "rule",
            "enabled": False,
            "autonomy": "supervised",
            "post_to_chat": True,
            "match": {"subject": "(?i)urgent"},
            "run": "host__do",
            "map": {"service": "{{ subject }}"},
            "reply": {"draft": True, "style": "short"},
        }
    )
    assert t.mode == "rule"
    assert t.enabled is False
    assert t.autonomy == "supervised"
    assert t.post_to_chat is True
    assert t.run == "host__do"
    assert t.arg_map == {"service": "{{ subject }}"}
    assert t.reply.draft is True and t.reply.style == "short"


@pytest.mark.parametrize(
    "data, msg",
    [
        ({}, "missing a 'name'"),
        ({"name": "t", "event": "e"}, "missing 'source'"),
        ({"name": "t", "source": "s"}, "missing 'event'"),
        ({"name": "t", "source": "s", "event": "e", "mode": "x"}, "unknown mode"),
        (
            {"name": "t", "source": "s", "event": "e", "autonomy": "x"},
            "unknown autonomy",
        ),
        (
            {"name": "t", "source": "s", "event": "e", "match": {"f": "("}},
            "invalid match regex",
        ),
        ({"name": "t", "source": "s", "event": "e", "mode": "rule"}, "no 'run'"),
        (
            # `rules` written after [triggers.match] → parsed into match (the
            # classic TOML ordering slip); must be rejected, not silently broken.
            {
                "name": "t",
                "source": "s",
                "event": "e",
                "match": {"subject": "x", "rules": "do stuff"},
            },
            "misplaced trigger keys",
        ),
    ],
)
def test_from_config_errors(data, msg):
    with pytest.raises(ValueError) as exc:
        Trigger.from_config(data)
    assert msg in str(exc.value)


# ── matching ──────────────────────────────────────────────────────────────────


def test_matches_source_and_type():
    t = Trigger.from_config({"name": "t", "source": "outlook", "event": "new_mail"})
    assert t.matches(_mail(subject="hi"))
    assert not t.matches(Event.new("outlook", "reminder", {}))
    assert not t.matches(Event.new("other", "new_mail", {}))


def test_matches_empty_match_matches_all():
    t = Trigger.from_config({"name": "t", "source": "outlook", "event": "new_mail"})
    assert t.matches(_mail())


def test_matches_all_patterns_required():
    t = Trigger.from_config(
        {
            "name": "t",
            "source": "outlook",
            "event": "new_mail",
            "match": {"from": ".*@big\\.com", "subject": "(?i)outage"},
        }
    )
    assert t.matches(_mail(**{"from": "a@big.com", "subject": "Outage now"}))
    assert not t.matches(_mail(**{"from": "a@big.com", "subject": "lunch"}))
    assert not t.matches(_mail(subject="Outage now"))  # missing 'from' field


def test_matches_disabled_never():
    t = Trigger.from_config(
        {"name": "t", "source": "outlook", "event": "new_mail", "enabled": False}
    )
    assert not t.matches(_mail())


# ── arg rendering ─────────────────────────────────────────────────────────────


def test_render_args_templates_and_missing():
    t = Trigger.from_config(
        {
            "name": "t",
            "source": "s",
            "event": "e",
            "mode": "rule",
            "run": "h__r",
            "map": {"svc": "{{ subject }}", "static": "x", "mix": "{{a}}-{{b}}"},
        }
    )
    out = t.render_args({"subject": "web", "a": "1", "b": "2"})
    assert out == {"svc": "web", "static": "x", "mix": "1-2"}
    assert t.render_args({})["svc"] == ""  # missing key → empty


# ── load_triggers ─────────────────────────────────────────────────────────────


def test_load_triggers_skips_bad():
    triggers, errors = load_triggers(
        [
            {"name": "good", "source": "outlook", "event": "new_mail"},
            {"name": "bad", "source": "outlook"},  # missing event
        ]
    )
    assert [t.name for t in triggers] == ["good"]
    assert len(errors) == 1 and "missing 'event'" in errors[0]


# ── TriggerCard ───────────────────────────────────────────────────────────────


def test_card_move_fires_hook_and_merges_detail():
    seen: list[tuple[str, dict]] = []
    card = TriggerCard(
        id="c1",
        trigger="t",
        source="outlook",
        event_type="new_mail",
        summary="hi",
        _on_change=lambda c: seen.append((c.column, dict(c.detail))),
    )
    card.move(Column.RUNNING, host="h")
    card.move(Column.DONE)
    assert [c for c, _ in seen] == [Column.RUNNING, Column.DONE]
    assert seen[0][1] == {"host": "h"}
    assert card.terminal


def test_card_to_dict():
    card = TriggerCard("c1", "t", "outlook", "new_mail", "hi")
    d = card.to_dict()
    assert d["card_id"] == "c1" and d["column"] == Column.TRIGGERED


# ── engine routing ────────────────────────────────────────────────────────────


# ── working-hours rota ────────────────────────────────────────────────────────


def test_within_rota_no_or_disabled_is_always_on():
    assert within_rota(None, datetime(2026, 6, 10, 3, 0)) is True
    assert within_rota({}, datetime(2026, 6, 10, 3, 0)) is True
    assert (
        within_rota({**_ALL_ON, "enabled": False}, datetime(2026, 6, 10, 3, 0)) is True
    )


def test_within_rota_in_and_out_of_window():
    assert within_rota(_ALL_ON, datetime(2026, 6, 10, 10, 0)) is True  # 10:00
    assert within_rota(_ALL_ON, datetime(2026, 6, 10, 8, 59)) is False  # before
    assert within_rota(_ALL_ON, datetime(2026, 6, 10, 17, 1)) is False  # after


def test_within_rota_day_off():
    # Every day off → never within, whatever the time.
    rota = {
        "enabled": True,
        **{d: {"on": False} for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
    }
    assert within_rota(rota, datetime(2026, 6, 10, 12, 0)) is False


def test_within_rota_overnight_window():
    rota = {
        "enabled": True,
        **{
            d: {"on": True, "start": "22:00", "end": "06:00"}
            for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        },
    }
    assert within_rota(rota, datetime(2026, 6, 10, 23, 0)) is True
    assert within_rota(rota, datetime(2026, 6, 10, 5, 0)) is True
    assert within_rota(rota, datetime(2026, 6, 10, 12, 0)) is False


def test_engine_emits_event_records():
    recs: list[dict] = []

    def handler(card, trigger, event):
        card.move(Column.DONE)

    t = Trigger.from_config(
        {
            "name": "vip",
            "source": "outlook",
            "event": "new_mail",
            "match": {"subject": "(?i)urgent"},
        }
    )
    engine, fake, _ = _engine([t], handler, on_event=recs.append)
    engine.start()
    fake.fire("new_mail", {"subject": "URGENT: down"})  # matches
    fake.fire("new_mail", {"subject": "lunch?"})  # no match

    assert len(recs) == 2  # every event is audited, matched or not
    assert recs[0]["matched"] == ["vip"] and recs[0]["skipped"] == []
    assert recs[0]["summary"] == "URGENT: down"
    assert recs[1]["matched"] == [] and recs[1]["skipped"] == []


def test_engine_event_record_marks_rota_skip():
    recs: list[dict] = []

    def handler(card, trigger, event):
        card.move(Column.DONE)

    t = Trigger.from_config({"name": "vip", "source": "outlook", "event": "new_mail"})
    engine, fake, _ = _engine(
        [t], handler, on_event=recs.append, clock_ok=lambda: False
    )
    engine.start()
    fake.fire("new_mail", {"subject": "x"})
    assert recs[0]["matched"] == [] and recs[0]["skipped"] == ["vip"]


def test_engine_clock_gate_skips_outside_hours():
    fired: list[str] = []

    def handler(card, trigger, event):
        fired.append(trigger.name)
        card.move(Column.DONE)

    gated = Trigger.from_config({"name": "g", "source": "outlook", "event": "new_mail"})
    always = Trigger.from_config(
        {"name": "a", "source": "outlook", "event": "new_mail", "ignore_rota": True}
    )
    engine, fake, cards = _engine(
        [gated, always], handler, cards=[], clock_ok=lambda: False
    )
    engine.start()
    fake.fire("new_mail", {"subject": "x"})
    # Only the ignore_rota trigger fires while outside working hours.
    assert fired == ["a"]


# ── card timeline ─────────────────────────────────────────────────────────────


def test_card_timeline_records_stages_and_logs():
    card = TriggerCard(
        id="c1", trigger="t", source="outlook", event_type="new_mail", summary="hi"
    )
    assert [e["text"] for e in card.timeline] == ["triggered"]  # seeded
    card.move(Column.TRIAGE)
    card.log("check-memory → ok")
    card.move(Column.DONE)
    assert [e["text"] for e in card.timeline] == [
        "triggered",
        "triage",
        "check-memory → ok",
        "done",
    ]
    assert all("ts" in e for e in card.timeline)
    assert card.to_dict()["timeline"] == card.timeline


def _engine(triggers, handler, **kw):
    """Engine wired to a single FakeSource and a synchronous executor."""
    fake = FakeSource(id="outlook", types=["new_mail", "reminder"])
    cards: list[TriggerCard] = kw.pop("cards", [])
    engine = TriggerEngine(
        triggers,
        handler=handler,
        on_card=cards.append if cards is not None else None,
        create_source=lambda name: fake if name == "outlook" else None,
        executor=InlineExecutor(),
        **kw,
    )
    return engine, fake, cards


def test_engine_routes_matching_event_to_handler():
    calls: list[tuple[str, str]] = []

    def handler(card, trigger, event):
        calls.append((trigger.name, event.payload.get("subject", "")))
        card.move(Column.DONE)

    t = Trigger.from_config(
        {
            "name": "vip",
            "source": "outlook",
            "event": "new_mail",
            "match": {"subject": "(?i)outage"},
        }
    )
    engine, fake, cards = _engine([t], handler, cards=[])
    engine.start()

    fake.fire("new_mail", {"subject": "Outage on web01"})
    fake.fire("new_mail", {"subject": "lunch?"})  # no match

    assert calls == [("vip", "Outage on web01")]
    # one card created, ending DONE
    assert len(cards) >= 1
    assert any(c.column == Column.DONE for c in cards)


def test_engine_handler_exception_marks_failed():
    def handler(card, trigger, event):
        raise RuntimeError("boom")

    t = Trigger.from_config({"name": "t", "source": "outlook", "event": "new_mail"})
    engine, fake, cards = _engine([t], handler, cards=[])
    engine.start()
    fake.fire("new_mail", {"subject": "x"})

    failed = [c for c in cards if c.column == Column.FAILED]
    assert failed and failed[-1].detail.get("error") == "boom"


def test_engine_auto_done_when_handler_leaves_nonterminal():
    def handler(card, trigger, event):
        card.move(Column.RUNNING)  # handler stops before terminal

    t = Trigger.from_config({"name": "t", "source": "outlook", "event": "new_mail"})
    engine, fake, cards = _engine([t], handler, cards=[])
    engine.start()
    fake.fire("new_mail", {"subject": "x"})
    assert cards[-1].column == Column.DONE


def test_engine_multiple_triggers_match_one_event():
    fired: list[str] = []

    def handler(card, trigger, event):
        fired.append(trigger.name)
        card.move(Column.DONE)

    a = Trigger.from_config({"name": "a", "source": "outlook", "event": "new_mail"})
    b = Trigger.from_config({"name": "b", "source": "outlook", "event": "new_mail"})
    engine, fake, _ = _engine([a, b], handler)
    engine.start()
    fake.fire("new_mail", {"subject": "x"})
    assert sorted(fired) == ["a", "b"]


# ── engine start() problem reporting ─────────────────────────────────────────


def test_start_reports_unknown_source():
    t = Trigger.from_config({"name": "t", "source": "ghost", "event": "e"})
    engine = TriggerEngine(
        [t], handler=lambda *a: None, create_source=lambda name: None
    )
    problems = engine.start()
    assert any("unknown event source 'ghost'" in p for p in problems)


def test_start_reports_unavailable_source():
    class Down(FakeSource):
        def available(self) -> tuple[bool, str]:
            return (False, "not on Windows")

    t = Trigger.from_config({"name": "t", "source": "outlook", "event": "new_mail"})
    engine = TriggerEngine(
        [t],
        handler=lambda *a: None,
        create_source=lambda name: Down(id="outlook"),
    )
    problems = engine.start()
    assert any("unavailable: not on Windows" in p for p in problems)


def test_start_skips_disabled_trigger_sources():
    created: list[str] = []

    def factory(name):
        created.append(name)
        return FakeSource(id=name)

    t = Trigger.from_config(
        {"name": "t", "source": "outlook", "event": "new_mail", "enabled": False}
    )
    engine = TriggerEngine([t], handler=lambda *a: None, create_source=factory)
    engine.start()
    assert created == []  # disabled trigger's source never started


# ── external sources (engine doesn't start them; feed() routes their events) ──


def test_external_source_not_started():
    created: list[str] = []

    def factory(name):
        created.append(name)
        return FakeSource(id=name)

    t = Trigger.from_config({"name": "t", "source": "chat", "event": "message"})
    engine = TriggerEngine(
        [t],
        handler=lambda *a: None,
        create_source=factory,
        external_sources={"chat"},
    )
    problems = engine.start()
    assert created == []  # the engine never constructs an external source
    assert problems == []  # …and reports no 'unavailable' noise for it


def test_feed_routes_external_event_to_handler():
    seen: list[tuple[str, Event]] = []

    def handler(card, trigger, event):
        seen.append((trigger.name, event))

    t = Trigger.from_config({"name": "chatty", "source": "chat", "event": "message"})
    engine = TriggerEngine(
        [t],
        handler=handler,
        create_source=lambda name: None,
        external_sources={"chat"},
        executor=InlineExecutor(),
    )
    engine.start()
    engine.feed(Event.new("chat", "message", {"room": "ops", "text": "help"}))
    assert [name for name, _ in seen] == ["chatty"]


def test_feed_respects_the_rota_gate():
    seen: list[str] = []
    t = Trigger.from_config({"name": "t", "source": "chat", "event": "message"})
    engine = TriggerEngine(
        [t],
        handler=lambda c, tr, e: seen.append(tr.name),
        create_source=lambda name: None,
        external_sources={"chat"},
        executor=InlineExecutor(),
        clock_ok=lambda: False,  # outside working hours
    )
    engine.start()
    engine.feed(Event.new("chat", "message", {"text": "x"}))
    assert seen == []  # gated by the rota, same as a built-in source's events


# ── per-trigger serialisation (real thread pool) ─────────────────────────────


def test_same_trigger_runs_serialised():
    active = 0
    max_active = 0
    guard = threading.Lock()

    def handler(card, trigger, event):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        card.move(Column.DONE)

    fake = FakeSource(id="outlook", types=["new_mail"])
    t = Trigger.from_config({"name": "t", "source": "outlook", "event": "new_mail"})
    engine = TriggerEngine(
        [t],
        handler=handler,
        create_source=lambda name: fake,
        max_workers=4,  # pool could run them concurrently — the lock must not
    )
    engine.start()
    # _route returns the submitted run's Future, so we can wait on both runs.
    f1 = engine._route(Event.new("outlook", "new_mail", {"subject": "1"}))
    f2 = engine._route(Event.new("outlook", "new_mail", {"subject": "2"}))
    for f in (f1, f2):
        assert f is not None
        f.result(timeout=5)
    engine.stop()
    assert max_active == 1  # never two runs of the same trigger at once
