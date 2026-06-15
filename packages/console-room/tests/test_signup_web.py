"""
test_signup_web.py — the email-verify sign-up door end to end via TestClient.

Exercises /queues, /signup (GET form + POST), and /verify against a real
SessionMiddleware + routing: the domain allowlist, the construction guards, the
signed single-use verify token, the session it establishes, and that the verified
session then unlocks the gated /catalog + /me. The dispatch of verify mail to a
console (the outbox flush + send_verify frame) is covered in test_signup_dispatch;
here a pending row is seeded directly so /verify can be driven without a console.
Skipped if the web stack isn't installed.
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

SECRET = "unit-test-session-secret-0123456789"


class FakeStore:
    def append(self, msg):
        pass

    def recent(self, room, limit):
        return []


def _config(public_url="http://room.test"):
    # http (not https) so SessionMiddleware doesn't mark the cookie Secure — the
    # TestClient speaks plain http and a Secure cookie wouldn't round-trip.
    return EmailVerifyConfig(
        session_secret=SECRET, public_url=public_url, token_ttl=1800
    )


def _build(*, advertise=True):
    """An email-verify app whose hub has an it-support queue advertised. Returns
    (client, hub, enrollment)."""
    enrollment = EnrollmentStore(":memory:")
    hub = Hub(FakeStore(), enrollment=enrollment, email_verify=_config())
    if advertise:
        hub.advertise_queue(
            "agent@web01", "it-support", "it-support@corp.com", "IT Support"
        )
    app = create_app(hub, email_verify=_config())
    return TestClient(app), hub, enrollment


def _seed_pending(enrollment, nonce="n1", email="jane@corp.com", queue="it-support"):
    enrollment.add_pending(
        PendingSignup(
            nonce=nonce, email=email, name="Jane Doe", queue=queue, created_at=0.0
        )
    )
    return sign_verify_token(SECRET, {"nonce": nonce})


# ── construction guards ───────────────────────────────────────────────────────


def test_email_verify_requires_enabled_hub():
    # A hub without the enrollment store / config can't serve email-verify.
    with pytest.raises(ValueError):
        create_app(Hub(FakeStore()), email_verify=_config())


def test_email_verify_requires_public_url():
    enrollment = EnrollmentStore(":memory:")
    hub = Hub(FakeStore(), enrollment=enrollment, email_verify=_config(public_url=""))
    with pytest.raises(ValueError):
        create_app(hub, email_verify=_config(public_url=""))


def test_email_verify_requires_enrollment_hub():
    # email_verify mode needs a Hub built with the enrollment store + config.
    hub = Hub(FakeStore())  # no enrollment → not email_verify_enabled
    with pytest.raises(ValueError):
        create_app(hub, email_verify=_config())


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


def test_lifespan_starts_and_stops_cleanly():
    # The context manager runs the app lifespan (which launches the expiry sweeper
    # in email-verify mode and cancels it on shutdown) — exercise that path.
    client, *_ = _build()
    with client:
        assert client.get("/healthz").json() == {"ok": True}


# ── POST /signup: validation + outbox ─────────────────────────────────────────


def test_signup_rejects_foreign_domain():
    client, _hub, enrollment = _build()
    r = client.post(
        "/signup",
        data={"queue": "it-support", "name": "Jane", "email": "jane@evil.com"},
    )
    assert r.status_code == 400
    assert "corp.com" in r.text  # tells them which domain to use
    # Nothing written to the outbox.
    assert enrollment.claim_pending_sends("it-support", "x") == []


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


def test_signup_accepts_and_writes_outbox():
    client, _hub, enrollment = _build()
    r = client.post(
        "/signup",
        data={"queue": "it-support", "name": "Jane Doe", "email": "Jane@Corp.com"},
    )
    assert r.status_code == 200 and "Check your email" in r.text
    # A pending row is in the outbox, email normalised to lower-case.
    pending = enrollment.claim_pending_sends("it-support", "probe")
    assert [p.email for p in pending] == ["jane@corp.com"]


# ── /verify: token, single-use, session ───────────────────────────────────────


def test_verify_enrolls_and_sets_session():
    client, _hub, enrollment = _build()
    token = _seed_pending(enrollment)

    r = client.get(f"/verify?token={token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?queue=it-support"
    assert enrollment.is_member("jane@corp.com", "it-support")
    # The session now identifies the verified email — the gate unlocks.
    assert client.get("/me").json() == {"auth": True, "user": "jane@corp.com"}
    assert client.get("/", follow_redirects=False).status_code == 200


def test_verify_is_single_use():
    client, _hub, enrollment = _build()
    token = _seed_pending(enrollment)
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
    client, _hub, enrollment = _build()
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
    client, hub, enrollment = _build()
    token = _seed_pending(enrollment)
    # A fresh (unverified) client is 401 on the gated catalog.
    fresh_enroll = EnrollmentStore(":memory:")
    fresh_hub = Hub(FakeStore(), enrollment=fresh_enroll, email_verify=_config())
    fresh = TestClient(create_app(fresh_hub, email_verify=_config()))
    assert fresh.get("/catalog?queue=it-support").status_code == 401
    # After verify, this client's session passes the gate.
    client.get(f"/verify?token={token}")
    r = client.get("/catalog?queue=it-support")
    assert r.status_code == 200 and r.json()["queue"] == "it-support"
