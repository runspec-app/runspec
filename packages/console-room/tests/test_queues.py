"""
test_queues.py — the advertised-queue directory: aggregation, dedup, usable-domain
filtering, per-console withdraw, and the Hub methods that wrap it.
"""

from __future__ import annotations

from console_room.hub import Hub
from console_room.queues import QueueDescriptor, QueueDirectory


class FakeStore:
    def append(self, msg):  # pragma: no cover - unused here
        pass

    def recent(self, room, limit):  # pragma: no cover - unused here
        return []


def _d(queue, email="it-support@corp.com", label="IT Support"):
    return QueueDescriptor(queue=queue, queue_email=email, display_name=label)


# ── descriptor ────────────────────────────────────────────────────────────────


def test_descriptor_domain_and_label():
    d = _d("it-support", "it-support@corp.com", "IT Support")
    assert d.domain == "corp.com"
    assert d.label == "IT Support"


def test_descriptor_label_falls_back_to_queue():
    assert QueueDescriptor("it-support", "x@corp.com", "").label == "it-support"


# ── directory ─────────────────────────────────────────────────────────────────


def test_advertise_and_queues():
    dir_ = QueueDirectory()
    dir_.advertise("c1", _d("it-support"))
    queues = dir_.queues()
    assert [q.queue for q in queues] == ["it-support"]


def test_dedup_across_consoles_first_wins():
    dir_ = QueueDirectory()
    dir_.advertise("c1", _d("it-support", label="IT Support"))
    dir_.advertise("c2", _d("it-support", label="Helpdesk"))  # same queue
    queues = dir_.queues()
    assert len(queues) == 1
    assert queues[0].label == "IT Support"  # first console wins


def test_get_returns_descriptor():
    dir_ = QueueDirectory()
    dir_.advertise("c1", _d("it-support", "it-support@corp.com"))
    got = dir_.get("it-support")
    assert got is not None and got.queue_email == "it-support@corp.com"
    assert dir_.get("missing") is None


def test_queues_skips_unusable_mailbox():
    # A descriptor whose mailbox can't yield a domain can never be a verify target,
    # so it's never offered.
    dir_ = QueueDirectory()
    dir_.advertise("c1", _d("broken", email="not-an-email"))
    assert dir_.queues() == []
    assert dir_.get("broken") is None


def test_withdraw_drops_consoles_queues():
    dir_ = QueueDirectory()
    dir_.advertise("c1", _d("it-support"))
    dir_.advertise("c2", _d("infra", "infra@corp.com", "Infra"))
    dir_.withdraw("c1")
    assert [q.queue for q in dir_.queues()] == ["infra"]
    dir_.withdraw("c1")  # idempotent


def test_withdraw_keeps_queue_advertised_by_another_console():
    dir_ = QueueDirectory()
    dir_.advertise("c1", _d("it-support"))
    dir_.advertise("c2", _d("it-support"))
    dir_.withdraw("c1")
    assert [q.queue for q in dir_.queues()] == ["it-support"]


def test_readvertise_replaces_same_queue():
    dir_ = QueueDirectory()
    dir_.advertise("c1", _d("it-support", label="Old"))
    dir_.advertise("c1", _d("it-support", label="New"))
    assert dir_.get("it-support").label == "New"


# ── Hub wrappers ──────────────────────────────────────────────────────────────


def test_hub_advertise_and_read():
    hub = Hub(FakeStore())
    hub.advertise_queue(
        "agent@web01", "it-support", "it-support@corp.com", "IT Support"
    )
    queues = hub.advertised_queues()
    assert [q.queue for q in queues] == ["it-support"]
    assert hub.queue_descriptor("it-support").domain == "corp.com"


def test_hub_withdraw_queues():
    hub = Hub(FakeStore())
    hub.advertise_queue("agent@web01", "it-support", "it-support@corp.com")
    hub.withdraw_queues("agent@web01")
    assert hub.advertised_queues() == []
    assert hub.queue_descriptor("it-support") is None
