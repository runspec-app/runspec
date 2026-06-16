"""
test_signup_web.py — the email-verify login door end to end via TestClient.

Exercises /teams, /login (GET form + POST), the /signup→/login redirect, and
/verify against a real SessionMiddleware + routing: the domain allowlist, the
construction guards, the signed single-use verify token, the access-TTL session it
establishes, the remembered-email cookie + one-click "welcome back" re-login, and
that the verified session unlocks the gated /catalog + /me. The dispatch of verify
mail to a console (the outbox flush + send_verify frame) is covered in
test_signup_dispatch; here a pending row is seeded directly so /verify can be driven
without a console. Skipped if the web stack isn't installed.
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
    Member,
    Team,
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


def _build():
    """An email-verify app whose hub serves the ``helpdesk@corp.com`` mailbox (the
    sign-up domain) and an extra ``IT Support`` team beside the seeded Default.
    Returns (client, hub, enrollment)."""
    enrollment = EnrollmentStore(":memory:")
    enrollment.add_team(Team(slug="it-support", name="IT Support"))
    hub = Hub(
        FakeStore(),
        enrollment=enrollment,
        email_verify=_config(),
        mailbox="helpdesk@corp.com",
    )
    app = create_app(hub, email_verify=_config())
    return TestClient(app), hub, enrollment


def _seed_pending(enrollment, nonce="n1", email="jane@corp.com", queue="helpdesk"):
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


# ── /teams + /signup form ─────────────────────────────────────────────────────


def test_teams_lists_default_and_added():
    client, *_ = _build()
    body = client.get("/teams").json()
    assert body["teams"] == [
        {"slug": "default", "name": "Default"},
        {"slug": "it-support", "name": "IT Support"},
    ]


def test_login_form_renders_team_options():
    client, *_ = _build()
    r = client.get("/login")
    assert r.status_code == 200
    # The team dropdown + the mailbox domain hint render.
    assert "IT Support" in r.text and 'value="it-support"' in r.text
    assert "Default" in r.text and "corp.com" in r.text
    assert "Send login link" in r.text


def test_signup_path_redirects_to_login():
    # The old /signup name is kept as a redirect for existing links / docs.
    client, *_ = _build()
    r = client.get("/signup", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/login"


def test_index_redirects_to_login_when_unauthenticated():
    client, *_ = _build()
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/login"


def test_me_reports_auth_enabled():
    client, *_ = _build()
    assert client.get("/me").json() == {
        "auth": True,
        "user": "",
        "name": "",
        "team": "",
        "queue": "helpdesk",
    }


def test_lifespan_starts_and_stops_cleanly():
    # The context manager runs the app lifespan (which launches the expiry sweeper
    # in email-verify mode and cancels it on shutdown) — exercise that path.
    client, *_ = _build()
    with client:
        assert client.get("/healthz").json() == {"ok": True}


# ── POST /login: validation + outbox ──────────────────────────────────────────


def test_login_rejects_foreign_domain():
    client, _hub, enrollment = _build()
    r = client.post(
        "/login",
        data={"team": "it-support", "name": "Jane", "email": "jane@evil.com"},
    )
    assert r.status_code == 400
    assert "corp.com" in r.text  # tells them which domain to use
    # Nothing written to the outbox.
    assert enrollment.claim_pending_sends("helpdesk", "x") == []


def test_login_rejects_unknown_queue():
    client, *_ = _build()
    r = client.post(
        "/login",
        data={"team": "ghost", "name": "Jane", "email": "jane@corp.com"},
    )
    assert r.status_code == 400


def test_login_rejects_blank_name():
    client, *_ = _build()
    r = client.post(
        "/login",
        data={"team": "it-support", "name": "  ", "email": "jane@corp.com"},
    )
    assert r.status_code == 400


def test_login_accepts_and_writes_outbox():
    client, _hub, enrollment = _build()
    r = client.post(
        "/login",
        data={"team": "it-support", "name": "Jane Doe", "email": "Jane@Corp.com"},
    )
    assert r.status_code == 200 and "Check your email" in r.text
    # A pending row is in the outbox, email normalised to lower-case.
    pending = enrollment.claim_pending_sends("helpdesk", "probe")
    assert [p.email for p in pending] == ["jane@corp.com"]


# ── /verify: token, single-use, session ───────────────────────────────────────


def test_verify_enrolls_and_sets_session():
    client, _hub, enrollment = _build()
    token = _seed_pending(enrollment)

    r = client.get(f"/verify?token={token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?queue=helpdesk"
    assert enrollment.is_member("jane@corp.com", "helpdesk")
    # The session now identifies the verified email — the gate unlocks.
    assert client.get("/me").json() == {
        "auth": True,
        "user": "jane@corp.com",
        "name": "Jane Doe",
        "team": "",
        "queue": "helpdesk",
    }
    assert client.get("/", follow_redirects=False).status_code == 200


def test_session_cookie_carries_access_ttl():
    # Access expiry is the session cookie's Max-Age (signed server-side). A short
    # session_ttl must propagate to the Set-Cookie so the browser drops it on time.
    enrollment = EnrollmentStore(":memory:")
    cfg = EmailVerifyConfig(
        session_secret=SECRET,
        public_url="http://room.test",
        token_ttl=1800,
        session_ttl=120,
    )
    hub = Hub(
        FakeStore(),
        enrollment=enrollment,
        email_verify=cfg,
        mailbox="helpdesk@corp.com",
    )
    client = TestClient(create_app(hub, email_verify=cfg))
    token = _seed_pending(enrollment)
    r = client.get(f"/verify?token={token}", follow_redirects=False)
    session_cookie = [
        c for c in r.headers.get_list("set-cookie") if c.startswith("session=")
    ][0]
    assert "Max-Age=120" in session_cookie


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


# ── remembered email + one-click "welcome back" re-login ──────────────────────


def _remember(email):
    """The remembered-email cookie as an explicit request header — the httpx test
    jar won't re-send a host-only cookie set by hand, so drive it directly (the
    real browser round-trips the Set-Cookie from /verify, exercised below)."""
    return {"Cookie": f"room_email={email}"}


def _add_member(enrollment, email="jane@corp.com", name="Jane Doe", team="it-support"):
    enrollment.add_member(
        Member(email=email, queue="helpdesk", name=name, verified_at=0.0, team=team)
    )


def test_verify_sets_remembered_email_cookie():
    client, _hub, enrollment = _build()
    token = _seed_pending(enrollment)
    r = client.get(f"/verify?token={token}", follow_redirects=False)
    assert r.status_code == 303
    # The proven email is remembered (Set-Cookie) so next login is one click.
    set_cookie = r.headers["set-cookie"]
    assert "room_email=" in set_cookie and "jane@corp.com" in set_cookie


def test_login_offers_one_click_for_known_member():
    client, _hub, enrollment = _build()
    _add_member(enrollment)  # a verified member with a still-valid team
    r = client.get("/login", headers=_remember("jane@corp.com"))
    assert "Welcome back" in r.text and "jane@corp.com" in r.text
    assert 'name="mode" value="known"' in r.text
    assert "Use a different email" in r.text


def test_known_member_one_click_post_reuses_name_and_team():
    client, _hub, enrollment = _build()
    _add_member(enrollment)
    r = client.post(
        "/login", data={"mode": "known"}, headers=_remember("jane@corp.com")
    )
    assert r.status_code == 200 and "Check your email" in r.text
    pending = enrollment.claim_pending_sends("helpdesk", "probe")
    assert len(pending) == 1
    assert pending[0].email == "jane@corp.com"
    assert pending[0].name == "Jane Doe" and pending[0].team == "it-support"


def test_login_one_click_ignored_without_known_member():
    # mode=known but the cookie email isn't a member → falls back to the full form
    # (which rejects an empty submission), never a blind submit.
    client, _hub, enrollment = _build()
    r = client.post(
        "/login", data={"mode": "known"}, headers=_remember("ghost@corp.com")
    )
    assert r.status_code == 400
    assert enrollment.claim_pending_sends("helpdesk", "probe") == []


def test_login_switch_shows_full_form_for_known_member():
    client, _hub, enrollment = _build()
    _add_member(enrollment)
    r = client.get("/login?switch=1", headers=_remember("jane@corp.com"))
    assert "Welcome back" not in r.text
    assert "Send login link" in r.text and "Choose your team" in r.text


def test_login_full_form_when_member_team_removed():
    # The stored team no longer exists (config sync dropped it) → no stale one-click;
    # show the full form so the visitor re-picks a current team.
    client, _hub, enrollment = _build()
    _add_member(enrollment, team="ghost-team")
    r = client.get("/login", headers=_remember("jane@corp.com"))
    assert "Welcome back" not in r.text and "Choose your team" in r.text


def test_login_prefills_remembered_email_for_unknown_member():
    # Cookie present but no member record → full form, email pre-filled.
    client, *_ = _build()
    r = client.get("/login", headers=_remember("ghost@corp.com"))
    assert "Welcome back" not in r.text
    assert 'value="ghost@corp.com"' in r.text


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
