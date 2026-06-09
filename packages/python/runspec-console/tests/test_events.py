"""
Tests for the event-source layer (events.py).

Pure logic — no COM, no pywebview, no threads — so this runs on any platform.
"""

from __future__ import annotations

import time

import pytest

from runspec_console.events import (
    Event,
    EventSource,
    FakeSource,
    available_sources,
    create_source,
    register_source,
)


# ── Event ───────────────────────────────────────────────────────────────────


def test_event_new_fills_id_and_timestamp():
    before = time.time()
    e = Event.new("outlook", "new_mail", {"subject": "hi"})
    assert e.source == "outlook"
    assert e.type == "new_mail"
    assert e.payload == {"subject": "hi"}
    assert e.id  # non-empty random id
    assert e.received_at >= before


def test_event_new_ids_are_unique():
    a = Event.new("s", "t")
    b = Event.new("s", "t")
    assert a.id != b.id


def test_event_new_defaults_empty_payload():
    e = Event.new("s", "t")
    assert e.payload == {}


def test_event_is_frozen():
    e = Event.new("s", "t")
    with pytest.raises(Exception):
        e.type = "other"  # type: ignore[misc]


# ── registry ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    """Snapshot and restore the module registry so tests don't leak into each
    other (or into the real source list)."""
    from runspec_console import events

    saved = dict(events._REGISTRY)
    try:
        yield
    finally:
        events._REGISTRY.clear()
        events._REGISTRY.update(saved)


def test_register_and_create():
    register_source("fake", FakeSource)
    src = create_source("fake")
    assert isinstance(src, FakeSource)
    assert src.id == "fake"


def test_create_unknown_returns_none():
    assert create_source("nope") is None


def test_create_returns_fresh_instances():
    register_source("fake", FakeSource)
    assert create_source("fake") is not create_source("fake")


def test_available_sources_sorted():
    register_source("zeta", FakeSource)
    register_source("alpha", FakeSource)
    assert available_sources() == sorted(available_sources())
    assert {"alpha", "zeta"} <= set(available_sources())


def test_register_replaces():
    register_source("fake", lambda: FakeSource(id="first"))
    register_source("fake", lambda: FakeSource(id="second"))
    src = create_source("fake")
    assert src is not None and src.id == "second"


# ── FakeSource / protocol conformance ───────────────────────────────────────


def test_fakesource_satisfies_protocol():
    assert isinstance(FakeSource(), EventSource)


def test_fakesource_lifecycle_and_fire():
    received: list[Event] = []
    src = FakeSource(types=["new_mail"])

    assert src.available() == (True, "")
    assert src.event_types() == ["new_mail"]

    src.start(received.append)
    assert src.started

    event = src.fire("new_mail", {"subject": "outage"})
    assert received == [event]
    assert event.source == "fake"
    assert event.payload == {"subject": "outage"}

    src.stop()
    assert src.stopped


def test_fakesource_fire_before_start_raises():
    src = FakeSource()
    with pytest.raises(RuntimeError):
        src.fire("test")


def test_fakesource_fire_after_stop_raises():
    src = FakeSource()
    src.start(lambda _e: None)
    src.stop()
    with pytest.raises(RuntimeError):
        src.fire("test")
