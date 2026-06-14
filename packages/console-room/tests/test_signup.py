"""
test_signup.py — the pure half of email-verify auth: config, domain allowlist,
and the signed single-use verify token.
"""

from __future__ import annotations

import pytest

from console_room import signup
from console_room.signup import (
    DEFAULT_TOKEN_TTL,
    EmailVerifyConfig,
    domain_allowed,
    email_domain,
    email_verify_from_env,
    make_nonce,
    read_auth_mode,
    read_verify_token,
    sign_verify_token,
)


# ── config / env ──────────────────────────────────────────────────────────────


def test_secure_cookies_follows_https_public_url():
    assert EmailVerifyConfig("s", "https://room.corp.com").secure_cookies is True
    assert EmailVerifyConfig("s", "http://room.local").secure_cookies is False
    assert EmailVerifyConfig("s").secure_cookies is False  # no public url


def test_read_auth_mode_explicit(monkeypatch):
    monkeypatch.setenv("CONSOLE_ROOM_AUTH", "email-verify")
    assert read_auth_mode() == "email-verify"
    # underscore alias normalises to the hyphen form
    monkeypatch.setenv("CONSOLE_ROOM_AUTH", "email_verify")
    assert read_auth_mode() == "email-verify"
    monkeypatch.setenv("CONSOLE_ROOM_AUTH", "OIDC")
    assert read_auth_mode() == "oidc"


def test_read_auth_mode_infers_when_unset(monkeypatch):
    monkeypatch.delenv("CONSOLE_ROOM_AUTH", raising=False)
    # No OIDC configured → dev.
    monkeypatch.setattr(signup, "_env", lambda name: "")

    def _no_oidc():
        return None

    import console_room.web.auth as auth

    monkeypatch.setattr(auth, "auth_from_env", _no_oidc)
    assert read_auth_mode() == "dev"


def test_read_auth_mode_unknown_value_infers(monkeypatch):
    monkeypatch.setenv("CONSOLE_ROOM_AUTH", "nonsense")
    import console_room.web.auth as auth

    monkeypatch.setattr(auth, "auth_from_env", lambda: None)
    assert read_auth_mode() == "dev"


def test_email_verify_from_env_needs_secret(monkeypatch):
    monkeypatch.delenv("CONSOLE_ROOM_SESSION_SECRET", raising=False)
    assert email_verify_from_env() is None


def test_email_verify_from_env_reads_values(monkeypatch):
    monkeypatch.setenv("CONSOLE_ROOM_SESSION_SECRET", "shh")
    monkeypatch.setenv("CONSOLE_ROOM_PUBLIC_URL", "https://room.corp.com")
    monkeypatch.setenv("CONSOLE_ROOM_VERIFY_TTL", "600")
    cfg = email_verify_from_env()
    assert cfg is not None
    assert cfg.session_secret == "shh"
    assert cfg.public_url == "https://room.corp.com"
    assert cfg.token_ttl == 600


def test_email_verify_from_env_bad_ttl_falls_back(monkeypatch):
    monkeypatch.setenv("CONSOLE_ROOM_SESSION_SECRET", "shh")
    monkeypatch.delenv("CONSOLE_ROOM_PUBLIC_URL", raising=False)
    monkeypatch.setenv("CONSOLE_ROOM_VERIFY_TTL", "not-a-number")
    cfg = email_verify_from_env()
    assert cfg is not None and cfg.token_ttl == DEFAULT_TOKEN_TTL


def test_email_verify_from_env_nonpositive_ttl_falls_back(monkeypatch):
    monkeypatch.setenv("CONSOLE_ROOM_SESSION_SECRET", "shh")
    monkeypatch.setenv("CONSOLE_ROOM_VERIFY_TTL", "0")
    cfg = email_verify_from_env()
    assert cfg is not None and cfg.token_ttl == DEFAULT_TOKEN_TTL


# ── domain allowlist ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "addr,expected",
    [
        ("Jane@Corp.com", "corp.com"),
        ("  jane@corp.com  ", "corp.com"),
        ("jane@", ""),
        ("@corp.com", ""),
        ("janecorp.com", ""),
        ("a@b@c", ""),
        ("", ""),
    ],
)
def test_email_domain(addr, expected):
    assert email_domain(addr) == expected


def test_domain_allowed_same_domain():
    assert domain_allowed("jane@corp.com", "it-support@corp.com") is True
    # case-insensitive
    assert domain_allowed("Jane@CORP.com", "it-support@corp.com") is True


def test_domain_allowed_rejects_foreign_domain():
    assert domain_allowed("jane@evil.com", "it-support@corp.com") is False


def test_domain_allowed_rejects_malformed():
    assert domain_allowed("jane", "it-support@corp.com") is False
    assert domain_allowed("jane@corp.com", "not-an-email") is False
    assert domain_allowed("", "") is False


# ── verify token ──────────────────────────────────────────────────────────────


def test_token_round_trip():
    secret = "topsecret"
    payload = {"nonce": "abc", "email": "jane@corp.com", "queue": "it-support"}
    token = sign_verify_token(secret, payload)
    assert read_verify_token(secret, token, max_age=DEFAULT_TOKEN_TTL) == payload


def test_token_rejected_with_wrong_secret():
    token = sign_verify_token("right", {"nonce": "abc"})
    assert read_verify_token("wrong", token, max_age=DEFAULT_TOKEN_TTL) is None


def test_token_rejected_when_expired():
    token = sign_verify_token("s", {"nonce": "abc"})
    # A negative max_age makes any token (age >= 0) already expired —
    # deterministic regardless of itsdangerous's 1-second timestamp granularity.
    assert read_verify_token("s", token, max_age=-1) is None


def test_token_rejected_when_garbage():
    assert read_verify_token("s", "not-a-real-token", max_age=10) is None
    assert read_verify_token("s", "", max_age=10) is None


def test_make_nonce_is_unique():
    assert make_nonce() != make_nonce()
    assert len(make_nonce()) == 32  # uuid4 hex
