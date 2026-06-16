"""
signup.py — email-verified queue signup: tokens, domain check, config (pure).

The pure half of the email-verify auth mode (see docs/design/email-queue-signup.md).
An internal employee signs up for a queue with their email + name; the room emails
a verify link (sent *by a console* as the queue mailbox); clicking it enrolls them.
This module holds the parts with no I/O — the signed verify token, the
domain-allowlist check (the allowlist *is* the queue mailbox's domain), and the
small dataclasses — so they're unit-tested on their own. Storage lives in
``enrollment_store.py``; the web routes in ``web/app.py`` (slice 2).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

# Salt namespaces the verify token so it can't be confused with another use of the
# same session secret (the signed session cookie).
_TOKEN_SALT = "console-room-email-verify"

#: Default verify-link lifetime (seconds). The link only confirms an address, so a
#: generous-but-bounded window is fine; single use is enforced by the DB nonce.
DEFAULT_TOKEN_TTL = 1800  # 30 minutes

#: Default access lifetime (seconds) before a member must log in again. The login
#: session is deliberately *not* permanent: access expires and is renewed via the
#: same email-link mechanism, so a fleet of rooms (each its own login) feels normal.
#: Enforced server-side as the session cookie's signed ``max_age``.
DEFAULT_SESSION_TTL = 86400  # 24 hours

#: Auth modes the web door supports. ``email-verify`` is the authenticated door;
#: ``dev`` is the unauthenticated local/test fallback (``?user=``).
AUTH_MODES = ("email-verify", "dev")


@dataclass(frozen=True)
class EmailVerifyConfig:
    """Settings for the email-verify door. Built from the environment
    (:func:`email_verify_from_env`); reuses the same ``CONSOLE_ROOM_SESSION_SECRET``
    that signs the session cookie."""

    session_secret: str
    public_url: str = ""  # external base URL; used to build the verify link
    token_ttl: int = DEFAULT_TOKEN_TTL
    session_ttl: int = DEFAULT_SESSION_TTL  # access lifetime before re-login

    @property
    def secure_cookies(self) -> bool:
        """Mark cookies ``Secure`` when served over https (a https ``public_url``)."""
        return self.public_url.startswith("https://")


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def read_auth_mode() -> str:
    """The web door's auth mode: ``"email-verify"`` (the authenticated door) or
    ``"dev"`` (the unauthenticated local/test fallback).

    Explicit via ``CONSOLE_ROOM_AUTH`` (``email_verify`` is accepted as an alias).
    When the session secret is set we default to **email-verify** — email sign-up
    is the only authenticated mode; otherwise ``dev``."""
    mode = _env("CONSOLE_ROOM_AUTH").lower().replace("_", "-")
    if mode in AUTH_MODES:
        return mode
    # No explicit mode → email-verify when it can run (a session secret is set),
    # else dev. Email sign-up is the default authenticated door.
    return "email-verify" if _env("CONSOLE_ROOM_SESSION_SECRET") else "dev"


def email_verify_from_env() -> EmailVerifyConfig | None:
    """Build :class:`EmailVerifyConfig` from the environment, or ``None`` when the
    session secret is missing (email-verify can't run without it)."""
    secret = _env("CONSOLE_ROOM_SESSION_SECRET")
    if not secret:
        return None
    ttl = _positive_int_env("CONSOLE_ROOM_VERIFY_TTL", DEFAULT_TOKEN_TTL)
    session_ttl = _positive_int_env("CONSOLE_ROOM_SESSION_TTL", DEFAULT_SESSION_TTL)
    return EmailVerifyConfig(
        session_secret=secret,
        public_url=_env("CONSOLE_ROOM_PUBLIC_URL"),
        token_ttl=ttl,
        session_ttl=session_ttl,
    )


def _positive_int_env(name: str, default: int) -> int:
    """A positive integer from env ``name``, or ``default`` (blank / unparseable /
    non-positive all fall back — a TTL must be a real, positive window)."""
    raw = _env(name)
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


# ── domain allowlist (the queue mailbox's domain) ─────────────────────────────


def email_domain(addr: str) -> str:
    """The lowercased domain of an email address, or ``""`` if it isn't a single
    ``local@domain`` with both parts present."""
    text = (addr or "").strip().lower()
    if text.count("@") != 1:
        return ""
    local, _, domain = text.partition("@")
    return domain if local and domain else ""


def domain_allowed(email: str, queue_email: str) -> bool:
    """True if ``email`` may sign up for a queue whose mailbox is ``queue_email`` —
    i.e. they share a (non-empty) domain. The allowlist is automatic: the queue's
    own mailbox domain. Internal-only, so same-domain ≈ same company."""
    domain = email_domain(email)
    return bool(domain) and domain == email_domain(queue_email)


# ── verify token (signed, TTL; single use is DB-enforced via the nonce) ───────


def make_team_slug(name: str) -> str:
    """A stable, URL-safe slug for a team ``name``: lowercased, non-alphanumerics
    collapsed to single hyphens, trimmed. ``""`` if nothing usable remains (the
    caller rejects an empty slug). E.g. ``"Team One!"`` → ``"team-one"``."""
    out: list[str] = []
    prev_hyphen = False
    for ch in (name or "").strip().lower():
        if ch.isascii() and ch.isalnum():
            out.append(ch)
            prev_hyphen = False
        elif not prev_hyphen and out:
            out.append("-")
            prev_hyphen = True
    return "".join(out).strip("-")


def make_nonce() -> str:
    """A fresh single-use nonce for a pending signup (the DB row's key — burning it
    on verify makes the link single-use)."""
    return uuid.uuid4().hex


def sign_verify_token(secret: str, payload: dict[str, Any]) -> str:
    """A URL-safe, timestamped, signed token carrying ``payload`` (e.g. ``{nonce,
    email, name, queue}``). The signature + timestamp are checked on read; single
    use is enforced separately by consuming the nonce row."""
    return URLSafeTimedSerializer(secret, salt=_TOKEN_SALT).dumps(payload)


def read_verify_token(secret: str, token: str, max_age: int) -> dict[str, Any] | None:
    """Validate a verify token: correct signature and within ``max_age`` seconds.
    Returns the payload dict, or ``None`` on a bad / expired / malformed token."""
    try:
        data = URLSafeTimedSerializer(secret, salt=_TOKEN_SALT).loads(
            token, max_age=max_age
        )
    except (BadSignature, SignatureExpired):
        return None
    return data if isinstance(data, dict) else None


# ── dataclasses (shared with enrollment_store) ────────────────────────────────


@dataclass(frozen=True)
class PendingSignup:
    """A signup awaiting email verification — also the durable send-outbox row.

    ``send_state`` is the outbox lifecycle: ``"pending"`` (no console has it yet) →
    ``"claimed"`` (a console is sending it) → ``"sent"``. ``claimed_by`` is the
    console identity that claimed the send."""

    nonce: str
    email: str
    name: str
    queue: str
    created_at: float
    send_state: str = "pending"
    claimed_by: str = ""
    team: str = ""  # the visitor's chosen team (department); display-only routing tag


@dataclass(frozen=True)
class Member:
    """A verified queue member — the persisted enrollment."""

    email: str
    queue: str
    name: str
    verified_at: float
    team: str = ""  # the visitor's team (department), carried from sign-up


@dataclass(frozen=True)
class Team:
    """A customer team/department a visitor belongs to — chosen at sign-up and shown
    to the operator. ``slug`` is the stable id (used in URLs / storage); ``name`` is
    the human label. Not a queue or an operator partition — just a tag on the visitor
    (see docs/design/teams-and-email-monitoring.md §3)."""

    slug: str
    name: str
