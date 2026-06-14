"""
test_signup_dispatch.py — the hub-owned email-verify outbox: dispatch to a console,
flush-on-(advertise), the verify_sent ack, disconnect release + re-flush, the
namespace guard, and verify_token enrolment.

Each test runs its whole scenario inside one ``asyncio.run`` so the hub's lock
binds to a single event loop.
"""

from __future__ import annotations

import asyncio

from console_room.enrollment_store import EnrollmentStore
from console_room.hub import Hub
from console_room.models import SEND_VERIFY
from console_room.signup import EmailVerifyConfig, sign_verify_token

SECRET = "dispatch-test-secret"


class FakeStore:
    def append(self, msg):
        pass

    def recent(self, room, limit):
        return []


class FakeAgent:
    """A console subscribed to a namespace; captures frames the hub sends it."""

    wants_backscroll = False

    def __init__(self, user, namespace):
        self.user = user
        self.namespace = namespace
        self.sent: list = []

    async def send(self, frame):
        self.sent.append(frame)


def _hub():
    cfg = EmailVerifyConfig(session_secret=SECRET, public_url="https://room.test")
    return Hub(FakeStore(), enrollment=EnrollmentStore(":memory:"), email_verify=cfg)


def _verifies(agent):
    return [f for f in agent.sent if f.get("type") == SEND_VERIFY]


def test_email_verify_enabled_flag():
    assert _hub().email_verify_enabled is True
    assert Hub(FakeStore()).email_verify_enabled is False


def test_submit_signup_dispatches_to_connected_console():
    async def scenario():
        hub = _hub()
        agent = FakeAgent("agent@web01", "helpdesk/it-support")
        await hub.join_agent(agent)
        await hub.on_advertise(agent, "it-support", "it-support@corp.com", "IT Support")
        await hub.submit_signup("jane@corp.com", "Jane Doe", "it-support")
        return _verifies(agent)

    frames = asyncio.run(scenario())
    assert len(frames) == 1
    f = frames[0]
    assert f["to"] == "jane@corp.com"
    assert f["queue_email"] == "it-support@corp.com"
    assert f["verify_url"].startswith("https://room.test/verify?token=")
    assert f["nonce"]


def test_submit_signup_without_console_stays_in_outbox():
    async def scenario():
        hub = _hub()
        # Advertised (so the queue is known) but no console connected.
        await hub.submit_signup("jane@corp.com", "Jane", "it-support")
        return hub

    hub = asyncio.run(scenario())
    # The row waits, claimable by a console that later connects.
    pending = hub._enrollment.claim_pending_sends("it-support", "later")  # noqa: SLF001
    assert [p.email for p in pending] == ["jane@corp.com"]


def test_advertise_flushes_backlog_on_connect():
    async def scenario():
        hub = _hub()
        # A signup arrives with no console for the queue → stays pending.
        await hub.submit_signup("jane@corp.com", "Jane", "it-support")
        # Now a console connects and advertises the queue → its backlog flushes.
        agent = FakeAgent("agent@web01", "helpdesk/it-support")
        await hub.join_agent(agent)
        await hub.on_advertise(agent, "it-support", "it-support@corp.com", "IT Support")
        return _verifies(agent)

    frames = asyncio.run(scenario())
    assert [f["to"] for f in frames] == ["jane@corp.com"]


def test_advertise_outside_namespace_is_refused():
    async def scenario():
        hub = _hub()
        agent = FakeAgent("agent@web01", "helpdesk/it-support")
        await hub.join_agent(agent)
        # Advertises a queue whose namespace != the agent's → ignored.
        await hub.on_advertise(agent, "payroll", "payroll@corp.com", "Payroll")
        return hub.advertised_queues()

    assert asyncio.run(scenario()) == []


def test_verify_sent_ok_marks_sent():
    async def scenario():
        hub = _hub()
        agent = FakeAgent("agent@web01", "helpdesk/it-support")
        await hub.join_agent(agent)
        await hub.on_advertise(agent, "it-support", "it-support@corp.com", "IT Support")
        await hub.submit_signup("jane@corp.com", "Jane", "it-support")
        nonce = _verifies(agent)[0]["nonce"]
        await hub.on_verify_sent(nonce, True)
        return hub._enrollment.get_pending(nonce)  # noqa: SLF001

    row = asyncio.run(scenario())
    assert row.send_state == "sent"


def test_verify_sent_failure_returns_to_pending():
    async def scenario():
        hub = _hub()
        agent = FakeAgent("agent@web01", "helpdesk/it-support")
        await hub.join_agent(agent)
        await hub.on_advertise(agent, "it-support", "it-support@corp.com", "IT Support")
        await hub.submit_signup("jane@corp.com", "Jane", "it-support")
        nonce = _verifies(agent)[0]["nonce"]
        await hub.on_verify_sent(nonce, False)
        return hub._enrollment.get_pending(nonce)  # noqa: SLF001

    row = asyncio.run(scenario())
    assert row.send_state == "pending" and row.claimed_by == ""


def test_disconnect_releases_and_reflows_to_other_console():
    async def scenario():
        hub = _hub()
        a = FakeAgent("agent-a@web01", "helpdesk/it-support")
        b = FakeAgent("agent-b@web02", "helpdesk/it-support")
        await hub.join_agent(a)
        await hub.on_advertise(a, "it-support", "it-support@corp.com", "IT Support")
        await hub.join_agent(b)
        await hub.on_advertise(b, "it-support", "it-support@corp.com", "IT Support")
        # The signup claims for whichever console flushes first (a).
        await hub.submit_signup("jane@corp.com", "Jane", "it-support")
        # a drops before acking → its claim is released and re-flushed to b.
        await hub.leave_agent(a)
        return _verifies(b)

    frames = asyncio.run(scenario())
    assert [f["to"] for f in frames] == ["jane@corp.com"]


def test_verify_token_enrols_and_is_single_use():
    from console_room.signup import PendingSignup

    hub = _hub()
    hub._enrollment.add_pending(  # noqa: SLF001
        PendingSignup(
            nonce="n1",
            email="jane@corp.com",
            name="Jane",
            queue="it-support",
            created_at=0.0,
        )
    )
    token = sign_verify_token(SECRET, {"nonce": "n1"})
    member = hub.verify_token(token)
    assert member is not None and member.email == "jane@corp.com"
    assert hub._enrollment.is_member("jane@corp.com", "it-support")  # noqa: SLF001
    # Burned — a second click fails.
    assert hub.verify_token(token) is None


def test_verify_token_rejects_bad_token():
    hub = _hub()
    assert hub.verify_token("garbage") is None


# ── expiry sweep ──────────────────────────────────────────────────────────────


def test_sweep_expired_signups_drops_lapsed_pending():
    from console_room.signup import PendingSignup

    async def scenario():
        hub = _hub()  # token_ttl defaults to 1800
        # An old pending (created at epoch 0 → well past the TTL) and a fresh one.
        hub._enrollment.add_pending(  # noqa: SLF001
            PendingSignup("old", "a@corp.com", "A", "it-support", created_at=0.0)
        )
        hub._enrollment.add_pending(  # noqa: SLF001
            PendingSignup(
                "fresh",
                "b@corp.com",
                "B",
                "it-support",
                created_at=__import__("time").time(),
            )
        )
        removed = await hub.sweep_expired_signups()
        return hub, removed

    hub, removed = asyncio.run(scenario())
    assert removed == 1
    assert hub._enrollment.get_pending("old") is None  # noqa: SLF001
    assert hub._enrollment.get_pending("fresh") is not None  # noqa: SLF001


def test_sweep_is_noop_without_email_verify():
    async def scenario():
        return await Hub(FakeStore()).sweep_expired_signups()

    assert asyncio.run(scenario()) == 0


def test_run_sweeper_returns_immediately_when_disabled():
    async def scenario():
        # No email-verify → returns at once (doesn't hang on the interval).
        await asyncio.wait_for(Hub(FakeStore()).run_signup_sweeper(), timeout=1.0)
        # Enabled but interval 0 → also a no-op.
        await asyncio.wait_for(_hub().run_signup_sweeper(interval=0), timeout=1.0)

    asyncio.run(scenario())


def test_run_sweeper_ticks_and_sweeps_then_cancels():
    from contextlib import suppress

    from console_room.signup import PendingSignup

    async def scenario():
        hub = _hub()
        hub._enrollment.add_pending(  # noqa: SLF001
            PendingSignup("old", "a@corp.com", "A", "it-support", created_at=0.0)
        )
        task = asyncio.create_task(hub.run_signup_sweeper(interval=0.01))
        for _ in range(100):  # poll up to ~1s for the first tick to sweep
            await asyncio.sleep(0.01)
            if hub._enrollment.get_pending("old") is None:  # noqa: SLF001
                break
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return hub._enrollment.get_pending("old")  # noqa: SLF001

    assert asyncio.run(scenario()) is None
