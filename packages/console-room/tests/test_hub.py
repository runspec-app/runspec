"""
test_hub.py — the broker: join/leave presence, message stamping + echo, presence
relay, and the browser-only backscroll asymmetry. Driven with fake participants
and a fake store (no sockets, no SQLite); run via asyncio.run so no pytest-asyncio
dependency is needed.
"""

from __future__ import annotations

import asyncio
from typing import Any

from console_room.hub import Hub
from console_room.models import Message


def run(coro):
    return asyncio.run(coro)


class FakeStore:
    def __init__(self, seed: list[Message] | None = None) -> None:
        self.appended: list[Message] = []
        self._seed = list(seed or [])

    def append(self, msg: Message) -> None:
        self.appended.append(msg)

    def recent(self, room: str, limit: int) -> list[Message]:
        msgs = [m for m in self._seed if m.room == room]
        return msgs[-limit:] if limit > 0 else []


class FakeP:
    def __init__(self, user: str, room: str, wants_backscroll: bool = False) -> None:
        self.user = user
        self.room = room
        self.wants_backscroll = wants_backscroll
        self.frames: list[dict[str, Any]] = []

    async def send(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)


class FakeAgent:
    """A namespace-subscribed agent (the helpdesk console side). Forward-only."""

    wants_backscroll = False

    def __init__(self, user: str, namespace: str) -> None:
        self.user = user
        self.namespace = namespace
        self.frames: list[dict[str, Any]] = []

    async def send(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)


def _presences(p: FakeP) -> list[tuple[str, str]]:
    return [(f["user"], f["status"]) for f in p.frames if f["type"] == "presence"]


def _messages(p: FakeP) -> list[str]:
    return [f["text"] for f in p.frames if f["type"] == "message"]


def _room_presences(p: Any) -> list[tuple[str, str, str]]:
    return [
        (f["room"], f["user"], f["status"]) for f in p.frames if f["type"] == "presence"
    ]


def _room_messages(p: Any) -> list[tuple[str, str]]:
    return [(f["room"], f["text"]) for f in p.frames if f["type"] == "message"]


# ── presence on join / leave ──────────────────────────────────────────────────


def test_join_announces_and_catches_up():
    async def scenario():
        hub = Hub(FakeStore())
        a = FakeP("a", "ops")
        b = FakeP("b", "ops")
        await hub.join(a)  # alone — no one to hear or learn from
        await hub.join(b)  # arrives second
        return a, b

    a, b = run(scenario())
    # a was told b became available; b learned a was already present.
    assert _presences(a) == [("b", "available")]
    assert _presences(b) == [("a", "available")]


def test_leave_broadcasts_away():
    async def scenario():
        hub = Hub(FakeStore())
        a = FakeP("a", "ops")
        b = FakeP("b", "ops")
        await hub.join(a)
        await hub.join(b)
        a.frames.clear()
        b.frames.clear()
        await hub.leave(b)
        return hub, a

    hub, a = run(scenario())
    assert _presences(a) == [("b", "away")]
    assert hub.room_size("ops") == 1


def test_leave_when_not_joined_is_silent():
    async def scenario():
        hub = Hub(FakeStore())
        ghost = FakeP("ghost", "ops")
        await hub.leave(ghost)  # never joined → no broadcast, no crash
        return ghost

    ghost = run(scenario())
    assert ghost.frames == []


# ── messages ──────────────────────────────────────────────────────────────────


def test_message_persisted_and_echoed_to_all():
    async def scenario():
        store = FakeStore()
        hub = Hub(store)
        a = FakeP("a", "ops")
        b = FakeP("b", "ops")
        await hub.join(a)
        await hub.join(b)
        a.frames.clear()
        b.frames.clear()
        await hub.on_message(a, "deploying now")
        return store, a, b

    store, a, b = run(scenario())
    assert len(store.appended) == 1
    saved = store.appended[0]
    assert saved.sender == "a" and saved.text == "deploying now" and saved.id
    # Echoed to the sender too (so all views converge on the server id/ts).
    assert _messages(a) == ["deploying now"]
    assert _messages(b) == ["deploying now"]
    assert a.frames[0]["sender"] == "a" and a.frames[0]["id"] == saved.id


def test_message_only_reaches_its_room():
    async def scenario():
        hub = Hub(FakeStore())
        a = FakeP("a", "ops")
        other = FakeP("c", "infra")
        await hub.join(a)
        await hub.join(other)
        a.frames.clear()
        other.frames.clear()
        await hub.on_message(a, "ops only")
        return a, other

    a, other = run(scenario())
    assert _messages(a) == ["ops only"]
    assert other.frames == []  # different room hears nothing


# ── presence relay ────────────────────────────────────────────────────────────


def test_on_presence_relays_to_others_not_self():
    async def scenario():
        hub = Hub(FakeStore())
        a = FakeP("a", "ops")
        b = FakeP("b", "ops")
        await hub.join(a)
        await hub.join(b)
        a.frames.clear()
        b.frames.clear()
        await hub.on_presence(a, "away")  # a's rota flipped it off-shift
        return a, b

    a, b = run(scenario())
    assert _presences(a) == []  # never echoed to self
    assert _presences(b) == [("a", "away")]


# ── backscroll asymmetry ──────────────────────────────────────────────────────


def test_browser_gets_backscroll_console_does_not():
    seed = [
        Message(room="ops", sender="x", text="old1", id="i1", ts=1.0),
        Message(room="ops", sender="y", text="old2", id="i2", ts=2.0),
    ]

    async def scenario():
        hub = Hub(FakeStore(seed))
        console = FakeP("c", "ops", wants_backscroll=False)
        browser = FakeP("p", "ops", wants_backscroll=True)
        await hub.join(console)
        await hub.join(browser)
        return console, browser

    console, browser = run(scenario())
    assert _messages(console) == []  # forward-only: no history
    assert _messages(browser) == ["old1", "old2"]  # chronological backscroll


# ── namespace agents (helpdesk routing) ───────────────────────────────────────

IT = "helpdesk/it"
ALICE = "helpdesk/it/alice@corp"
BOB = "helpdesk/it/bob@corp"
HR_ROOM = "helpdesk/hr/carol@corp"


def test_agent_receives_visitor_message_across_conversations():
    """An agent subscribed to a queue gets messages from every conversation under
    it — its inbox — while visitors stay isolated in their own rooms."""

    async def scenario():
        store = FakeStore()
        hub = Hub(store)
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE, wants_backscroll=True)
        bob = FakeP("bob@corp", BOB, wants_backscroll=True)
        await hub.join_agent(agent)
        await hub.join(alice)
        await hub.join(bob)
        agent.frames.clear()
        alice.frames.clear()
        bob.frames.clear()
        await hub.on_message(alice, "my laptop won't boot")
        await hub.on_message(bob, "vpn is down")
        return agent, alice, bob

    agent, alice, bob = run(scenario())
    # The agent sees both conversations' messages, each tagged with its room.
    assert _room_messages(agent) == [
        (ALICE, "my laptop won't boot"),
        (BOB, "vpn is down"),
    ]
    # Visitors only ever see their own thread (no cross-talk).
    assert _room_messages(alice) == [(ALICE, "my laptop won't boot")]
    assert _room_messages(bob) == [(BOB, "vpn is down")]


def test_agent_reply_reaches_only_that_visitor():
    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        bob = FakeP("bob@corp", BOB)
        await hub.join_agent(agent)
        await hub.join(alice)
        await hub.join(bob)
        agent.frames.clear()
        alice.frames.clear()
        bob.frames.clear()
        await hub.agent_message(agent, ALICE, "have you tried safe mode?")
        return agent, alice, bob

    agent, alice, bob = run(scenario())
    assert _room_messages(alice) == [(ALICE, "have you tried safe mode?")]
    assert _room_messages(bob) == []  # bob never sees alice's thread
    assert _room_messages(agent) == [(ALICE, "have you tried safe mode?")]  # echo


def test_agent_cannot_post_outside_its_namespace():
    async def scenario():
        store = FakeStore()
        hub = Hub(store)
        agent = FakeAgent("agent@web01", IT)
        carol = FakeP("carol@corp", HR_ROOM)
        await hub.join_agent(agent)
        await hub.join(carol)
        carol.frames.clear()
        await hub.agent_message(agent, HR_ROOM, "wrong queue")
        return store, carol

    store, carol = run(scenario())
    assert _room_messages(carol) == []  # refused — different queue
    assert store.appended == []  # not even persisted


def test_agent_joining_is_caught_up_on_open_conversations():
    """An agent that joins after conversations are open learns who's waiting."""

    async def scenario():
        hub = Hub(FakeStore())
        alice = FakeP("alice@corp", ALICE)
        bob = FakeP("bob@corp", BOB)
        await hub.join(alice)
        await hub.join(bob)
        agent = FakeAgent("agent@web01", IT)
        await hub.join_agent(agent)
        return agent

    agent = run(scenario())
    # Presence for each waiting visitor, tagged with their conversation room.
    seen = set(_room_presences(agent))
    assert (ALICE, "alice@corp", "available") in seen
    assert (BOB, "bob@corp", "available") in seen


def test_visitor_sees_agent_presence_on_join():
    """A visitor opening a thread sees the on-shift agent already present."""

    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        await hub.join_agent(agent)
        alice = FakeP("alice@corp", ALICE)
        await hub.join(alice)
        return alice

    alice = run(scenario())
    assert (ALICE, "agent@web01", "available") in _room_presences(alice)


def test_agent_leave_marks_away_in_conversations():
    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(agent)
        await hub.join(alice)
        alice.frames.clear()
        await hub.leave_agent(agent)
        return hub, alice

    hub, alice = run(scenario())
    assert (ALICE, "agent@web01", "away") in _room_presences(alice)
    assert hub.agent_count(IT) == 0


def test_agent_namespace_does_not_leak_into_group_chat():
    """A namespace agent must not receive an unrelated group-chat room's traffic."""

    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        ops = FakeP("oncall", "ops")  # plain group-chat room
        await hub.join_agent(agent)
        await hub.join(ops)
        agent.frames.clear()
        await hub.on_message(ops, "deploy started")
        return agent

    agent = run(scenario())
    assert agent.frames == []  # the agent's queue does not cover "ops"


# ── presence-aware live catalogue + affinity (the portal spine) ───────────────


def _live_names(hub: Hub, namespace: str) -> list[str]:
    return [t["name"] for t in hub.live_catalog(namespace)]


def test_live_catalog_is_union_of_on_shift_consoles():
    async def scenario():
        hub = Hub(FakeStore())
        a = FakeAgent("a@web01", IT)
        b = FakeAgent("b@web02", IT)
        await hub.join_agent(a)
        await hub.join_agent(b)
        hub.publish_catalog(a, [{"name": "reset-password"}, {"name": "request-vm"}])
        hub.publish_catalog(b, [{"name": "unlock-account"}])
        return hub

    hub = run(scenario())
    assert _live_names(hub, IT) == ["reset-password", "request-vm", "unlock-account"]
    assert hub.runnable_holders(IT, "reset-password") == ["a@web01"]


def test_single_holder_runnable_vanishes_off_shift_and_on_leave():
    async def scenario():
        hub = Hub(FakeStore())
        a = FakeAgent("a@web01", IT)
        b = FakeAgent("b@web02", IT)
        await hub.join_agent(a)
        await hub.join_agent(b)
        hub.publish_catalog(a, [{"name": "reset-password"}])
        hub.publish_catalog(b, [{"name": "request-vm"}])  # only b holds this
        before = _live_names(hub, IT)
        await hub.set_agent_shift(b, False)  # b's rota flips off shift
        off_shift = _live_names(hub, IT)
        # b is off shift but still connected → still a page target.
        paged = hub.connected_holders(IT, "request-vm")
        await hub.set_agent_shift(b, True)
        back = _live_names(hub, IT)
        await hub.leave_agent(b)  # b's console disconnects entirely
        gone = _live_names(hub, IT)
        return before, off_shift, paged, back, gone

    before, off_shift, paged, back, gone = run(scenario())
    assert before == ["reset-password", "request-vm"]
    assert off_shift == ["reset-password"]  # request-vm vanished
    assert paged == ["b@web02"]  # still reachable by a page
    assert back == ["reset-password", "request-vm"]  # reappears on shift
    assert gone == ["reset-password"]  # gone for good once the console leaves


def test_leave_agent_withdraws_from_live_catalog():
    async def scenario():
        hub = Hub(FakeStore())
        a = FakeAgent("a@web01", IT)
        await hub.join_agent(a)
        hub.publish_catalog(a, [{"name": "reset-password"}])
        present = _live_names(hub, IT)
        await hub.leave_agent(a)
        return present, hub

    present, hub = run(scenario())
    assert present == ["reset-password"]
    assert hub.live_catalog(IT) == []
    assert hub.runnable_holders(IT, "reset-password") == []


def test_live_catalog_isolated_per_queue():
    async def scenario():
        hub = Hub(FakeStore())
        it_agent = FakeAgent("a@web01", IT)
        hr_agent = FakeAgent("c@web03", "helpdesk/hr")
        await hub.join_agent(it_agent)
        await hub.join_agent(hr_agent)
        hub.publish_catalog(it_agent, [{"name": "reset-password"}])
        hub.publish_catalog(hr_agent, [{"name": "onboard"}])
        return hub

    hub = run(scenario())
    assert _live_names(hub, IT) == ["reset-password"]
    assert _live_names(hub, "helpdesk/hr") == ["onboard"]


# ── portal requests: auto-assignment + claim-free lifecycle ───────────────────


def _assignments(a: Any) -> list[tuple[str, str]]:
    return [(f["id"], f["runnable"]) for f in a.frames if f["type"] == "assignment"]


def _statuses(p: Any) -> list[tuple[str, str | None]]:
    return [
        (f["state"], f["assignee"]) for f in p.frames if f["type"] == "request_status"
    ]


def test_submit_request_auto_assigns_to_on_shift_holder():
    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(agent)
        await hub.join(alice)
        hub.publish_catalog(agent, [{"name": "reset-password"}])
        agent.frames.clear()
        alice.frames.clear()
        req = await hub.submit_request(alice, "reset-password", {"user": "alice"})
        return hub, agent, alice, req

    hub, agent, alice, req = run(scenario())
    assert req is not None and hub.request_state(req.id) == "assigned"
    # The operator got the assignment in its inbox…
    assert _assignments(agent) == [(req.id, "reset-password")]
    # …and the thread (visitor + team) saw it assigned to that operator.
    assert ("assigned", "agent@web01") in _statuses(alice)
    assert ("assigned", "agent@web01") in _statuses(agent)


def test_submit_request_carries_visitor_name_to_operator_card():
    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        alice.display_name = "Alice Smith"  # set by the email-verify session
        await hub.join_agent(agent)
        await hub.join(alice)
        hub.publish_catalog(agent, [{"name": "reset-password"}])
        agent.frames.clear()
        await hub.submit_request(alice, "reset-password", {"user": "alice"})
        return agent

    agent = run(scenario())
    assignment = next(f for f in agent.frames if f["type"] == "assignment")
    assert assignment["visitor"] == "alice@corp"
    assert assignment["visitor_name"] == "Alice Smith"


def test_operator_skill_request_routes_with_kind():
    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        ops = FakeP("ops@corp", ALICE)
        ops.operator = True  # an operator session may request operator-audience tools
        await hub.join_agent(agent)
        await hub.join(ops)
        hub.publish_catalog(
            agent,
            [{"name": "skill:restart", "x-audience": "operator", "x-kind": "skill"}],
        )
        agent.frames.clear()
        req = await hub.submit_request(ops, "skill:restart", {"svc": "nginx"}, "skill")
        return agent, req

    agent, req = run(scenario())
    assert req is not None and req.kind == "skill" and req.runnable == "skill:restart"
    assignment = next(f for f in agent.frames if f["type"] == "assignment")
    assert assignment["kind"] == "skill"
    assert assignment["runnable"] == "skill:restart"
    assert assignment["args"] == {"svc": "nginx"}


def test_request_carries_chosen_host_to_operator():
    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(agent)
        await hub.join(alice)
        hub.publish_catalog(
            agent, [{"name": "check-disk", "x-hosts": ["web01", "web02"]}]
        )
        agent.frames.clear()
        req = await hub.submit_request(alice, "check-disk", {}, "runnable", "web02")
        return agent, req

    agent, req = run(scenario())
    assert req is not None and req.host == "web02"
    assignment = next(f for f in agent.frames if f["type"] == "assignment")
    assert assignment["host"] == "web02"  # the operator's target rides to the console


def test_visitor_cannot_request_operator_skill():
    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)  # a plain visitor (no .operator)
        await hub.join_agent(agent)
        await hub.join(alice)
        hub.publish_catalog(
            agent,
            [{"name": "skill:restart", "x-audience": "operator", "x-kind": "skill"}],
        )
        return await hub.submit_request(alice, "skill:restart", {}, "skill")

    assert run(scenario()) is None  # refused — operator-only, no request created


def test_submit_request_blank_visitor_name_without_session_name():
    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)  # no display_name (dev mode / console)
        await hub.join_agent(agent)
        await hub.join(alice)
        hub.publish_catalog(agent, [{"name": "reset-password"}])
        agent.frames.clear()
        await hub.submit_request(alice, "reset-password", {})
        return agent

    agent = run(scenario())
    assignment = next(f for f in agent.frames if f["type"] == "assignment")
    assert assignment["visitor_name"] == ""


def test_submit_request_persists_thread_message_and_fans_to_agent():
    async def scenario():
        store = FakeStore()
        hub = Hub(store)
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(agent)
        await hub.join(alice)
        hub.publish_catalog(agent, [{"name": "reset-password"}])
        agent.frames.clear()
        alice.frames.clear()
        req = await hub.submit_request(alice, "reset-password", {"user": "alice"})
        return store, agent, alice, req

    store, agent, alice, req = run(scenario())
    # Persisted as a request-kind thread message linking to the lifecycle record.
    saved = store.appended[0]
    assert saved.kind == "request"
    assert saved.data == {
        "tool": "reset-password",
        "args": {"user": "alice"},
        "request_id": req.id,
    }
    assert saved.text.startswith("Requested: reset-password")
    # The request message reached the agent's inbox and echoed to the visitor.
    agent_msgs = [f for f in agent.frames if f["type"] == "message"]
    assert agent_msgs and agent_msgs[0]["data"]["tool"] == "reset-password"
    assert any(f.get("kind") == "request" for f in alice.frames)


def test_submit_request_refused_for_oversized_args():
    async def scenario():
        store = FakeStore()
        hub = Hub(store)
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(agent)
        await hub.join(alice)
        hub.publish_catalog(agent, [{"name": "reset-password"}])
        req = await hub.submit_request(alice, "reset-password", {"blob": "x" * 10_000})
        return store, req

    store, req = run(scenario())
    assert req is None and store.appended == []


def test_submit_request_round_robins_across_holders():
    async def scenario():
        hub = Hub(FakeStore())
        a = FakeAgent("a@web01", IT)
        b = FakeAgent("b@web02", IT)
        alice = FakeP("alice@corp", ALICE)
        bob = FakeP("bob@corp", BOB)
        await hub.join_agent(a)
        await hub.join_agent(b)
        await hub.join(alice)
        await hub.join(bob)
        hub.publish_catalog(a, [{"name": "reset-password"}])
        hub.publish_catalog(b, [{"name": "reset-password"}])
        r1 = await hub.submit_request(alice, "reset-password", {})
        r2 = await hub.submit_request(bob, "reset-password", {})
        return hub, r1, r2

    hub, r1, r2 = run(scenario())
    # Two submits alternate between the two holders (plain round-robin).
    assignees = {hub._requests.get(r1.id).assignee, hub._requests.get(r2.id).assignee}
    assert assignees == {"a@web01", "b@web02"}


def test_submit_request_unassigned_then_auto_assigns_on_shift():
    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(agent)
        await hub.join(alice)
        hub.publish_catalog(agent, [{"name": "reset-password"}])
        await hub.set_agent_shift(agent, False)  # only holder steps off shift
        agent.frames.clear()
        alice.frames.clear()
        # Submit lands with no on-shift holder → accepted but unassigned.
        req = await hub.submit_request(alice, "reset-password", {})
        unassigned_state = hub.request_state(req.id)
        first_status = _statuses(alice)
        no_assignment_yet = _assignments(agent)
        # The holder comes back on shift → the backlog drains to it.
        await hub.set_agent_shift(agent, True)
        return hub, agent, alice, req, unassigned_state, first_status, no_assignment_yet

    hub, agent, alice, req, unassigned_state, first_status, no_assign = run(scenario())
    assert unassigned_state == "unassigned"
    assert first_status == [("unassigned", None)]
    assert no_assign == []  # nothing routed while no one was on shift
    # Once on shift, the pending request auto-assigned without a re-submit.
    assert hub.request_state(req.id) == "assigned"
    assert _assignments(agent) == [(req.id, "reset-password")]
    assert ("assigned", "agent@web01") in _statuses(alice)


def test_submit_request_refused_when_nobody_holds_it():
    async def scenario():
        store = FakeStore()
        hub = Hub(store)
        alice = FakeP("alice@corp", ALICE)
        await hub.join(alice)
        alice.frames.clear()
        req = await hub.submit_request(alice, "rotate-root-key", {})  # no holder
        return store, alice, req

    store, alice, req = run(scenario())
    assert req is None
    assert store.appended == [] and alice.frames == []


def test_submit_request_refused_outside_conversation_room():
    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        await hub.join_agent(agent)
        hub.publish_catalog(agent, [{"name": "reset-password"}])
        chatter = FakeP("oncall", "ops")  # a group-chat room, not a conversation
        await hub.join(chatter)
        req = await hub.submit_request(chatter, "reset-password", {})
        return req

    req = run(scenario())
    assert req is None


def test_start_and_complete_request_broadcast_status():
    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(agent)
        await hub.join(alice)
        hub.publish_catalog(agent, [{"name": "reset-password"}])
        req = await hub.submit_request(alice, "reset-password", {})
        alice.frames.clear()
        await hub.start_request(agent, req.id)
        await hub.complete_request(agent, req.id)
        return hub, alice, req

    hub, alice, req = run(scenario())
    assert hub.request_state(req.id) == "done"
    states = [s for s, _ in _statuses(alice)]
    assert states == ["in_progress", "done"]


def test_request_control_scoped_to_the_assignee():
    async def scenario():
        hub = Hub(FakeStore())
        owner = FakeAgent("owner@web01", IT)
        mate = FakeAgent("mate@web02", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(owner)
        await hub.join_agent(mate)
        await hub.join(alice)
        # Only owner holds it, so it routes to owner.
        hub.publish_catalog(owner, [{"name": "reset-password"}])
        req = await hub.submit_request(alice, "reset-password", {})
        await hub.start_request(mate, req.id)  # a different agent — refused
        return hub, req

    hub, req = run(scenario())
    assert hub.request_state(req.id) == "assigned"  # mate couldn't start it


def test_release_request_returns_to_queue_and_holds_when_alone():
    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(agent)
        await hub.join(alice)
        hub.publish_catalog(agent, [{"name": "reset-password"}])
        req = await hub.submit_request(alice, "reset-password", {})
        await hub.set_agent_shift(agent, False)  # the only holder drops off shift
        alice.frames.clear()
        await hub.release_request(req.id)  # AFK timeout fires
        return hub, alice, req

    hub, alice, req = run(scenario())
    # No on-shift holder to take it → back in the queue, awaiting a page.
    assert hub.request_state(req.id) == "unassigned"
    assert ("unassigned", None) in _statuses(alice)


# ── page / chime (no on-shift operator) ───────────────────────────────────────


def _pages(a: Any) -> list[tuple[str, str]]:
    return [(f["runnable"], f["id"]) for f in a.frames if f["type"] == "page"]


def test_unassigned_submit_pages_connected_consoles():
    async def scenario():
        hub = Hub(FakeStore())
        holder = FakeAgent("holder@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(holder)
        await hub.join(alice)
        hub.publish_catalog(holder, [{"name": "reset-password"}])
        await hub.set_agent_shift(holder, False)  # the holder is off shift now
        holder.frames.clear()
        req = await hub.submit_request(alice, "reset-password", {})
        return hub, holder, req

    hub, holder, req = run(scenario())
    assert hub.request_state(req.id) == "unassigned"
    # The off-shift-but-connected holder was chimed, naming the runnable.
    assert _pages(holder) == [("reset-password", req.id)]


def test_page_reaches_every_connected_console_not_just_holders():
    async def scenario():
        hub = Hub(FakeStore())
        holder = FakeAgent("holder@web01", IT)  # off shift, holds it
        bystander = FakeAgent("bystander@web02", IT)  # on shift, holds nothing
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(holder)
        await hub.join_agent(bystander)
        await hub.join(alice)
        hub.publish_catalog(holder, [{"name": "reset-password"}])
        await hub.set_agent_shift(holder, False)
        holder.frames.clear()
        bystander.frames.clear()
        req = await hub.submit_request(alice, "reset-password", {})
        return hub, holder, bystander, req

    hub, holder, bystander, req = run(scenario())
    assert hub.request_state(req.id) == "unassigned"
    # Every connected console of the queue is chimed — including the on-shift
    # console that doesn't hold it (it might know who can, or come help).
    assert _pages(holder) == [("reset-password", req.id)]
    assert _pages(bystander) == [("reset-password", req.id)]


def test_assigned_submit_does_not_page():
    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(agent)
        await hub.join(alice)
        hub.publish_catalog(agent, [{"name": "reset-password"}])  # on shift
        agent.frames.clear()
        await hub.submit_request(alice, "reset-password", {})
        return agent

    agent = run(scenario())
    assert _pages(agent) == []  # it auto-assigned — no need to page


def test_explicit_page_request_rechime_and_guards():
    async def scenario():
        hub = Hub(FakeStore())
        holder = FakeAgent("holder@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(holder)
        await hub.join(alice)
        hub.publish_catalog(holder, [{"name": "reset-password"}])
        await hub.set_agent_shift(holder, False)
        req = await hub.submit_request(alice, "reset-password", {})  # → unassigned
        holder.frames.clear()
        ok = await hub.page_request(alice, req.id)  # visitor clicks "Page the team"
        unknown = await hub.page_request(alice, "nope")  # unknown request
        wrong_room = await hub.page_request(FakeP("bob@corp", BOB), req.id)
        return holder, ok, unknown, wrong_room

    holder, ok, unknown, wrong_room = run(scenario())
    assert ok is True
    assert _pages(holder) == [("reset-password", holder.frames[0]["id"])]  # re-chimed
    assert unknown is False and wrong_room is False


def test_page_request_refused_once_assigned():
    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(agent)
        await hub.join(alice)
        hub.publish_catalog(agent, [{"name": "reset-password"}])  # on shift → assigns
        req = await hub.submit_request(alice, "reset-password", {})
        ok = await hub.page_request(alice, req.id)  # already assigned — nothing waiting
        return ok

    ok = run(scenario())
    assert ok is False


def test_afk_release_pages_the_team():
    async def scenario():
        hub = Hub(FakeStore())
        holder = FakeAgent("holder@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(holder)
        await hub.join(alice)
        hub.publish_catalog(holder, [{"name": "reset-password"}])
        req = await hub.submit_request(alice, "reset-password", {})  # assigned
        await hub.set_agent_shift(holder, False)  # the assignee drops off shift
        holder.frames.clear()
        await hub.release_request(req.id)  # AFK timeout fires
        return hub, holder, req

    hub, holder, req = run(scenario())
    assert hub.request_state(req.id) == "unassigned"
    assert _pages(holder) == [("reset-password", req.id)]  # team re-chimed


# ── AFK release timer ─────────────────────────────────────────────────────────


def test_afk_timer_releases_stranded_request_and_pages():
    async def scenario():
        hub = Hub(FakeStore(), assignment_timeout=0.05)
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(agent)
        await hub.join(alice)
        hub.publish_catalog(agent, [{"name": "reset-password"}])
        req = await hub.submit_request(alice, "reset-password", {})  # → assigned
        assigned = hub.request_state(req.id)
        agent.frames.clear()
        alice.frames.clear()
        await asyncio.sleep(0.15)  # let the AFK timer fire (console never started it)
        return hub, agent, alice, req, assigned

    hub, agent, alice, req, assigned = run(scenario())
    assert assigned == "assigned"
    # Released back to the queue (the one stuck holder is excluded → unassigned),
    # and the team is re-paged.
    assert hub.request_state(req.id) == "unassigned"
    assert _pages(agent) == [("reset-password", req.id)]
    assert ("unassigned", None) in _statuses(alice)


def test_start_cancels_the_afk_timer():
    async def scenario():
        hub = Hub(FakeStore(), assignment_timeout=0.05)
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(agent)
        await hub.join(alice)
        hub.publish_catalog(agent, [{"name": "reset-password"}])
        req = await hub.submit_request(alice, "reset-password", {})
        await hub.start_request(agent, req.id)  # the console picked it up
        await asyncio.sleep(0.12)  # past the timeout — must NOT release
        return hub, req

    hub, req = run(scenario())
    assert hub.request_state(req.id) == "in_progress"


def test_afk_reassigns_to_a_different_holder():
    async def scenario():
        hub = Hub(FakeStore(), assignment_timeout=0.05)
        a = FakeAgent("a@web01", IT)
        b = FakeAgent("b@web02", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(a)
        await hub.join_agent(b)
        await hub.join(alice)
        hub.publish_catalog(a, [{"name": "reset-password"}])
        hub.publish_catalog(b, [{"name": "reset-password"}])
        req = await hub.submit_request(alice, "reset-password", {})
        first = hub._requests.get(req.id).assignee  # round-robin → a@web01
        # Sleep into the window after the *first* AFK release (one timeout, 0.05s)
        # but before a second could fire (two timeouts, 0.10s): with only two holders
        # each timeout oscillates the assignee a↔b, so land squarely on the single
        # reassignment this asserts. a never starts → AFK → reassign excluding a → b.
        await asyncio.sleep(0.08)
        return hub, req, first

    hub, req, first = run(scenario())
    assert first == "a@web01"
    assert hub.request_state(req.id) == "assigned"  # reassigned, not stranded
    assert hub._requests.get(req.id).assignee == "b@web02"  # to the OTHER holder


def test_afk_timer_disabled_when_timeout_zero():
    async def scenario():
        hub = Hub(FakeStore(), assignment_timeout=0)
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(agent)
        await hub.join(alice)
        hub.publish_catalog(agent, [{"name": "reset-password"}])
        req = await hub.submit_request(alice, "reset-password", {})
        await asyncio.sleep(0.08)
        return hub, req

    hub, req = run(scenario())
    assert hub.request_state(req.id) == "assigned"  # timer disabled → never released
