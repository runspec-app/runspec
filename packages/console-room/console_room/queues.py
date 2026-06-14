"""
queues.py — the advertised-queue directory for the email-verify signup page (pure).

The sign-up dropdown is not a static list; it's the **live union** of the queues
the connected agent consoles advertise (see docs/design/email-queue-signup.md §4).
Each console, per queue it staffs, advertises a small descriptor — the queue's
namespace, its mailbox address (the verify sender *and* the allowlist domain), and
a friendly display name. A queue with no connected console simply isn't offered.

This registry holds those advertisements per console so they drop cleanly when a
console disconnects, and answers the two questions the web routes ask: *what
queues can I sign up for?* (:meth:`queues`) and *what is this queue's mailbox?*
(:meth:`get`, for the domain check + the verify sender). Pure (no I/O); the hub
owns one instance and feeds it the consoles' advertisements (slice 3). The wire
plumbing is separate; the aggregation rules are unit-tested here.
"""

from __future__ import annotations

from dataclasses import dataclass

from .signup import email_domain


@dataclass(frozen=True)
class QueueDescriptor:
    """One queue a console staffs: its namespace, mailbox, and display label. The
    mailbox is the single source of truth — the verify-from address *and* the
    allowlist domain."""

    queue: str
    queue_email: str
    display_name: str = ""

    @property
    def domain(self) -> str:
        """The allowlist domain — the mailbox's domain (``""`` if unparseable)."""
        return email_domain(self.queue_email)

    @property
    def label(self) -> str:
        """The dropdown label — the display name, falling back to the queue name."""
        return self.display_name or self.queue


class QueueDirectory:
    """Per-console advertised queues, aggregated to the live sign-up list.

    Synchronous and side-effect-free beyond the internal map, so the hub can call
    it inline between ``await``s (atomic on the event loop) and the rules are
    tested directly. Insertion order is the consoles' arrival order, giving the
    aggregated list a stable order."""

    def __init__(self) -> None:
        # console identity -> queue name -> descriptor.
        self._consoles: dict[str, dict[str, QueueDescriptor]] = {}

    def advertise(self, console: str, descriptor: QueueDescriptor) -> None:
        """Record that ``console`` staffs the queue in ``descriptor`` (replacing any
        previous descriptor it gave for the same queue)."""
        self._consoles.setdefault(console, {})[descriptor.queue] = descriptor

    def withdraw(self, console: str) -> None:
        """Drop all of ``console``'s advertisements (its link dropped). Idempotent."""
        self._consoles.pop(console, None)

    def queues(self) -> list[QueueDescriptor]:
        """The live sign-up list: every advertised queue with a **usable mailbox
        domain**, deduplicated by queue name (first console to advertise it wins).
        A queue whose mailbox can't yield a domain is skipped — it could never be a
        verify target."""
        seen: dict[str, QueueDescriptor] = {}
        for queues in self._consoles.values():
            for name, descriptor in queues.items():
                if name not in seen and descriptor.domain:
                    seen[name] = descriptor
        return list(seen.values())

    def get(self, queue: str) -> QueueDescriptor | None:
        """The descriptor for ``queue`` if it's currently advertised (and usable),
        else ``None`` — the source of the verify sender + the allowlist domain."""
        for descriptor in self.queues():
            if descriptor.queue == queue:
                return descriptor
        return None
