"""
events.py — the event-source layer behind runspec-console triggers.

A *trigger* is the symmetric half of a runnable: a runnable declares how it is
called; a trigger declares what causes it to run. The thing that produces those
causes is an **event source** — Outlook (new mail / reminders), the Windows
notification listener, a filesystem watch, a remote log stream over SSH, …

This module is deliberately pure and platform-independent: it defines the
``Event`` value, the ``EventSource`` protocol every source implements, a small
name→factory registry (mirroring ``adapters/base.register_adapter``), and a
``FakeSource`` so the engine and triggers can be tested without touching COM.
Concrete sources (``sources/outlook.py``, …) live in their own modules and
register themselves; the heavy / OS-specific imports stay there.

See docs/design/console-triggers.md.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable


@dataclass(frozen=True)
class Event:
    """A single thing that happened, emitted by an ``EventSource``.

    ``payload`` is source-defined and documented per source (e.g. Outlook
    ``new_mail`` carries ``from``/``subject``/``body``/…). ``id`` and
    ``received_at`` identify the occurrence; use :meth:`new` to fill them.
    """

    source: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    received_at: float = 0.0

    @classmethod
    def new(
        cls, source: str, type: str, payload: dict[str, Any] | None = None
    ) -> Event:
        """Create an event, filling ``id`` (random) and ``received_at`` (now)."""
        return cls(
            source=source,
            type=type,
            payload=payload or {},
            id=uuid.uuid4().hex,
            received_at=time.time(),
        )


# A source pushes events by calling this. It must be safe to call from whatever
# thread the source runs on (e.g. the Outlook COM pump thread) — the engine's
# implementation is responsible for hopping back to wherever it needs to be.
EmitFn = Callable[[Event], None]


@runtime_checkable
class EventSource(Protocol):
    """A producer of :class:`Event` objects.

    Sources are long-lived: :meth:`start` begins pushing events through the
    supplied ``emit`` callback and returns immediately (any blocking loop —
    e.g. a COM message pump — runs on the source's own thread); :meth:`stop`
    tears that down. The engine holds at most one started instance per ``id``
    and shares it across every trigger bound to that source.
    """

    #: Stable identifier used in trigger config (``source = "outlook"``).
    id: str

    def available(self) -> tuple[bool, str]:
        """``(True, "")`` if the source can run here, else ``(False, reason)``.

        The reason is shown in the UI so a disabled source explains itself
        ("not on Windows", "Outlook not installed", "notification access not
        granted") instead of silently vanishing.
        """
        ...

    def event_types(self) -> list[str]:
        """Event ``type`` strings this source can emit (for trigger config/UI)."""
        ...

    def start(self, emit: EmitFn) -> None:
        """Begin pushing events through ``emit``. Returns immediately."""
        ...

    def stop(self) -> None:
        """Stop pushing events and release resources. Idempotent."""
        ...


# ── source registry ───────────────────────────────────────────────────────────
#
# Mirrors adapters/base.register_adapter: concrete sources register a zero-arg
# factory under their id, and the engine instantiates (and caches) one per id.
# Factories are lazy so importing this module never drags in pywin32 / winrt —
# a source's OS-specific imports happen inside its factory / __init__.
#
# (Entry-point discovery — like the adapters' `runspec_console.adapters` group —
# can be added when sources start shipping as separate plugin distributions; it
# isn't needed while every source lives in this package.)

SourceFactory = Callable[[], EventSource]

_REGISTRY: dict[str, SourceFactory] = {}


def register_source(name: str, factory: SourceFactory) -> None:
    """Register a source factory under its id. A later call replaces an earlier."""
    _REGISTRY[name] = factory


def create_source(name: str) -> EventSource | None:
    """Instantiate the source registered under ``name`` (``None`` if unknown).

    Each call constructs a fresh instance; the engine is responsible for
    keeping a single started instance per id.
    """
    factory = _REGISTRY.get(name)
    return factory() if factory is not None else None


def available_sources() -> list[str]:
    """All registered source ids, sorted."""
    return sorted(_REGISTRY)


# ── FakeSource — for tests and local development ────────────────────────────────
#
# Not registered by default (it would clutter the real source list); tests that
# want it call register_source("fake", FakeSource).


class FakeSource:
    """An in-memory source whose events are pushed by the test, not the OS.

    Satisfies the ``EventSource`` protocol. Call :meth:`fire` after
    :meth:`start` to deliver an event synchronously through the engine's
    ``emit`` callback — the whole trigger path can be exercised with no COM,
    no threads, and no platform dependency.
    """

    def __init__(self, id: str = "fake", types: list[str] | None = None) -> None:
        self.id = id
        self._types = types or ["test"]
        self._emit: EmitFn | None = None
        self.started = False
        self.stopped = False

    def available(self) -> tuple[bool, str]:
        return (True, "")

    def event_types(self) -> list[str]:
        return list(self._types)

    def start(self, emit: EmitFn) -> None:
        self._emit = emit
        self.started = True

    def stop(self) -> None:
        self._emit = None
        self.stopped = True

    def fire(self, type: str, payload: dict[str, Any] | None = None) -> Event:
        """Emit an event now. Raises if the source has not been started."""
        if self._emit is None:
            raise RuntimeError("FakeSource.fire() called before start()")
        event = Event.new(self.id, type, payload)
        self._emit(event)
        return event
