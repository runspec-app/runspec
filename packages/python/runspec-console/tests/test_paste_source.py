"""
test_paste_source.py — the operator-driven "paste a block of text" source.

Three seams:
  - the ``paste`` source registers and reports itself available (so it shows up
    in the Triggers editor's source dropdown, with ``text`` as its event type),
  - ``TriggerEngine.fire_paste`` dispatches exactly the named trigger through its
    real gates (match / rota / dedup) — a real run, unlike the "Trigger now" test
    which bypasses them — while bypassing the lock gate (a human is present),
  - ``Bridge.submit_paste_input`` builds the ``paste``/``text`` event (mirroring
    the input into ``text`` + ``body``) and only accepts a paste-source trigger.

The engine tests use a synchronous executor so routing is deterministic; the
bridge method is bound onto a bare stub (à la test_schedule_bridge) to dodge
``Bridge.__init__``.
"""

from __future__ import annotations

import threading
import types
from concurrent.futures import Executor, Future
from typing import Any, Callable

import runspec_console.sources  # noqa: F401 — registers the built-in sources
from runspec_console.bridge import Bridge
from runspec_console.events import Event, available_sources, create_source
from runspec_console.triggers import Column, Trigger, TriggerEngine


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


def _paste_trigger(**over: Any) -> Trigger:
    data: dict[str, Any] = {"name": "triage", "source": "paste", "event": "text"}
    data.update(over)
    return Trigger.from_config(data)


def _engine(triggers: list[Trigger], handler, **kw: Any) -> TriggerEngine:
    return TriggerEngine(
        triggers,
        handler=handler,
        executor=InlineExecutor(),
        # The paste source is inert/external — the engine never starts it.
        external_sources={"paste"},
        **kw,
    )


# ── source registration ───────────────────────────────────────────────────────


def test_paste_source_registered_and_available():
    assert "paste" in available_sources()
    src = create_source("paste")
    assert src is not None
    assert src.available() == (True, "")
    assert src.event_types() == ["text"]


def test_paste_source_start_stop_are_inert():
    src = create_source("paste")
    emitted: list[Event] = []
    src.start(emitted.append)  # never emits on its own
    src.stop()
    assert emitted == []


# ── engine.fire_paste ─────────────────────────────────────────────────────────


def test_fire_paste_dispatches_named_trigger_with_text_and_body():
    fired: list[tuple[str, dict]] = []

    def handler(card, trigger, event):
        fired.append((trigger.name, event.payload))
        card.move(Column.DONE)

    other = _paste_trigger(name="other")
    engine = _engine([_paste_trigger(), other], handler)
    ev = Event.new("paste", "text", {"text": "log dump", "body": "log dump"})

    result = engine.fire_paste("triage", ev)

    assert result == {"ok": True, "skipped": None}
    assert [name for name, _ in fired] == ["triage"]  # only the named one
    assert fired[0][1]["text"] == "log dump"


def test_fire_paste_unknown_trigger_is_a_no_op():
    engine = _engine([_paste_trigger()], lambda *a: None)
    result = engine.fire_paste("nope", Event.new("paste", "text", {"text": "x"}))
    assert result["ok"] is False
    assert "No trigger named" in result["skipped"]


def test_fire_paste_respects_match_filter():
    fired: list[str] = []
    recs: list[dict] = []

    def handler(card, trigger, event):
        fired.append(trigger.name)
        card.move(Column.DONE)

    t = _paste_trigger(match={"text": "(?i)urgent"})
    engine = _engine([t], handler, on_event=recs.append)

    miss = engine.fire_paste("triage", Event.new("paste", "text", {"text": "hello"}))
    assert miss["ok"] is False and "match" in miss["skipped"].lower()
    assert fired == []
    assert recs[-1]["matched"] == []

    hit = engine.fire_paste(
        "triage", Event.new("paste", "text", {"text": "URGENT: down"})
    )
    assert hit["ok"] is True
    assert fired == ["triage"]


def test_fire_paste_respects_rota_unless_ignore():
    fired: list[str] = []
    recs: list[dict] = []

    def handler(card, trigger, event):
        fired.append(trigger.name)
        card.move(Column.DONE)

    gated = _paste_trigger(name="gated")
    always = _paste_trigger(name="always", ignore_rota=True)
    engine = _engine(
        [gated, always], handler, on_event=recs.append, clock_ok=lambda: False
    )

    ev = lambda name: Event.new("paste", "text", {"text": "x"})  # noqa: E731
    blocked = engine.fire_paste("gated", ev("gated"))
    assert blocked["ok"] is False and "rota" in blocked["skipped"].lower()
    assert fired == []
    assert recs[-1]["skipped"] == ["gated"]

    ran = engine.fire_paste("always", ev("always"))
    assert ran["ok"] is True  # ignore_rota bypasses the working-hours gate
    assert fired == ["always"]


def test_fire_paste_bypasses_lock_gate():
    # A human actively pasting is present by definition — the lock gate (which
    # suppresses *reactive* triggers while the screen is locked) must not block.
    fired: list[str] = []

    def handler(card, trigger, event):
        fired.append(trigger.name)
        card.move(Column.DONE)

    engine = _engine([_paste_trigger()], handler, lock_ok=lambda: False)
    result = engine.fire_paste("triage", Event.new("paste", "text", {"text": "x"}))
    assert result["ok"] is True
    assert fired == ["triage"]


def test_fire_paste_once_per_thread_dedup():
    fired: list[str] = []

    def handler(card, trigger, event):
        fired.append(trigger.name)
        card.move(Column.DONE)

    t = _paste_trigger(once_per_thread=True)
    engine = _engine([t], handler)

    first = engine.fire_paste(
        "triage", Event.new("paste", "text", {"text": "a", "conversation_id": "c1"})
    )
    assert first["ok"] is True
    second = engine.fire_paste(
        "triage", Event.new("paste", "text", {"text": "b", "conversation_id": "c1"})
    )
    assert second["ok"] is False and "thread" in second["skipped"].lower()
    assert fired == ["triage"]  # only the first fired


# ── bridge.submit_paste_input ─────────────────────────────────────────────────


class _FakeEngine:
    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[tuple[str, Event]] = []
        self._result = result or {"ok": True, "skipped": None}

    def fire_paste(self, name: str, event: Event) -> dict:
        self.calls.append((name, event))
        return self._result


def _bridge_stub(monkeypatch, triggers: list[dict], *, engine=None):
    ns = types.SimpleNamespace()
    ns.submit_paste_input = types.MethodType(Bridge.submit_paste_input, ns)
    ns._trigger_engine = engine
    ns._lock = threading.Lock()
    monkeypatch.setattr("runspec_console.config.read_triggers", lambda: triggers)
    return ns


def test_submit_paste_input_builds_event_and_dispatches(monkeypatch):
    engine = _FakeEngine()
    ns = _bridge_stub(
        monkeypatch,
        [{"name": "triage", "source": "paste", "event": "text"}],
        engine=engine,
    )
    out = ns.submit_paste_input("triage", "big block of text", {"host": "web-01"})
    assert out == {"ok": True, "skipped": None}
    ((name, event),) = engine.calls
    assert name == "triage"
    assert event.source == "paste" and event.type == "text"
    assert event.payload["text"] == "big block of text"
    assert event.payload["body"] == "big block of text"  # mirrored for render_event
    assert event.payload["host"] == "web-01"  # extra field passed through


def test_submit_paste_input_requires_engine(monkeypatch):
    ns = _bridge_stub(
        monkeypatch,
        [{"name": "triage", "source": "paste", "event": "text"}],
        engine=None,
    )
    out = ns.submit_paste_input("triage", "x")
    assert out["ok"] is False and "isn't running" in out["error"]


def test_submit_paste_input_unknown_trigger(monkeypatch):
    ns = _bridge_stub(monkeypatch, [], engine=_FakeEngine())
    out = ns.submit_paste_input("ghost", "x")
    assert out["ok"] is False and "No trigger named" in out["error"]


def test_submit_paste_input_rejects_non_paste_source(monkeypatch):
    engine = _FakeEngine()
    ns = _bridge_stub(
        monkeypatch,
        [{"name": "mail", "source": "outlook", "event": "new_mail"}],
        engine=engine,
    )
    out = ns.submit_paste_input("mail", "x")
    assert out["ok"] is False and "paste" in out["error"]
    assert engine.calls == []  # never dispatched
