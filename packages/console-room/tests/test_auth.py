"""
test_auth.py — the OIDC config + identity helpers (pure, dependency-free).

These don't import authlib (it's loaded lazily only in build_oauth), so they run
in a minimal environment. The gated-app behaviour is in test_auth_app.py.
"""

from __future__ import annotations

from console_room.web.auth import (
    AuthConfig,
    auth_from_env,
    identity_from_userinfo,
    session_user,
)

_ENV_KEYS = [
    "CONSOLE_ROOM_OIDC_CLIENT_ID",
    "CONSOLE_ROOM_OIDC_CLIENT_SECRET",
    "CONSOLE_ROOM_SESSION_SECRET",
    "CONSOLE_ROOM_OIDC_METADATA_URL",
    "CONSOLE_ROOM_OIDC_ISSUER",
    "CONSOLE_ROOM_OIDC_SCOPES",
    "CONSOLE_ROOM_PUBLIC_URL",
]


def _clear_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


# ── auth_from_env ─────────────────────────────────────────────────────────────


def test_auth_from_env_none_when_unconfigured(monkeypatch):
    _clear_env(monkeypatch)
    assert auth_from_env() is None


def test_auth_from_env_none_when_partial(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CONSOLE_ROOM_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("CONSOLE_ROOM_OIDC_CLIENT_SECRET", "csec")
    # missing session secret + issuer/metadata → still dev mode
    assert auth_from_env() is None


def test_auth_from_env_derives_metadata_from_issuer(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CONSOLE_ROOM_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("CONSOLE_ROOM_OIDC_CLIENT_SECRET", "csec")
    monkeypatch.setenv("CONSOLE_ROOM_SESSION_SECRET", "s3cr3t")
    monkeypatch.setenv("CONSOLE_ROOM_OIDC_ISSUER", "https://idp.example/")
    cfg = auth_from_env()
    assert cfg is not None
    assert cfg.metadata_url == "https://idp.example/.well-known/openid-configuration"
    assert cfg.client_id == "cid" and cfg.scopes == "openid email profile"


def test_auth_from_env_explicit_metadata_wins(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CONSOLE_ROOM_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("CONSOLE_ROOM_OIDC_CLIENT_SECRET", "csec")
    monkeypatch.setenv("CONSOLE_ROOM_SESSION_SECRET", "s3cr3t")
    monkeypatch.setenv("CONSOLE_ROOM_OIDC_ISSUER", "https://idp.example")
    monkeypatch.setenv("CONSOLE_ROOM_OIDC_METADATA_URL", "https://idp.example/meta")
    cfg = auth_from_env()
    assert cfg is not None and cfg.metadata_url == "https://idp.example/meta"


# ── AuthConfig behaviour ──────────────────────────────────────────────────────


def _cfg(**kw) -> AuthConfig:
    base = dict(
        client_id="cid",
        client_secret="csec",
        session_secret="s3cr3t",
        metadata_url="https://idp.example/meta",
    )
    base.update(kw)
    return AuthConfig(**base)


def test_redirect_uri_prefers_public_url():
    cfg = _cfg(public_url="https://room.example.com/")
    assert (
        cfg.redirect_uri("http://ignored/") == "https://room.example.com/auth/callback"
    )


def test_redirect_uri_falls_back_to_request_base():
    cfg = _cfg()
    assert cfg.redirect_uri("https://host:8080/") == "https://host:8080/auth/callback"


def test_secure_cookies_follows_public_url_scheme():
    assert _cfg(public_url="https://room.example.com").secure_cookies is True
    assert _cfg(public_url="http://localhost:8080").secure_cookies is False
    assert _cfg().secure_cookies is False  # no public_url → dev/http


# ── identity_from_userinfo ────────────────────────────────────────────────────


def test_identity_prefers_email_then_falls_through():
    assert identity_from_userinfo({"email": "a@b.com", "sub": "123"}) == "a@b.com"
    assert identity_from_userinfo({"preferred_username": "ana", "sub": "1"}) == "ana"
    assert identity_from_userinfo({"name": "Ana Q", "sub": "1"}) == "Ana Q"
    assert identity_from_userinfo({"sub": "abc"}) == "abc"


def test_identity_empty_when_no_claims():
    assert identity_from_userinfo({}) == ""
    assert identity_from_userinfo(None) == ""
    assert identity_from_userinfo({"email": "  "}) == ""  # blank ignored


# ── session_user ──────────────────────────────────────────────────────────────


class _Conn:
    def __init__(self, session) -> None:
        self.session = session


def test_session_user_reads_session():
    assert session_user(_Conn({"user": "ana@b.com"})) == "ana@b.com"


def test_session_name_reads_session():
    from console_room.web.auth import session_name

    assert session_name(_Conn({"name": "Ana B"})) == "Ana B"
    assert session_name(_Conn({})) == ""  # no name claim
    assert session_name(object()) == ""  # no session at all


def test_session_user_safe_without_session():
    assert session_user(_Conn(None)) == ""
    assert session_user(object()) == ""  # no .session attr at all
    assert session_user(_Conn({})) == ""  # session present, no user


def test_session_user_safe_when_session_property_raises():
    # Starlette's request.session asserts SessionMiddleware is installed (dev mode
    # has none) — accessing it raises AssertionError, which must not 500 /me.
    class _NoMiddleware:
        @property
        def session(self):
            raise AssertionError("SessionMiddleware must be installed")

    assert session_user(_NoMiddleware()) == ""
