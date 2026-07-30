"""
sources/paste.py — the operator-driven "paste a block of text" event source.

Every other source turns something *happening* (a new mail, a toast, a chat
message, the clock) into an :class:`~runspec_console.events.Event`. This one has
no autonomous stimulus at all: the operator pastes a block of text into the
Triggers tab and that *is* the event. A trigger declared ``source = "paste"``
then acts on it — agent mode receives the text as its user turn; rule mode
templates ``{{ text }}`` into a runnable's args.

Because it never fires on its own, the source itself is inert: ``start`` /
``stop`` are no-ops. Its driver is the bridge — the UI paste action calls
:meth:`~runspec_console.bridge_triggers.TriggersMixin.submit_paste_input`, which
builds the event and dispatches the chosen trigger through the engine's real
gate logic (``TriggerEngine.fire_paste``). So, like ``chat`` and ``schedule``,
it is **bridge-owned and marked external** on the engine (``external_sources``);
registering it here is purely what makes it show up in the source dropdown (with
``text`` autocompleting the event field).

The id is ``paste`` rather than ``manual`` on purpose: ``manual`` already names
the most-restrictive *autonomy* level, and a source of that name in the same UI
would be confusing.

See docs/design/console-triggers.md.
"""

from __future__ import annotations

from ..events import EmitFn, EventSource, register_source


class PasteSource:
    """An inert, operator-driven source (satisfies :class:`EventSource`).

    It emits nothing itself; the bridge feeds pasted text into the engine on
    demand. ``start`` / ``stop`` exist only to satisfy the protocol.
    """

    id = "paste"

    def available(self) -> tuple[bool, str]:
        return (True, "")

    def event_types(self) -> list[str]:
        return ["text"]

    def start(self, emit: EmitFn) -> None:  # noqa: ARG002 — inert by design
        """No-op: this source never emits on its own (the bridge drives it)."""

    def stop(self) -> None:
        """No-op: nothing to tear down."""


# Satisfy the EventSource Protocol at import time (documents intent; cheap).
_source: EventSource = PasteSource()

register_source("paste", PasteSource)
