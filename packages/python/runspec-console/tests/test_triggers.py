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
    assert t.once_per_thread is None  # tri-state: inherit the mailbox default


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
        (
            {"name": "t", "source": "s", "event": "e", "runnables": "not-a-list"},
            "'runnables' must be a list",
        ),
        (
            # an agent allowlist is meaningless in rule mode (which runs `run`).
            {
                "name": "t",
                "source": "s",
                "event": "e",
                "mode": "rule",
                "run": "h__r",
                "runnables": ["h__r"],
            },
            "agent tool allowlist",
        ),
        (
            # claim only applies to the Outlook new_mail source.
            {
                "name": "t",
                "source": "schedule",
                "event": "tick",
                "claim": True,
                "claim_folder": "Claimed",
                "match": {"folder": "^x$"},
            },
            "only supported for source='outlook'",
        ),
        (
            # a claim trigger must scope to a folder (match.folder).
            {
                "name": "t",
                "source": "outlook",
                "event": "new_mail",
                "claim": True,
                "claim_folder": "Claimed",
            },
            "needs a folder to match on",
        ),
        (
            # a claim trigger must name the destination folder.
            {
                "name": "t",
                "source": "outlook",
                "event": "new_mail",
                "claim": True,
                "match": {"folder": "^Open$"},
            },
            "needs 'claim_folder'",
        ),
    ],
)
def test_from_config_errors(data, msg):
    with pytest.raises(ValueError) as exc:
        Trigger.from_config(data)
    assert msg in str(exc.value)


def test_from_config_claim_fields():
    t = Trigger.from_config(
        {
            "name": "hd",
            "source": "outlook",
            "event": "new_mail",
            "claim": True,
            "claim_folder": "Tickets/Claimed",
            "match": {"folder": "^Tickets/Open$"},
        }
    )
    assert t.claim is True
    assert t.claim_folder == "Tickets/Claimed"


def test_from_config_claim_defaults_off():
    t = Trigger.from_config({"name": "t", "source": "outlook", "event": "new_mail"})
    assert t.claim is False
    assert t.claim_folder == ""


def test_from_config_runnables_allowlist():
    t = Trigger.from_config(
        {
            "name": "t",
            "source": "outlook",
            "event": "new_mail",
            "runnables": ["web01__check-memory", " restart-service ", ""],
        }
    )
    # entries are trimmed and blanks dropped; default (no key) is an empty list.
    assert t.runnables == ["web01__check-memory", "restart-service"]
    bare = Trigger.from_config({"name": "t", "source": "s", "event": "e"})
    assert bare.runnables == []


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
    # The reason rides the audit record so the UI can label it (rota, not lock).
    assert recs[0]["skip_reasons"] == {"vip": "working hours"}


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


def test_engine_lock_gate_is_absolute_even_for_ignore_rota():
    """Lock auto-suspend is a presence governance control: while the screen is
    locked, NO reactive trigger fires — not even an ``ignore_rota`` one."""
    fired: list[str] = []
    recs: list[dict] = []

    def handler(card, trigger, event):
        fired.append(trigger.name)
        card.move(Column.DONE)

    gated = Trigger.from_config({"name": "g", "source": "outlook", "event": "new_mail"})
    always = Trigger.from_config(
        {"name": "a", "source": "outlook", "event": "new_mail", "ignore_rota": True}
    )
    engine, fake, _ = _engine(
        [gated, always], handler, on_event=recs.append, lock_ok=lambda: False
    )
    engine.start()
    fake.fire("new_mail", {"subject": "x"})
    assert fired == []  # absolute — ignore_rota does not bypass the lock gate
    assert set(recs[-1]["skipped"]) == {"g", "a"}
    # Both are recorded as lock skips, distinct from a rota skip, so the event
    # log can say "screen locked" without the operator opening the log file.
    assert recs[-1]["skip_reasons"] == {"g": "screen locked", "a": "screen locked"}


def test_engine_lock_gate_exempts_scheduled_jobs():
    """A pre-authorised schedule-sourced event still fires while locked — a 2am
    job isn't a reaction-while-away."""
    fired: list[str] = []

    def handler(card, trigger, event):
        fired.append(trigger.name)
        card.move(Column.DONE)

    sched = Trigger.from_config(
        {"name": "nightly", "source": "schedule", "event": "tick"}
    )
    engine, _, _ = _engine([sched], handler, lock_ok=lambda: False)
    engine.start()
    engine.feed(Event.new("schedule", "tick", {}))
    assert fired == ["nightly"]  # exempt from the lock gate


def test_engine_lock_gate_allows_when_unlocked():
    fired: list[str] = []

    def handler(card, trigger, event):
        fired.append(trigger.name)
        card.move(Column.DONE)

    t = Trigger.from_config({"name": "g", "source": "outlook", "event": "new_mail"})
    engine, fake, _ = _engine([t], handler, lock_ok=lambda: True)
    engine.start()
    fake.fire("new_mail", {"subject": "x"})
    assert fired == ["g"]


def test_fire_test_dispatches_one_trigger_bypassing_gates():
    """'Trigger now' fires exactly the named trigger, ignoring match/rota/dedup."""
    fired: list[str] = []
    recs: list[dict] = []

    def handler(card, trigger, event):
        fired.append(trigger.name)
        card.move(Column.DONE)

    # A non-matching `match` + clock_ok=False would normally suppress this entirely.
    t = Trigger.from_config(
        {
            "name": "vip",
            "source": "outlook",
            "event": "new_mail",
            "match": {"subject": "(?i)never-matches-this"},
        }
    )
    other = Trigger.from_config(
        {"name": "other", "source": "outlook", "event": "new_mail"}
    )
    engine, _, _ = _engine(
        [t, other], handler, on_event=recs.append, clock_ok=lambda: False
    )
    engine.start()
    engine.fire_test("vip", Event.new("outlook", "new_mail", {"body": "hello"}))
    assert fired == ["vip"]  # only the named trigger, and it ran despite the gates
    assert recs[-1]["matched"] == ["vip"]
    # An unknown name is a no-op.
    assert engine.fire_test("nope", Event.new("outlook", "new_mail", {})) is None
    assert fired == ["vip"]


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


def _claim_trigger():
    return Trigger.from_config(
        {
            "name": "hd",
            "source": "outlook",
            "event": "new_mail",
            "claim": True,
            "claim_folder": "Tickets/Claimed",
            "match": {"folder": "^Tickets/Open$"},
        }
    )


def test_claim_trigger_parks_without_running():
    ran: list[str] = []

    def handler(card, trigger, event):
        ran.append(card.id)
        card.move(Column.DONE)

    engine, fake, cards = _engine([_claim_trigger()], handler, cards=[])
    engine.start()
    fake.fire("new_mail", {"folder": "Tickets/Open", "entry_id": "E1", "subject": "hi"})

    # The handler never ran; the card sits parked awaiting a claim.
    assert ran == []
    assert cards[-1].column == Column.AWAITING_CLAIM
    assert engine.parked_claim_ids() == [cards[-1].id]
    trig, ev = engine.parked_claim(cards[-1].id)
    assert trig.claim_folder == "Tickets/Claimed"
    assert ev.payload["entry_id"] == "E1"


def test_resume_claim_runs_the_action():
    ran: list[str] = []

    def handler(card, trigger, event):
        ran.append(event.payload.get("entry_id", ""))
        card.move(Column.DONE)

    engine, fake, cards = _engine([_claim_trigger()], handler, cards=[])
    engine.start()
    fake.fire("new_mail", {"folder": "Tickets/Open", "entry_id": "E1"})
    card_id = cards[-1].id

    assert engine.resume_claim(card_id) is True
    assert ran == ["E1"]  # the action ran on resume
    assert cards[-1].column == Column.DONE
    assert engine.parked_claim_ids() == []  # no longer parked
    # A second resume is a no-op (already resolved).
    assert engine.resume_claim(card_id) is False


def test_mark_claimed_elsewhere_resolves_card():
    engine, fake, cards = _engine([_claim_trigger()], lambda *a: None, cards=[])
    engine.start()
    fake.fire("new_mail", {"folder": "Tickets/Open", "entry_id": "E1"})
    card_id = cards[-1].id

    assert engine.mark_claimed_elsewhere(card_id, "alice") is True
    assert cards[-1].column == Column.DONE
    assert cards[-1].detail.get("claimed_by") == "alice"
    assert engine.parked_claim_ids() == []


def test_fire_test_bypasses_claim_gate():
    # "Trigger now" on a claim trigger runs the action directly (no parking).
    ran: list[str] = []

    def handler(card, trigger, event):
        ran.append(card.id)
        card.move(Column.DONE)

    engine, fake, cards = _engine([_claim_trigger()], handler, cards=[])
    engine.start()
    engine.fire_test("hd", _mail(folder="Tickets/Open", entry_id="E9"))
    assert ran  # ran without a claim
    assert engine.parked_claim_ids() == []


def test_once_per_thread_fires_only_for_first_message_in_conversation():
    # A once_per_thread trigger reacts to the opening message of a conversation and
    # skips later replies (keyed on conversation_id) — the "don't re-run on every
    # reply" guarantee.
    fired: list[str] = []
    recs: list[dict] = []

    def handler(card, trigger, event):
        fired.append(event.payload.get("subject", ""))
        card.move(Column.DONE)

    t = Trigger.from_config(
        {
            "name": "t",
            "source": "outlook",
            "event": "new_mail",
            "once_per_thread": True,
        }
    )
    engine, fake, _ = _engine([t], handler, on_event=recs.append)
    engine.start()
    fake.fire("new_mail", {"subject": "ticket", "conversation_id": "C1"})  # fires
    fake.fire("new_mail", {"subject": "re: ticket", "conversation_id": "C1"})  # skip
    fake.fire("new_mail", {"subject": "other", "conversation_id": "C2"})  # new thread

    assert fired == ["ticket", "other"]
    # The reply is audited as deduped (matched but a once_per_thread repeat), not
    # as a non-match — so the feed still shows it was seen and why it didn't run.
    deduped = [r["summary"] for r in recs if r["deduped"]]
    assert deduped == ["re: ticket"]


def test_once_per_thread_falls_open_without_a_thread_id():
    # No conversation/thread id in the payload → we can't tell a reply from a new
    # message, so the trigger fires every time (fail open, not silently dead).
    fired: list[str] = []

    def handler(card, trigger, event):
        fired.append(event.payload.get("subject", ""))
        card.move(Column.DONE)

    t = Trigger.from_config(
        {"name": "t", "source": "outlook", "event": "new_mail", "once_per_thread": True}
    )
    engine, fake, _ = _engine([t], handler)
    engine.start()
    fake.fire("new_mail", {"subject": "a"})
    fake.fire("new_mail", {"subject": "b"})

    assert fired == ["a", "b"]


def test_once_per_thread_inherits_mailbox_default():
    # A trigger that doesn't set once_per_thread inherits the engine default (the
    # [mailbox] setting). With the default on, replies in a thread are suppressed.
    fired: list[str] = []

    def handler(card, trigger, event):
        fired.append(event.payload.get("subject", ""))
        card.move(Column.DONE)

    t = Trigger.from_config({"name": "t", "source": "outlook", "event": "new_mail"})
    assert t.once_per_thread is None  # unset → inherits
    engine, fake, _ = _engine([t], handler, default_once_per_thread=True)
    engine.start()
    fake.fire("new_mail", {"subject": "open", "conversation_id": "C1"})
    fake.fire("new_mail", {"subject": "reply", "conversation_id": "C1"})

    assert fired == ["open"]


def test_per_trigger_false_overrides_mailbox_default():
    # An explicit once_per_thread=false overrides a mailbox default of on, so the
    # trigger fires on every message in a thread (the per-trigger escape hatch).
    fired: list[str] = []

    def handler(card, trigger, event):
        fired.append(event.payload.get("subject", ""))
        card.move(Column.DONE)

    t = Trigger.from_config(
        {
            "name": "t",
            "source": "outlook",
            "event": "new_mail",
            "once_per_thread": False,
        }
    )
    assert t.once_per_thread is False
    engine, fake, _ = _engine([t], handler, default_once_per_thread=True)
    engine.start()
    fake.fire("new_mail", {"subject": "open", "conversation_id": "C1"})
    fake.fire("new_mail", {"subject": "reply", "conversation_id": "C1"})

    assert fired == ["open", "reply"]


def test_once_per_thread_is_per_trigger():
    # Two once_per_thread triggers on the same event each track their own threads,
    # so both fire for a conversation's opening message.
    fired: list[str] = []

    def handler(card, trigger, event):
        fired.append(trigger.name)
        card.move(Column.DONE)

    a = Trigger.from_config(
        {"name": "a", "source": "outlook", "event": "new_mail", "once_per_thread": True}
    )
    b = Trigger.from_config(
        {"name": "b", "source": "outlook", "event": "new_mail", "once_per_thread": True}
    )
    engine, fake, _ = _engine([a, b], handler)
    engine.start()
    fake.fire("new_mail", {"conversation_id": "C1"})
    fake.fire("new_mail", {"conversation_id": "C1"})  # both skip the reply

    assert sorted(fired) == ["a", "b"]


def test_engine_feed_gate_suppresses_routing_and_audit():
    # A feed_gate that rejects an event drops it entirely — no handler run, and
    # no audit record (parity with the chat claim gate, where an unclaimed
    # message never reaches the engine).
    fired: list[str] = []
    recs: list[dict] = []

    def handler(card, trigger, event):
        fired.append(trigger.name)
        card.move(Column.DONE)

    t = Trigger.from_config({"name": "t", "source": "outlook", "event": "new_mail"})
    engine, fake, _ = _engine(
        [t],
        handler,
        on_event=recs.append,
        feed_gate=lambda e: bool(e.payload.get("allow")),
    )
    engine.start()
    fake.fire("new_mail", {"subject": "blocked"})  # gate rejects
    fake.fire("new_mail", {"subject": "ok", "allow": True})  # gate passes

    assert fired == ["t"]
    assert [r["summary"] for r in recs] == ["ok"]  # rejected event left no record


def test_engine_feed_gate_runs_source_hook_before_suppressing():
    # The source-event hook (queue-card fold) still runs for a gated event, so an
    # unclaimed thread surfaces a card even though it isn't routed to triggers.
    seen: list[str] = []
    fired: list[str] = []

    def handler(card, trigger, event):
        fired.append(trigger.name)
        card.move(Column.DONE)

    t = Trigger.from_config({"name": "t", "source": "outlook", "event": "new_mail"})
    engine, fake, _ = _engine(
        [t],
        handler,
        on_source_event=lambda e: seen.append(e.payload.get("subject", "")),
        feed_gate=lambda e: False,
    )
    engine.start()
    fake.fire("new_mail", {"subject": "x"})

    assert seen == ["x"]  # hook ran (the card surfaces)
    assert fired == []  # but routing was suppressed


def test_engine_feed_gate_failure_routes():
    # A raising gate must never block routing (fail open).
    fired: list[str] = []

    def handler(card, trigger, event):
        fired.append(trigger.name)
        card.move(Column.DONE)

    def boom(event):
        raise RuntimeError("gate broke")

    t = Trigger.from_config({"name": "t", "source": "outlook", "event": "new_mail"})
    engine, fake, _ = _engine([t], handler, feed_gate=boom)
    engine.start()
    fake.fire("new_mail", {"subject": "x"})

    assert fired == ["t"]


def test_engine_would_match():
    # would_match answers "is this triggered?" without dispatching — the email
    # channel surfaces a ticket only for mail a trigger matches.
    t = Trigger.from_config(
        {
            "name": "vip",
            "source": "outlook",
            "event": "new_mail",
            "match": {"subject": "(?i)urgent"},
        }
    )
    engine = TriggerEngine([t], handler=lambda *a: None)
    assert engine.would_match(_mail(subject="URGENT: down")) is True
    assert engine.would_match(_mail(subject="lunch?")) is False
    assert engine.would_match(Event.new("chat", "message", {"text": "x"})) is False


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


# ── declarative multi-runnable actions + pre-turn (Phase 2/3) ──────────────────


def test_actions_parse_and_render():
    t = Trigger.from_config(
        {
            "name": "seq",
            "source": "schedule",
            "event": "nightly",
            "mode": "rule",
            "action_mode": "parallel",
            "actions": [
                {"run": "web01__check-memory", "map": {"svc": "{{ subject }}"}},
                {"run": "db01__check-disk"},
            ],
        }
    )
    assert t.action_mode == "parallel"
    assert [s.run for s in t.actions] == ["web01__check-memory", "db01__check-disk"]
    rendered = t.render_action_args(t.actions[0], {"subject": "web"})
    assert rendered == {"svc": "web"}


def test_actions_dotted_llm_namespace():
    t = Trigger.from_config(
        {
            "name": "p",
            "source": "schedule",
            "event": "a",
            "mode": "rule",
            "actions": [{"run": "x__y", "map": {"h": "{{ _llm.host }}"}}],
        }
    )
    assert t.render_action_args(t.actions[0], {"_llm": {"host": "web9"}}) == {
        "h": "web9"
    }
    # Missing namespace resolves to empty, never errors.
    assert t.render_action_args(t.actions[0], {}) == {"h": ""}


def test_actions_default_mode_is_sequential():
    t = Trigger.from_config(
        {
            "name": "d",
            "source": "schedule",
            "event": "a",
            "mode": "rule",
            "actions": [{"run": "x__y"}],
        }
    )
    assert t.action_mode == "sequential"


def test_actions_unknown_mode_rejected():
    import pytest

    with pytest.raises(ValueError, match="action_mode"):
        Trigger.from_config(
            {
                "name": "d",
                "source": "schedule",
                "event": "a",
                "mode": "rule",
                "action_mode": "fanout",
                "actions": [{"run": "x__y"}],
            }
        )


def test_actions_and_run_are_mutually_exclusive():
    import pytest

    with pytest.raises(ValueError, match="either 'run'"):
        Trigger.from_config(
            {
                "name": "d",
                "source": "schedule",
                "event": "a",
                "mode": "rule",
                "run": "x__y",
                "actions": [{"run": "a__b"}],
            }
        )


def test_actions_rejected_in_agent_mode():
    import pytest

    with pytest.raises(ValueError, match="rule-mode declarative list"):
        Trigger.from_config(
            {
                "name": "d",
                "source": "schedule",
                "event": "a",
                "mode": "agent",
                "actions": [{"run": "x__y"}],
            }
        )


def test_actions_step_requires_run():
    import pytest

    with pytest.raises(ValueError, match="missing 'run'"):
        Trigger.from_config(
            {
                "name": "d",
                "source": "schedule",
                "event": "a",
                "mode": "rule",
                "actions": [{"map": {"a": "b"}}],
            }
        )


def test_actions_length_capped():
    import pytest

    with pytest.raises(ValueError, match="too many actions"):
        Trigger.from_config(
            {
                "name": "d",
                "source": "schedule",
                "event": "a",
                "mode": "rule",
                "actions": [{"run": f"h__r{i}"} for i in range(11)],
            }
        )


def test_rule_mode_needs_run_or_actions():
    import pytest

    with pytest.raises(ValueError, match="no 'run' or 'actions'"):
        Trigger.from_config(
            {"name": "d", "source": "schedule", "event": "a", "mode": "rule"}
        )


def test_action_allow_parsed():
    t = Trigger.from_config(
        {
            "name": "a",
            "source": "outlook",
            "event": "new_mail",
            "mode": "agent",
            "action_allow": ["create_document", "outlook_draft", ""],
        }
    )
    assert t.action_allow == ["create_document", "outlook_draft"]
