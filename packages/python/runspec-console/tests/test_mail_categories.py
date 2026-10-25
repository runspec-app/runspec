"""
Tests for Outlook lifecycle categories: the pure planner (mail_categories.py),
the engine's on_stage hook (triggers.py), the runner's needs-a-person flag
(trigger_runner.py), the bridge wiring (bridge_mail_categories.py, claim_email)
and the pure COM helpers in sources/outlook.py. No COM — Outlook calls are faked.
"""

from __future__ import annotations

from concurrent.futures import Executor, Future
from typing import Any, Callable

import pytest

from runspec_console import config as config_mod
from runspec_console import mail_categories as mc
from runspec_console.bridge_mail_categories import MailCategoriesMixin
from runspec_console.bridge_triggers import TriggersMixin
from runspec_console.events import Event, FakeSource
from runspec_console.sources import outlook as ol
from runspec_console.trigger_runner import needs_person
from runspec_console.triggers import Column, Trigger, TriggerEngine, outcome_stage


class InlineExecutor(Executor):
    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        f: Future = Future()
        f.set_result(fn(*args, **kwargs))
        return f

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        pass


# ── pure planner ──────────────────────────────────────────────────────────────


def test_read_settings_defaults_and_cleaning():
    assert mc.read_settings(None) == {
        "enabled": False,
        "prefix": "runspec",
        "colors": True,
    }
    s = mc.read_settings({"enabled": True, "prefix": " Ops, desk ", "colors": False})
    assert s == {"enabled": True, "prefix": "Ops desk", "colors": False}
    assert mc.read_settings({"prefix": "  "})["prefix"] == "runspec"


def test_category_names_never_contain_separators():
    assert mc.trigger_category("runspec", "disk, alerts;x") == "runspec: disk alerts x"
    assert mc.category("rs", "done") == "rs: done"


def test_stage_change_per_stage():
    tag = "runspec: vip"
    m = mc.stage_change(mc.MATCHED, "runspec", "vip")
    assert m.add == (tag,) and not m.status and m.colors == {tag: mc.COLOR_BLUE}

    a = mc.stage_change(mc.AWAITING_CLAIM, "runspec", "vip")
    assert a.add == (tag,) and a.status == "runspec: awaiting claim"

    d = mc.stage_change(mc.DONE, "runspec", "vip")
    assert d.status == "runspec: done" and not d.weak and not d.add

    s = mc.stage_change(mc.SKIPPED, "runspec", "vip")
    assert s.status == "runspec: skipped" and s.weak

    r = mc.stage_change(mc.REPLY_DRAFTED, "runspec", "vip")
    assert r.add == ("runspec: reply drafted",) and not r.status

    assert mc.stage_change(mc.NEEDS_PERSON, "runspec", "vip").status == (
        "runspec: needs a person"
    )
    assert mc.stage_change("nonsense", "runspec", "vip").empty


def test_merge_status_replaces_and_keeps_others():
    statuses = mc.status_categories("runspec")
    out = mc.merge(
        "Red, runspec: awaiting claim, runspec: vip",
        status="runspec: done",
        statuses=statuses,
    )
    assert out == "Red, runspec: vip, runspec: done"


def test_merge_weak_status_never_overwrites():
    statuses = mc.status_categories("runspec")
    kept = mc.merge(
        "runspec: done", status="runspec: skipped", statuses=statuses, weak=True
    )
    assert kept == "runspec: done"
    # With no status yet, a weak status is set.
    assert (
        mc.merge("Blue", status="runspec: skipped", statuses=statuses, weak=True)
        == "Blue, runspec: skipped"
    )
    # And a strong status later replaces it.
    assert (
        mc.merge("runspec: skipped", status="runspec: done", statuses=statuses)
        == "runspec: done"
    )


def test_merge_add_dedupes_case_insensitively_and_removes():
    out = mc.merge(
        "runspec: VIP; Red",
        add=["runspec: vip", "Claimed: Ana"],
        remove=["red"],
    )
    assert out == "runspec: VIP, Claimed: Ana"


# ── engine on_stage hook ──────────────────────────────────────────────────────


def _engine(triggers, handler, **kw):
    fake = FakeSource(id="outlook", types=["new_mail"])
    stages: list[tuple[str, str]] = []
    engine = TriggerEngine(
        triggers,
        handler=handler,
        create_source=lambda name: fake if name == "outlook" else None,
        executor=InlineExecutor(),
        on_stage=lambda st, t, e, c: stages.append((st, t.name)),
        **kw,
    )
    engine.start()
    return engine, fake, stages


def _trig(**extra) -> Trigger:
    return Trigger.from_config(
        {"name": "vip", "source": "outlook", "event": "new_mail", **extra}
    )


def test_engine_reports_matched_then_done():
    engine, fake, stages = _engine([_trig()], lambda c, t, e: None)
    fake.fire("new_mail", {"entry_id": "E1"})
    assert stages == [("matched", "vip"), ("done", "vip")]


def test_engine_reports_failed_on_exception():
    def boom(card, trigger, event):
        raise RuntimeError("nope")

    engine, fake, stages = _engine([_trig()], boom)
    fake.fire("new_mail", {"entry_id": "E1"})
    assert stages[-1] == ("failed", "vip")


def test_engine_reports_needs_person_from_card_flag():
    def handler(card, trigger, event):
        card.move(Column.DONE, needs_person=True)

    engine, fake, stages = _engine([_trig()], handler)
    fake.fire("new_mail", {"entry_id": "E1"})
    assert stages[-1] == ("needs_person", "vip")


def test_engine_reports_skipped_outside_rota():
    engine, fake, stages = _engine(
        [_trig()], lambda c, t, e: None, clock_ok=lambda: False
    )
    fake.fire("new_mail", {"entry_id": "E1"})
    assert stages == [("skipped", "vip")]


def test_engine_claim_trigger_reports_awaiting_then_done_on_resume():
    t = _trig(claim=True, claim_folder="Claimed", match={"folder": "^Open$"})
    engine, fake, stages = _engine([t], lambda c, tr, e: None)
    fake.fire("new_mail", {"entry_id": "E1", "folder": "Open"})
    assert stages == [("awaiting_claim", "vip")]
    engine.resume_claim(engine.parked_claim_ids()[0])
    assert stages[-1] == ("done", "vip")


def test_fire_test_reports_no_stages():
    engine, fake, stages = _engine([_trig()], lambda c, t, e: None)
    engine.fire_test("vip", Event.new("outlook", "new_mail", {"entry_id": "E1"}))
    assert stages == []


def test_outcome_stage():
    from runspec_console.triggers import TriggerCard

    card = TriggerCard(id="c", trigger="t", source="s", event_type="e", summary="")
    card.move(Column.DONE)
    assert outcome_stage(card) == "done"
    card.detail["needs_person"] = True
    assert outcome_stage(card) == "needs_person"
    card.move(Column.FAILED)
    assert outcome_stage(card) == "failed"


def test_trigger_categories_tristate():
    assert _trig().categories is None
    assert _trig(categories=True).categories is True
    assert _trig(categories=False).categories is False


# ── runner: needs a person ────────────────────────────────────────────────────


def test_needs_person_markers():
    assert needs_person("✗ The user declined to run 'restart' (autonomy = confirm).")
    assert needs_person("restart → ✗ The user declined to run 'restart'")
    assert needs_person("✗ You declined to save the draft reply.")
    assert needs_person(
        "✗ 'wipe' is manual-only (autonomy = manual) and cannot be run by the assistant."
    )
    assert needs_person("✗ 'wipe' is manual-only — the batch was not run (…).")
    assert not needs_person("disk usage 80%")
    assert not needs_person("the customer declined to renew")  # no gate marker


# ── bridge wiring ─────────────────────────────────────────────────────────────


class _Stub(TriggersMixin, MailCategoriesMixin):
    pass


@pytest.fixture
def bridge(monkeypatch):
    b = _Stub.__new__(_Stub)
    b._mail_cat_pool = InlineExecutor()  # type: ignore[assignment]
    b._mail_cat_ctx = {}
    writes: list[dict[str, Any]] = []

    def fake_update(entry_id, **kw):
        writes.append({"entry_id": entry_id, **kw})
        return {"changed": True, "categories": []}

    monkeypatch.setattr(ol, "outlook_available", lambda: True)
    monkeypatch.setattr(ol, "update_categories", fake_update)
    settings = {"enabled": True, "prefix": "runspec", "colors": True}
    monkeypatch.setattr(config_mod, "read_mail_categories", lambda: dict(settings))
    b.writes = writes  # type: ignore[attr-defined]
    b.settings = settings  # type: ignore[attr-defined]
    return b


def _mail(**payload) -> Event:
    return Event.new("outlook", "new_mail", payload)


def test_bridge_writes_stage_change(bridge):
    bridge._on_trigger_stage("done", _trig(), _mail(entry_id="E1"), None)
    (w,) = bridge.writes
    assert w["entry_id"] == "E1"
    assert w["status"] == "runspec: done"
    assert "runspec: awaiting claim" in w["statuses"]
    assert w["colors"] == {"runspec: done": mc.COLOR_GREEN}


def test_bridge_respects_setting_and_trigger_override(bridge):
    bridge.settings["enabled"] = False
    bridge._on_trigger_stage("done", _trig(), _mail(entry_id="E1"), None)
    assert bridge.writes == []
    # The trigger's own categories = true wins over the setting.
    bridge._on_trigger_stage("done", _trig(categories=True), _mail(entry_id="E1"), None)
    assert len(bridge.writes) == 1
    # And categories = false wins over an enabled setting.
    bridge.settings["enabled"] = True
    bridge._on_trigger_stage(
        "done", _trig(categories=False), _mail(entry_id="E1"), None
    )
    assert len(bridge.writes) == 1


def test_bridge_skips_non_mail_and_missing_entry_id(bridge):
    bridge._on_trigger_stage("done", _trig(), _mail(), None)
    bridge._on_trigger_stage(
        "done", _trig(), Event.new("outlook", "reminder", {"entry_id": "R"}), None
    )
    assert bridge.writes == []


def test_bridge_colors_off(bridge):
    bridge.settings["colors"] = False
    bridge._on_trigger_stage("done", _trig(), _mail(entry_id="E1"), None)
    assert bridge.writes[0]["colors"] is None


def test_bridge_reply_drafted_uses_card_context(bridge):
    from runspec_console.triggers import TriggerCard

    card = TriggerCard(
        id="c1", trigger="vip", source="outlook", event_type="new_mail", summary=""
    )
    ev = _mail(entry_id="E1")
    bridge._on_trigger_stage("matched", _trig(), ev, card)
    bridge._mail_stage_for_card("c1", "reply_drafted")
    assert bridge.writes[-1]["add"] == ("runspec: reply drafted",)
    # Unknown card → nothing.
    bridge._mail_stage_for_card("nope", "reply_drafted")
    assert len(bridge.writes) == 2


def test_bridge_write_failure_is_swallowed(bridge, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("item not found")

    monkeypatch.setattr(ol, "update_categories", boom)
    bridge._on_trigger_stage("done", _trig(), _mail(entry_id="E1"), None)  # no raise


def test_claim_email_names_claimer_drops_status_and_repoints_entry_id(
    bridge, monkeypatch
):
    t = _trig(claim=True, claim_folder="Claimed", match={"folder": "^Open$"})
    fake = FakeSource(id="outlook", types=["new_mail"])
    ran: list[str] = []
    engine = TriggerEngine(
        [t],
        handler=lambda c, tr, e: ran.append(e.payload["entry_id"]),
        create_source=lambda name: fake,
        executor=InlineExecutor(),
    )
    engine.start()
    fake.fire("new_mail", {"entry_id": "OLD", "folder": "Open"})
    card_id = engine.parked_claim_ids()[0]
    bridge._trigger_engine = engine

    seen: dict[str, Any] = {}

    def fake_claim(entry_id, owner, **kw):
        seen.update(entry_id=entry_id, owner=owner, **kw)
        return {"claimed": True, "owner": owner, "entry_id": "NEW"}

    monkeypatch.setattr(ol, "claim_message", fake_claim)
    monkeypatch.setattr(ol, "outlook_user_name", lambda: "Ana Ruiz")

    res = bridge.claim_email(card_id)
    assert res == {"claimed": True, "owner": "Ana Ruiz"}
    assert seen["owner"] == "Ana Ruiz"
    assert "runspec: awaiting claim" in seen["remove"]
    assert seen["colors"] == {"Claimed: Ana Ruiz": mc.CLAIMED_COLOR}
    # The action ran against the moved email.
    assert ran == ["NEW"]


def test_claim_operator_falls_back_to_login(monkeypatch):
    monkeypatch.setattr(ol, "outlook_user_name", lambda: "")
    import getpass

    monkeypatch.setattr(getpass, "getuser", lambda: "ana")
    assert TriggersMixin._claim_operator() == "ana"


# ── pure outlook helpers ──────────────────────────────────────────────────────


def test_natural_name():
    assert ol._natural_name("Ruiz, Ana") == "Ana Ruiz"
    assert ol._natural_name("Ana Ruiz") == "Ana Ruiz"
    assert ol._natural_name("a, b, c") == "a b c"


def test_claim_category_strips_commas():
    assert ol._claim_category("Ruiz, Ana") == "Claimed: Ruiz Ana"


class _Cat:
    def __init__(self, name):
        self.Name = name


class _Cats(list):
    def Add(self, name, color):  # noqa: N802 — COM name
        self.append(_Cat(name))
        self.added = getattr(self, "added", []) + [(name, color)]


def test_ensure_in_list_adds_missing_once(monkeypatch):
    monkeypatch.setattr(ol, "_ENSURED", {})
    cats = _Cats([_Cat("Runspec: Done")])
    want = {"runspec: done": 5, "runspec: failed": 1}
    ol._ensure_in_list(cats, "", want)
    assert cats.added == [("runspec: failed", 1)]
    # Second call is a no-op (cached), even if the list were re-read.
    ol._ensure_in_list(cats, "", want)
    assert cats.added == [("runspec: failed", 1)]
