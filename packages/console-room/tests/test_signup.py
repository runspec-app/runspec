"""
test_signup.py — the pure half of email-verify auth: config, domain allowlist,
and the signed single-use verify token.
"""

from __future__ import annotations

import pytest

from console_room.signup import (
    DEFAULT_SESSION_TTL,
    DEFAULT_TOKEN_TTL,
    EmailVerifyConfig,
    domain_allowed,
    email_domain,
    email_verify_from_env,
    login_domains,
    make_nonce,
    make_team_slug,
    normalize_domain,
    parse_domains,
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
    monkeypatch.setenv("CONSOLE_ROOM_AUTH", "dev")
    assert read_auth_mode() == "dev"


def test_read_auth_mode_defaults_to_email_verify_with_secret(monkeypatch):
    # No explicit mode but a session secret is set → email-verify is the default.
    monkeypatch.delenv("CONSOLE_ROOM_AUTH", raising=False)
    monkeypatch.setenv("CONSOLE_ROOM_SESSION_SECRET", "s3cr3t")
    assert read_auth_mode() == "email-verify"


def test_read_auth_mode_dev_when_no_secret(monkeypatch):
    monkeypatch.delenv("CONSOLE_ROOM_AUTH", raising=False)
    monkeypatch.delenv("CONSOLE_ROOM_SESSION_SECRET", raising=False)
    assert read_auth_mode() == "dev"


def test_read_auth_mode_unknown_value_falls_back(monkeypatch):
    monkeypatch.setenv("CONSOLE_ROOM_AUTH", "nonsense")
    monkeypatch.delenv("CONSOLE_ROOM_SESSION_SECRET", raising=False)
    assert read_auth_mode() == "dev"


def test_email_verify_from_env_needs_secret(monkeypatch):
    monkeypatch.delenv("CONSOLE_ROOM_SESSION_SECRET", raising=False)
    assert email_verify_from_env() is None


def test_email_verify_from_env_reads_values(monkeypatch):
    monkeypatch.setenv("CONSOLE_ROOM_SESSION_SECRET", "shh")
    monkeypatch.setenv("CONSOLE_ROOM_PUBLIC_URL", "https://room.corp.com")
    monkeypatch.setenv("CONSOLE_ROOM_VERIFY_TTL", "600")
    monkeypatch.setenv("CONSOLE_ROOM_SESSION_TTL", "3600")
    cfg = email_verify_from_env()
    assert cfg is not None
    assert cfg.session_secret == "shh"
    assert cfg.public_url == "https://room.corp.com"
    assert cfg.token_ttl == 600
    assert cfg.session_ttl == 3600


def test_email_verify_from_env_session_ttl_defaults(monkeypatch):
    monkeypatch.setenv("CONSOLE_ROOM_SESSION_SECRET", "shh")
    monkeypatch.delenv("CONSOLE_ROOM_SESSION_TTL", raising=False)
    cfg = email_verify_from_env()
    assert cfg is not None and cfg.session_ttl == DEFAULT_SESSION_TTL


def test_email_verify_from_env_bad_session_ttl_falls_back(monkeypatch):
    monkeypatch.setenv("CONSOLE_ROOM_SESSION_SECRET", "shh")
    monkeypatch.setenv("CONSOLE_ROOM_SESSION_TTL", "-5")
    cfg = email_verify_from_env()
    assert cfg is not None and cfg.session_ttl == DEFAULT_SESSION_TTL


def test_email_verify_from_env_bad_ttl_falls_back(monkeypatch):
    monkeypatch.setenv("CONSOLE_ROOM_SESSION_SECRET", "shh")
    monkeypatch.delenv("CONSOLE_ROOM_PUBLIC_URL", raising=False)
    monkeypatch.setenv("CONSOLE_ROOM_VERIFY_TTL", "not-a-number")
    cfg = email_verify_from_env()
    assert cfg is not None and cfg.token_ttl == DEFAULT_TOKEN_TTL


def test_email_verify_from_env_allowed_domains(monkeypatch):
    monkeypatch.setenv("CONSOLE_ROOM_SESSION_SECRET", "shh")
    monkeypatch.setenv(
        "CONSOLE_ROOM_ALLOWED_DOMAINS", "@company1.com, company1.company2.com"
    )
    cfg = email_verify_from_env()
    assert cfg is not None
    assert cfg.allowed_domains == ("company1.com", "company1.company2.com")


def test_email_verify_from_env_allowed_domains_default_empty(monkeypatch):
    monkeypatch.setenv("CONSOLE_ROOM_SESSION_SECRET", "shh")
    monkeypatch.delenv("CONSOLE_ROOM_ALLOWED_DOMAINS", raising=False)
    cfg = email_verify_from_env()
    assert cfg is not None and cfg.allowed_domains == ()


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


@pytest.mark.parametrize(
    "value,expected",
    [
        ("@company1.com", "company1.com"),
        ("  Company1.COM  ", "company1.com"),
        ("@ Company2.com", "company2.com"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_domain(value, expected):
    assert normalize_domain(value) == expected


def test_parse_domains_splits_normalizes_and_dedupes():
    raw = "@company1.com, company1.company2.com  company2.com, company1.com"
    assert parse_domains(raw) == [
        "company1.com",
        "company1.company2.com",
        "company2.com",
    ]
    assert parse_domains("") == []
    assert parse_domains("   ") == []


def test_login_domains_mailbox_first_then_extras():
    # The mailbox's own domain is always first; extras follow, deduped.
    assert login_domains(
        "shared@company2.com", ["company1.company2.com", "company1.com", "company2.com"]
    ) == ["company2.com", "company1.company2.com", "company1.com"]
    # No extras → just the mailbox domain (the prior behaviour).
    assert login_domains("it-support@corp.com") == ["corp.com"]


def test_domain_allowed_with_allowlist():
    # The single monitored mailbox stays shared@company2.com; users on the variants
    # are admitted by the allowlist.
    mailbox = "shared@company2.com"
    extras = ["company1.company2.com", "company1.com", "company2.com"]
    assert domain_allowed("my.email@company1.company2.com", mailbox, extras) is True
    assert domain_allowed("bob@company1.com", mailbox, extras) is True
    assert domain_allowed("ann@company2.com", mailbox, extras) is True
    # The mailbox domain is still allowed even when not repeated in the list.
    assert domain_allowed("ceo@company2.com", mailbox, ["company1.com"]) is True
    # A domain outside both the mailbox and the allowlist is still rejected.
    assert domain_allowed("evil@elsewhere.com", mailbox, extras) is False
    # Allowlist entries may carry a leading '@'.
    assert domain_allowed("bob@company1.com", mailbox, ["@company1.com"]) is True


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


def test_make_team_slug():
    assert make_team_slug("Team One") == "team-one"
    assert make_team_slug("  HR / Payroll!  ") == "hr-payroll"
    assert make_team_slug("Café 42") == "caf-42"  # non-ascii dropped
    assert make_team_slug("---") == ""  # nothing usable
    assert make_team_slug("Already-Slug") == "already-slug"
