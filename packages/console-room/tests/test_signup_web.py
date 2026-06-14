"""
test_signup_web.py — the email-verify sign-up door end to end via TestClient.

Exercises /queues, /signup (GET form + POST), and /verify against a real
SessionMiddleware + routing: the domain allowlist, the durable outbox row, the
signed single-use verify token, the session it establishes, and that the verified
session then unlocks the gated /catalog + /me. The verify mail "send" is a captured
stub (the real Outlook send is slice 4). Skipped if the web stack isn't installed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")
pytest.importorskip("itsdangerous")

from starlette.testclient import TestClient  # noqa: E402

from console_room.enrollment_store import EnrollmentStore  # noqa: E402
from console_room.hub import Hub  # noqa: E402
from console_room.signup import (  # noqa: E402
    EmailVerifyConfig,
    PendingSignup,
    sign_verify_token,
)
from console_room.web.app import create_app  # noqa: E402


class FakeStore:
    def append(self, msg):
        pass

    def recent(self, room, limit):
        return []


def _config():
    return EmailVerifyConfig(
        session_secret="unit-test-session-secret-0123456789",
        public_url="",
        token_ttl=1800,
    )


def _build(*, advertise=True, capture=True):
    """An email-verify app with an it-support queue advertised and (optionally) a
    capturing verify sender. Returns (client, hub, enrollment, sent)."""
    hub = Hub(FakeStore())
    if advertise:
        hub.advertise_queue(
            "agent@web01", "it-support", "it-support@corp.com", "IT Support"
        )
    enrollment = EnrollmentStore(":memory:")
    sent: list = []

    async def sender(send):
        sent.append(send)
        return True

    app = create_app(
        hub,
        auth=None,
        email_verify=_config(),
        enrollment=enrollment,
        verify_sender=sender if capture else None,
    )
    return TestClient(app), hub, enrollment, sent


# ── construction guards ───────────────────────────────────────────────────────


def test_email_verify_requires_enrollment_store():
    with pytest.raises(ValueError):
        create_app(Hub(FakeStore()), email_verify=_config())


def test_oidc_and_email_verify_are_mutually_exclusive():
    from console_room.web.auth import AuthConfig

    auth = AuthConfig(
        "c", "s", "secret", "https://idp/.well-known/openid-configuration"
    )
    with pytest.raises(ValueError):
        create_app(
            Hub(FakeStore()),
            auth=auth,
            email_verify=_config(),
            enrollment=EnrollmentStore(":memory:"),
        )


# ── /queues + /signup form ────────────────────────────────────────────────────


def test_queues_lists_advertised():
    client, *_ = _build()
    body = client.get("/queues").json()
    assert body["queues"] == [
        {"queue": "it-support", "label": "IT Support", "domain": "corp.com"}
    ]


def test_signup_form_renders_with_options():
    client, *_ = _build()
    r = client.get("/signup")
    assert r.status_code == 200
    assert "IT Support" in r.text and "it-support" in r.text


def test_signup_form_when_no_queues():
    client, *_ = _build(advertise=False)
    r = client.get("/signup")
    assert r.status_code == 200
    assert "No queues available" in r.text


def test_index_redirects_to_signup_when_unauthenticated():
    client, *_ = _build()
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/signup"


def test_me_reports_auth_enabled():
    client, *_ = _build()
    assert client.get("/me").json() == {"auth": True, "user": ""}


# ── POST /signup: validation + outbox ─────────────────────────────────────────


def test_signup_rejects_foreign_domain():
    client, _hub, enrollment, sent = _build()
    r = client.post(
        "/signup",
        data={"queue": "it-support", "name": "Jane", "email": "jane@evil.com"},
    )
    assert r.status_code == 400
    assert "corp.com" in r.text  # tells them which domain to use
    assert sent == []  # nothing emailed


def test_signup_rejects_unknown_queue():
    client, *_ = _build()
    r = client.post(
        "/signup",
        data={"queue": "ghost", "name": "Jane", "email": "jane@corp.com"},
    )
    assert r.status_code == 400


def test_signup_rejects_blank_name():
    client, *_ = _build()
    r = client.post(
        "/signup",
        data={"queue": "it-support", "name": "  ", "email": "jane@corp.com"},
    )
    assert r.status_code == 400


def test_signup_accepts_and_sends_verify():
    client, _hub, enrollment, sent = _build()
    r = client.post(
        "/signup",
        data={"queue": "it-support", "name": "Jane Doe", "email": "Jane@Corp.com"},
    )
    assert r.status_code == 200 and "Check your email" in r.text
    assert len(sent) == 1
    send = sent[0]
    assert send.to == "jane@corp.com"  # normalised lower-case
    assert send.queue_email == "it-support@corp.com"
    assert "/verify?token=" in send.verify_url


def test_signup_without_sender_leaves_pending_in_outbox():
    # No console send wired (slice 2 default) → the row waits in the outbox.
    client, _hub, enrollment, _sent = _build(capture=False)
    r = client.post(
        "/signup",
        data={"queue": "it-support", "name": "Jane", "email": "jane@corp.com"},
    )
    assert r.status_code == 200
    pending = enrollment.claim_pending_sends("it-support", "console-x")
    assert [p.email for p in pending] == ["jane@corp.com"]
    assert pending[0].send_state == "claimed"


# ── /verify: token, single-use, session ───────────────────────────────────────


def _token_from_signup(client, sent):
    client.post(
        "/signup",
        data={"queue": "it-support", "name": "Jane Doe", "email": "jane@corp.com"},
    )
    url = sent[-1].verify_url
    return url.split("token=", 1)[1]


def test_verify_enrolls_and_sets_session():
    client, _hub, enrollment, sent = _build()
    token = _token_from_signup(client, sent)

    r = client.get(f"/verify?token={token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?queue=it-support"
    # Persisted member.
    assert enrollment.is_member("jane@corp.com", "it-support")
    # The session now identifies the verified email — the gate unlocks.
    assert client.get("/me").json() == {"auth": True, "user": "jane@corp.com"}
    assert client.get("/", follow_redirects=False).status_code == 200


def test_verify_is_single_use():
    client, _hub, _enrollment, sent = _build()
    token = _token_from_signup(client, sent)
    assert (
        client.get(f"/verify?token={token}", follow_redirects=False).status_code == 303
    )
    # A second click finds the nonce already burned.
    r2 = client.get(f"/verify?token={token}", follow_redirects=False)
    assert r2.status_code == 400 and "expired or already used" in r2.text.lower()


def test_verify_rejects_garbage_token():
    client, *_ = _build()
    r = client.get("/verify?token=not-a-real-token", follow_redirects=False)
    assert r.status_code == 400


def test_verify_rejects_foreign_secret_token():
    client, _hub, enrollment, _sent = _build()
    # A token signed with the wrong secret must not verify even if a row exists.
    enrollment.add_pending(
        PendingSignup(
            nonce="n1",
            email="jane@corp.com",
            name="Jane",
            queue="it-support",
            created_at=0.0,
        )
    )
    forged = sign_verify_token("wrong-secret", {"nonce": "n1"})
    r = client.get(f"/verify?token={forged}", follow_redirects=False)
    assert r.status_code == 400
    assert enrollment.get_pending("n1") is not None  # row untouched


def test_verified_session_unlocks_catalog():
    client, hub, _enrollment, sent = _build()
    token = _token_from_signup(client, sent)
    # Before verify, the gated catalog is 401.
    fresh = TestClient(
        create_app(hub, email_verify=_config(), enrollment=EnrollmentStore(":memory:"))
    )
    assert fresh.get("/catalog?queue=it-support").status_code == 401
    # After verify, this client's session passes the gate.
    client.get(f"/verify?token={token}")
    r = client.get("/catalog?queue=it-support")
    assert r.status_code == 200 and r.json()["queue"] == "it-support"
