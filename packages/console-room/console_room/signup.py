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
# same session secret (e.g. the OIDC session cookie).
_TOKEN_SALT = "console-room-email-verify"

#: Default verify-link lifetime (seconds). The link only confirms an address, so a
#: generous-but-bounded window is fine; single use is enforced by the DB nonce.
DEFAULT_TOKEN_TTL = 1800  # 30 minutes

#: Auth modes the web door supports. ``email-verify`` is this feature; ``oidc`` and
#: ``dev`` are unchanged.
AUTH_MODES = ("oidc", "email-verify", "dev")


@dataclass(frozen=True)
class EmailVerifyConfig:
    """Settings for the email-verify door. Built from the environment
    (:func:`email_verify_from_env`); reuses the same ``CONSOLE_ROOM_SESSION_SECRET``
    that signs the session cookie."""

    session_secret: str
    public_url: str = ""  # external base URL; used to build the verify link
    token_ttl: int = DEFAULT_TOKEN_TTL

    @property
    def secure_cookies(self) -> bool:
        """Mark cookies ``Secure`` when served over https (a https ``public_url``)."""
        return self.public_url.startswith("https://")


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def read_auth_mode() -> str:
    """The web door's auth mode: ``"oidc"`` / ``"email-verify"`` / ``"dev"``.

    Explicit via ``CONSOLE_ROOM_AUTH`` (``email_verify`` is accepted as an alias);
    otherwise inferred for back-compat — ``oidc`` when ``CONSOLE_ROOM_OIDC_*`` is
    configured, else ``dev``."""
    mode = _env("CONSOLE_ROOM_AUTH").lower().replace("_", "-")
    if mode in AUTH_MODES:
        return mode
    # Back-compat: no explicit mode → infer from whether OIDC is configured.
    from .web.auth import auth_from_env

    return "oidc" if auth_from_env() is not None else "dev"


def email_verify_from_env() -> EmailVerifyConfig | None:
    """Build :class:`EmailVerifyConfig` from the environment, or ``None`` when the
    session secret is missing (email-verify can't run without it)."""
    secret = _env("CONSOLE_ROOM_SESSION_SECRET")
    if not secret:
        return None
    ttl_raw = _env("CONSOLE_ROOM_VERIFY_TTL")
    try:
        ttl = int(ttl_raw) if ttl_raw else DEFAULT_TOKEN_TTL
    except ValueError:
        ttl = DEFAULT_TOKEN_TTL
    return EmailVerifyConfig(
        session_secret=secret,
        public_url=_env("CONSOLE_ROOM_PUBLIC_URL"),
        token_ttl=ttl if ttl > 0 else DEFAULT_TOKEN_TTL,
    )


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


@dataclass(frozen=True)
class Member:
    """A verified queue member — the persisted enrollment."""

    email: str
    queue: str
    name: str
    verified_at: float
