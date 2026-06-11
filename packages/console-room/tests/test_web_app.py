"""
test_web_app.py — the Starlette app over a real WebSocket, via TestClient.

Skipped if the web stack (starlette + httpx) isn't installed, so the stdlib core
tests still run in a minimal environment. Covers the ws round-trip (two browser
clients in a room see each other's messages + presence), backscroll on join,
healthz, the static index, and the room-required guard.
"""

from __future__ import annotations

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402

from console_room.hub import Hub  # noqa: E402
from console_room.models import Message  # noqa: E402
from console_room.web.app import create_app  # noqa: E402


class FakeStore:
    def __init__(self, seed=None):
        self.appended = list(seed or [])

    def append(self, msg):
        self.appended.append(msg)

    def recent(self, room, limit):
        msgs = [m for m in self.appended if m.room == room]
        return msgs[-limit:] if limit > 0 else []


def _client(store=None):
    hub = Hub(store or FakeStore())
    # No ndjson_addr → the lifespan binds no extra socket during tests.
    return TestClient(create_app(hub))


def test_healthz():
    with _client() as client:
        r = client.get("/healthz")
        assert r.status_code == 200 and r.json() == {"ok": True}


def test_static_index_served():
    with _client() as client:
        r = client.get("/")
        assert r.status_code == 200 and "runspec room" in r.text


def test_ws_requires_room():
    with _client() as client:
        with pytest.raises(Exception):
            # No room → server closes before accept; the handshake fails.
            with client.websocket_connect("/ws?user=ana"):
                pass


def test_ws_two_clients_exchange_message_and_presence():
    store = FakeStore()
    with _client(store) as client:
        with client.websocket_connect("/ws?room=ops&user=ana") as ana:
            with client.websocket_connect("/ws?room=ops&user=bob") as bob:
                # On bob's join: ana is told bob is available; bob is caught up
                # that ana is already present.
                ev = ana.receive_json()
                assert ev["type"] == "presence" and ev["user"] == "bob"
                ev = bob.receive_json()
                assert ev["type"] == "presence" and ev["user"] == "ana"
                # bob sends a message; both see it, stamped with sender + id.
                bob.send_json({"type": "message", "room": "ops", "text": "hi all"})
                got = bob.receive_json()
                assert got["type"] == "message"
                assert got["sender"] == "bob" and got["text"] == "hi all" and got["id"]
                got_ana = ana.receive_json()
                assert got_ana["type"] == "message" and got_ana["text"] == "hi all"
    # Persisted.
    assert [m.text for m in store.appended] == ["hi all"]


def test_ws_backscroll_on_join():
    seed = [
        Message(room="ops", sender="x", text="earlier", id="i1", ts=1.0),
    ]
    with _client(FakeStore(seed)) as client:
        with client.websocket_connect("/ws?room=ops&user=ana") as ana:
            first = ana.receive_json()
            assert first["type"] == "message" and first["text"] == "earlier"
