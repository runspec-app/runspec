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


# ── conversation lifecycle ────────────────────────────────────────────────────


def _convs(p: Any) -> list[tuple[str, str | None]]:
    return [
        (f["state"], f["assignee"]) for f in p.frames if f["type"] == "conversation"
    ]


def test_visitor_message_opens_thread_and_notifies_agent():
    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(agent)
        await hub.join(alice)
        agent.frames.clear()
        await hub.on_message(alice, "help, printer's on fire")
        return hub, agent

    hub, agent = run(scenario())
    assert hub.conversation_state(ALICE) == "open"
    # The agent saw the thread open (state frame) before/with the message.
    assert ("open", None) in _convs(agent)


def test_claim_assigns_and_broadcasts_to_visitor_and_team():
    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        mate = FakeAgent("agent2@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(agent)
        await hub.join_agent(mate)
        await hub.join(alice)
        await hub.on_message(alice, "printer")
        agent.frames.clear()
        mate.frames.clear()
        alice.frames.clear()
        await hub.claim(agent, ALICE)
        return hub, agent, mate, alice

    hub, agent, mate, alice = run(scenario())
    assert hub.conversation_state(ALICE) == "assigned"
    # Visitor and the whole team see who owns it.
    assert ("assigned", "agent@web01") in _convs(alice)
    assert ("assigned", "agent@web01") in _convs(mate)
    assert ("assigned", "agent@web01") in _convs(agent)  # echo to claimer too


def test_resolve_then_visitor_reply_reopens():
    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(agent)
        await hub.join(alice)
        await hub.on_message(alice, "printer")
        await hub.claim(agent, ALICE)
        await hub.resolve(agent, ALICE)
        resolved_state = hub.conversation_state(ALICE)
        alice.frames.clear()
        await hub.on_message(alice, "still broken")  # visitor reply → reopen
        return hub, resolved_state, alice

    hub, resolved_state, alice = run(scenario())
    assert resolved_state == "resolved"
    assert hub.conversation_state(ALICE) == "open"
    # The reopen state went out before the message landed.
    assert ("open", None) in _convs(alice)


def test_agent_cannot_control_conversation_outside_namespace():
    async def scenario():
        hub = Hub(FakeStore())
        it_agent = FakeAgent("agent@web01", IT)
        carol = FakeP("carol@corp", HR_ROOM)
        await hub.join_agent(it_agent)
        await hub.join(carol)
        await hub.on_message(carol, "hr question")  # opens the HR thread
        carol.frames.clear()
        await hub.claim(it_agent, HR_ROOM)  # IT agent reaching into HR — refused
        return hub, carol

    hub, carol = run(scenario())
    assert hub.conversation_state(HR_ROOM) == "open"  # unchanged
    assert _convs(carol) == []  # nothing broadcast


def test_visitor_rejoin_is_caught_up_on_state():
    async def scenario():
        hub = Hub(FakeStore())
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(agent)
        await hub.join(alice)
        await hub.on_message(alice, "printer")
        await hub.claim(agent, ALICE)
        await hub.leave(alice)
        # Visitor reloads the page → a fresh connection to the same room.
        alice2 = FakeP("alice@corp", ALICE)
        await hub.join(alice2)
        return alice2

    alice2 = run(scenario())
    assert ("assigned", "agent@web01") in _convs(alice2)


def test_agent_subscribe_is_caught_up_on_open_threads_state():
    async def scenario():
        hub = Hub(FakeStore())
        alice = FakeP("alice@corp", ALICE)
        await hub.join(alice)
        await hub.on_message(alice, "printer")  # thread open, no agent yet
        # A second agent claims via a first agent, then a fresh agent subscribes.
        first = FakeAgent("a1", IT)
        await hub.join_agent(first)
        await hub.claim(first, ALICE)
        late = FakeAgent("a2", IT)
        await hub.join_agent(late)
        return late

    late = run(scenario())
    # The late-joining agent's inbox shows the assigned thread on subscribe.
    assert ("assigned", "a1") in _convs(late)


# ── self-service catalog + visitor requests ───────────────────────────────────

CATALOG = [
    {
        "name": "reset-password",
        "description": "Reset it",
        "inputSchema": {"type": "object"},
    },
    {"name": "request-vm", "description": "Provision a VM"},
]


def test_catalog_publish_and_clear():
    hub = Hub(FakeStore())
    hub.set_catalog(IT, CATALOG)
    assert [t["name"] for t in hub.catalog_for(IT)] == ["reset-password", "request-vm"]
    assert hub.catalog_for("helpdesk/hr") == []  # other queues unaffected
    hub.set_catalog(IT, [])  # last console off shift clears it
    assert hub.catalog_for(IT) == []


def test_visitor_request_persisted_and_fanned_to_agent():
    async def scenario():
        store = FakeStore()
        hub = Hub(store)
        hub.set_catalog(IT, CATALOG)
        agent = FakeAgent("agent@web01", IT)
        alice = FakeP("alice@corp", ALICE)
        await hub.join_agent(agent)
        await hub.join(alice)
        agent.frames.clear()
        alice.frames.clear()
        ok = await hub.visitor_request(alice, "reset-password", {"user": "alice"})
        return store, hub, agent, alice, ok

    store, hub, agent, alice, ok = run(scenario())
    assert ok
    # Persisted as a request-kind message with the machine payload.
    saved = store.appended[0]
    assert saved.kind == "request"
    assert saved.data == {"tool": "reset-password", "args": {"user": "alice"}}
    assert saved.text.startswith("Requested: reset-password")
    # The submission opened the thread and reached the agent's inbox.
    assert hub.conversation_state(ALICE) == "open"
    agent_msgs = [f for f in agent.frames if f["type"] == "message"]
    assert agent_msgs and agent_msgs[0]["kind"] == "request"
    assert agent_msgs[0]["data"]["tool"] == "reset-password"
    # Echoed to the visitor too.
    assert any(f.get("kind") == "request" for f in alice.frames)


def test_visitor_request_refused_for_unknown_tool():
    async def scenario():
        store = FakeStore()
        hub = Hub(store)
        hub.set_catalog(IT, CATALOG)
        alice = FakeP("alice@corp", ALICE)
        await hub.join(alice)
        alice.frames.clear()
        ok = await hub.visitor_request(alice, "rotate-root-key", {})  # not in catalog
        return store, alice, ok

    store, alice, ok = run(scenario())
    assert not ok
    assert store.appended == [] and alice.frames == []


def test_visitor_request_refused_outside_conversation_room():
    async def scenario():
        store = FakeStore()
        hub = Hub(store)
        hub.set_catalog(IT, CATALOG)
        chatter = FakeP("oncall", "ops")  # a group-chat room, not a conversation
        await hub.join(chatter)
        ok = await hub.visitor_request(chatter, "reset-password", {})
        return store, ok

    store, ok = run(scenario())
    assert not ok and store.appended == []


def test_visitor_request_refused_for_oversized_args():
    async def scenario():
        store = FakeStore()
        hub = Hub(store)
        hub.set_catalog(IT, CATALOG)
        alice = FakeP("alice@corp", ALICE)
        await hub.join(alice)
        ok = await hub.visitor_request(alice, "reset-password", {"blob": "x" * 10_000})
        return store, ok

    store, ok = run(scenario())
    assert not ok and store.appended == []
