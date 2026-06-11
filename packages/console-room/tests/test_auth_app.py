"""
test_auth_app.py — the OIDC-gated browser door, end to end via TestClient.

No live IdP: the token exchange (oauth.oidc.authorize_access_token) is stubbed, so
the callback → session → gated-access path is exercised against a real
SessionMiddleware + real routing. Skipped if the web/auth stack isn't installed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")
pytest.importorskip("authlib")
pytest.importorskip("itsdangerous")

from starlette.testclient import TestClient  # noqa: E402

from console_room.hub import Hub  # noqa: E402
from console_room.models import Message  # noqa: E402
from console_room.web.app import create_app  # noqa: E402
from console_room.web.auth import AuthConfig  # noqa: E402


class FakeStore:
    def __init__(self):
        self.appended = []

    def append(self, msg: Message) -> None:
        self.appended.append(msg)

    def recent(self, room, limit):
        return []


def _auth() -> AuthConfig:
    return AuthConfig(
        client_id="cid",
        client_secret="csec",
        session_secret="unit-test-session-secret-0123456789",
        metadata_url="https://idp.example/.well-known/openid-configuration",
    )


def _app(store=None):
    return create_app(Hub(store or FakeStore()), auth=_auth())


def _stub_login(app, user="ana@example.com"):
    """Patch the token exchange so the callback establishes a session for ``user``
    without a real IdP round-trip."""

    async def fake_exchange(_request):
        return {"userinfo": {"email": user}}

    app.state.oauth.oidc.authorize_access_token = fake_exchange


# ── gating without a session ──────────────────────────────────────────────────


def test_index_redirects_to_login_when_unauthenticated():
    client = TestClient(_app())
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/login"


def test_me_reports_auth_enabled_no_user():
    with TestClient(_app()) as client:
        assert client.get("/me").json() == {"auth": True, "user": ""}


def test_healthz_is_public():
    with TestClient(_app()) as client:
        assert client.get("/healthz").json() == {"ok": True}


def test_ws_rejected_without_session():
    client = TestClient(_app())
    with pytest.raises(Exception):
        with client.websocket_connect("/ws?room=ops"):
            pass


# ── the login → session → access path ─────────────────────────────────────────


def test_callback_establishes_session_and_unlocks_app():
    app = _app()
    _stub_login(app, "ana@example.com")
    client = TestClient(app)

    r = client.get("/auth/callback", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"

    # The session cookie now identifies us.
    assert client.get("/me").json() == {"auth": True, "user": "ana@example.com"}
    # And the shell is served instead of redirecting.
    assert client.get("/", follow_redirects=False).status_code == 200


def test_ws_uses_session_identity_not_the_query():
    store = FakeStore()
    app = _app(store)
    _stub_login(app, "ana@example.com")
    client = TestClient(app)
    client.get("/auth/callback")  # establish the session cookie

    with client.websocket_connect("/ws?room=ops&user=imposter") as ws:
        ws.send_json({"type": "message", "room": "ops", "text": "hi"})
        got = ws.receive_json()
        assert got["type"] == "message" and got["text"] == "hi"
        # Identity comes from the session, never the spoofable query param.
        assert got["sender"] == "ana@example.com"
    assert store.appended[0].sender == "ana@example.com"


def test_logout_clears_session():
    app = _app()
    _stub_login(app)
    client = TestClient(app)
    client.get("/auth/callback")
    assert client.get("/me").json()["user"] == "ana@example.com"

    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/me").json()["user"] == ""
