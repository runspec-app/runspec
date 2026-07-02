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


def test_me_in_dev_mode_has_no_session_error():
    # Dev mode installs no SessionMiddleware; /me must not 500 on request.session.
    with _client() as client:
        r = client.get("/me")
        assert r.status_code == 200
        # Dev mode: name + team are blank (the browser declares them); queue is the
        # single helpdesk queue this room serves.
        assert r.json() == {
            "auth": False,
            "user": "",
            "name": "",
            "team": "",
            "operator": False,
            "queue": "helpdesk",
            "branding": {},  # no [branding] configured → default look
        }


def test_favicon_returns_no_content():
    with _client() as client:
        assert client.get("/favicon.ico").status_code == 204


def test_ws_requires_room():
    with _client() as client:
        with pytest.raises(Exception):
            # No room → server closes before accept; the handshake fails.
            with client.websocket_connect("/ws?user=ana"):
                pass


def test_ws_no_room_logs_diagnostic(caplog):
    # The dev-mode no-room reject logs which branch fired (so a bare HTTP 403 at
    # the browser is traceable in the server log).
    with _client() as client:
        with caplog.at_level("INFO", logger="console_room.web"):
            with pytest.raises(Exception):
                with client.websocket_connect("/ws?user=ana"):
                    pass
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "no ?room= or ?queue=" in msg


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


# ── helpdesk visitors (?queue=) ────────────────────────────────────────────────


def test_helpdesk_visitor_room_is_derived_server_side():
    # A ?queue= visitor's room is derived from the queue + identity, not supplied.
    store = FakeStore()
    with _client(store) as client:
        with client.websocket_connect("/ws?queue=it&user=alice@corp") as alice:
            alice.send_json({"type": "message", "text": "laptop won't boot"})
            # The message echoes back stamped with the server-derived room (no
            # claim-conversation frame — the thread is just the message stream).
            got = alice.receive_json()
            assert got["type"] == "message" and got["text"] == "laptop won't boot"
            assert got["room"] == "helpdesk/it/alice@corp"
    assert store.appended[0].room == "helpdesk/it/alice@corp"


def test_two_helpdesk_visitors_are_isolated():
    # Two visitors on the same queue land in different private rooms and must
    # never see each other's traffic — the cross-department-leakage guarantee.
    # Were they sharing a room, bob's first frame would be a presence about alice;
    # in isolation his first (and only) frame is his own message echo.
    store = FakeStore()
    with _client(store) as client:
        with client.websocket_connect("/ws?queue=it&user=alice@corp") as alice:
            with client.websocket_connect("/ws?queue=it&user=bob@corp") as bob:
                alice.send_json({"type": "message", "text": "alice secret"})
                bob.send_json({"type": "message", "text": "bob secret"})
                # Each visitor's first (and only) frame is its own message echo,
                # stamped with its own room — never the other's.
                a_msg = alice.receive_json()
                b_msg = bob.receive_json()
                assert a_msg["room"] == "helpdesk/it/alice@corp"
                assert b_msg["room"] == "helpdesk/it/bob@corp"
                assert a_msg["type"] == "message" and a_msg["text"] == "alice secret"
                assert b_msg["type"] == "message" and b_msg["text"] == "bob secret"
    # Both threads persisted under distinct, identity-derived rooms.
    rooms = {m.room for m in store.appended}
    assert rooms == {"helpdesk/it/alice@corp", "helpdesk/it/bob@corp"}


def test_helpdesk_visitor_rejects_bad_queue_token():
    with _client() as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ws?queue=a/b&user=ana"):
                pass


def test_dev_visitor_team_param_rides_onto_the_card(monkeypatch):
    # In dev mode the browser declares its team via ?team= (no session to read it
    # from); it must reach the operator card the same way the verified session's
    # team does. Spy on the connection to capture the team ws_room resolved.
    from console_room.web import app as app_mod

    captured: dict[str, str] = {}
    real_init = app_mod.BrowserConnection.__init__

    def spy(self, ws, *, user, room, display_name="", team="", operator=False):
        captured["user"] = user
        captured["team"] = team
        real_init(
            self,
            ws,
            user=user,
            room=room,
            display_name=display_name,
            team=team,
            operator=operator,
        )

    monkeypatch.setattr(app_mod.BrowserConnection, "__init__", spy)
    with _client() as client:
        with client.websocket_connect(
            "/ws?queue=it&user=alice@corp&team=sales"
        ) as alice:
            alice.send_json({"type": "message", "text": "hi"})
            alice.receive_json()
    assert captured == {"user": "alice@corp", "team": "sales"}


# ── catalog endpoint + request flow ────────────────────────────────────────────

CATALOG = [
    {
        "name": "reset-password",
        "description": "Reset your password",
        "inputSchema": {
            "type": "object",
            "properties": {"user": {"type": "string"}},
            "required": ["user"],
        },
    }
]


class _SeedAgent:
    """Minimal on-shift agent stand-in to seed the live catalogue —
    ``publish_catalog`` only reads ``.namespace`` / ``.user``."""

    def __init__(self, user, namespace):
        self.user = user
        self.namespace = namespace

    async def send(self, frame):  # pragma: no cover - never delivered to in these tests
        pass


def _client_with_catalog(store=None):
    hub = Hub(store or FakeStore())
    hub.publish_catalog(_SeedAgent("agent@web01", "helpdesk/it"), CATALOG)
    return TestClient(create_app(hub))


def test_catalog_endpoint_serves_published_tools():
    with _client_with_catalog() as client:
        r = client.get("/catalog?queue=it")
        assert r.status_code == 200
        body = r.json()
        assert body["queue"] == "it"
        assert [t["name"] for t in body["tools"]] == ["reset-password"]
        # An unpublished queue is an empty catalog, not an error.
        assert client.get("/catalog?queue=hr").json()["tools"] == []


def test_catalog_endpoint_requires_valid_queue():
    with _client_with_catalog() as client:
        assert client.get("/catalog").status_code == 400
        assert client.get("/catalog?queue=a/b").status_code == 400


# An operator-only runnable + an operator runbook beside the self-service one.
MIXED_CATALOG = [
    {"name": "reset-password", "x-audience": "self-service", "x-kind": "runnable"},
    {"name": "rotate-root-key", "x-audience": "operator", "x-kind": "runnable"},
    {
        "name": "skill:restart",
        "x-audience": "operator",
        "x-kind": "skill",
        "x-title": "Restart a service",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _client_with_mixed_catalog(store=None):
    hub = Hub(store or FakeStore())
    hub.publish_catalog(_SeedAgent("agent@web01", "helpdesk/it"), MIXED_CATALOG)
    return TestClient(create_app(hub))


def test_catalog_filters_operator_entries_for_visitors():
    with _client_with_mixed_catalog() as client:
        # Dev mode visitor (no ?operator=1): only the self-service entry.
        visitor = client.get("/catalog?queue=it").json()["tools"]
        assert [t["name"] for t in visitor] == ["reset-password"]
        # Operator (?operator=1): the full catalogue + the runbook.
        op = client.get("/catalog?queue=it&operator=1").json()["tools"]
        assert [t["name"] for t in op] == [
            "reset-password",
            "rotate-root-key",
            "skill:restart",
        ]


def test_me_reports_operator_in_dev_mode():
    with _client_with_mixed_catalog() as client:
        assert client.get("/me").json()["operator"] is False
        assert client.get("/me?operator=1").json()["operator"] is True


def test_visitor_cannot_request_operator_tool():
    store = FakeStore()
    with _client_with_mixed_catalog(store) as client:
        # A visitor submitting an operator-only tool is refused — nothing lands; a
        # normal message after it is the next (and only) frame back.
        with client.websocket_connect("/ws?queue=it&user=alice@corp") as alice:
            alice.send_json({"type": "request", "tool": "rotate-root-key", "args": {}})
            alice.send_json({"type": "message", "text": "hello?"})
            got = alice.receive_json()
            assert got["type"] == "message" and got["text"] == "hello?"
    assert all(m.kind == "chat" for m in store.appended)


def test_operator_can_request_operator_tool():
    store = FakeStore()
    with _client_with_mixed_catalog(store) as client:
        # The same operator-only tool is accepted for an operator session — it lands
        # as a structured request-kind message.
        with client.websocket_connect("/ws?queue=it&user=ops@corp&operator=1") as ops:
            ops.send_json({"type": "request", "tool": "rotate-root-key", "args": {}})
            got = ops.receive_json()
            assert got["type"] == "message" and got["kind"] == "request"
            assert got["data"]["tool"] == "rotate-root-key"
    assert store.appended and store.appended[0].kind == "request"


def test_visitor_can_request_self_service_skill():
    # A runbook the team published self-service is a normal self-service entry: a
    # visitor sees it and may submit it as a kind="skill" request.
    store = FakeStore()
    catalog = [
        {
            "name": "skill:reset-pw",
            "x-audience": "self-service",
            "x-kind": "skill",
            "x-title": "Reset my password",
            "inputSchema": {"type": "object", "properties": {}},
        }
    ]
    hub = Hub(store)
    hub.publish_catalog(_SeedAgent("agent@web01", "helpdesk/it"), catalog)
    with TestClient(create_app(hub)) as client:
        # The visitor sees it without operator access…
        tools = client.get("/catalog?queue=it").json()["tools"]
        assert [t["name"] for t in tools] == ["skill:reset-pw"]
        # …and submitting it lands as a request the console will enact as a skill.
        with client.websocket_connect("/ws?queue=it&user=alice@corp") as alice:
            alice.send_json(
                {
                    "type": "request",
                    "tool": "skill:reset-pw",
                    "args": {},
                    "kind": "skill",
                }
            )
            got = alice.receive_json()
            assert got["type"] == "message" and got["kind"] == "request"
            assert got["data"]["tool"] == "skill:reset-pw"
    assert store.appended and store.appended[0].kind == "request"


def test_visitor_request_over_ws_lands_and_auto_assigns():
    store = FakeStore()
    with _client_with_catalog(store) as client:
        with client.websocket_connect("/ws?queue=it&user=alice@corp") as alice:
            alice.send_json(
                {"type": "request", "tool": "reset-password", "args": {"user": "alice"}}
            )
            # The ask lands as a structured request-kind message…
            got = alice.receive_json()
            assert got["type"] == "message" and got["kind"] == "request"
            assert got["data"]["tool"] == "reset-password"
            assert got["text"].startswith("Requested: reset-password")
            # …and the hub auto-assigns it to the on-shift holder.
            status = alice.receive_json()
            assert status["type"] == "request_status" and status["state"] == "assigned"
            assert status["assignee"] == "agent@web01"
    saved = store.appended[0]
    assert saved.kind == "request" and saved.room == "helpdesk/it/alice@corp"


def test_visitor_request_for_unknown_tool_is_refused():
    store = FakeStore()
    with _client_with_catalog(store) as client:
        with client.websocket_connect("/ws?queue=it&user=alice@corp") as alice:
            alice.send_json({"type": "request", "tool": "rotate-root-key", "args": {}})
            # Nothing comes back for the refused request; a normal message after it
            # is the next frame the visitor sees (proving the socket stayed open).
            alice.send_json({"type": "message", "text": "hello?"})
            got = alice.receive_json()
            assert got["type"] == "message" and got["text"] == "hello?"
    assert all(m.kind == "chat" for m in store.appended)


# ── white-label branding (config's [branding]) ─────────────────────────────────

from console_room.config import Branding  # noqa: E402


def test_me_includes_branding_when_configured():
    hub = Hub(FakeStore())
    brand = Branding(brand_name="MyCorp Ops", accent_color="#b3001b", favicon_path="")
    with TestClient(create_app(hub, branding=brand)) as client:
        body = client.get("/me").json()
        # Only the browser-safe subset — never the favicon filesystem path.
        assert body["branding"] == {
            "brand_name": "MyCorp Ops",
            "accent_color": "#b3001b",
        }


def test_favicon_204_without_branding():
    with _client() as client:
        r = client.get("/favicon.ico")
        assert r.status_code == 204


def test_favicon_serves_configured_file(tmp_path):
    icon = tmp_path / "brand.ico"
    icon.write_bytes(b"ICODATA")
    hub = Hub(FakeStore())
    brand = Branding(favicon_path=str(icon))
    with TestClient(create_app(hub, branding=brand)) as client:
        r = client.get("/favicon.ico")
        assert r.status_code == 200 and r.content == b"ICODATA"


def test_favicon_204_when_configured_file_missing(tmp_path):
    hub = Hub(FakeStore())
    brand = Branding(favicon_path=str(tmp_path / "does-not-exist.ico"))
    with TestClient(create_app(hub, branding=brand)) as client:
        r = client.get("/favicon.ico")
        assert r.status_code == 204
