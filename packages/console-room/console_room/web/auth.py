"""
web/auth.py — OIDC login config + the identity helpers the app gates on.

The browser door authenticates people via **OpenID Connect** (decision #4): the
server never stores passwords — it redirects to your IdP (Entra / Google / Okta
…), verifies the returned token, and keeps the resulting identity in a signed
session cookie. The WebSocket and the app shell then read that identity from the
session instead of a query param.

This module holds the *config* (env-driven, secrets never on the CLI) and the
*pure* identity helpers; the routes + middleware that use them live in web/app.py.
When OIDC isn't configured, the app falls back to the S2 dev mode (query-param
identity, no login), which is what keeps local development and the test-suite
runnable without an IdP. See docs/design/console-room-server.md §1 (#3/#4) / §6.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# The identity claims we accept, most-specific first.
_USER_CLAIMS = ("email", "preferred_username", "name", "sub")


@dataclass(frozen=True)
class AuthConfig:
    """OIDC settings. Built from the environment (``auth_from_env``); absent →
    the app runs in dev mode with no login."""

    client_id: str
    client_secret: str
    session_secret: str
    metadata_url: str
    scopes: str = "openid email profile"
    public_url: str = ""  # external base URL; used to build the redirect_uri
    callback_path: str = "/auth/callback"

    @property
    def secure_cookies(self) -> bool:
        """Mark the session cookie ``Secure`` when we're served over https (i.e.
        a https ``public_url``) — off for plain-http local dev so the cookie still
        round-trips."""
        return self.public_url.startswith("https://")

    def redirect_uri(self, request_base: str) -> str:
        """The OIDC ``redirect_uri``: ``public_url`` + callback path when an
        external URL is configured (correct behind a TLS proxy), else derived from
        the incoming request's base URL."""
        base = (self.public_url or request_base).rstrip("/")
        return base + self.callback_path


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def auth_from_env() -> AuthConfig | None:
    """Build :class:`AuthConfig` from ``CONSOLE_ROOM_OIDC_*`` env, or ``None`` when
    OIDC isn't fully configured (→ dev mode). Requires client id + secret, a
    session secret, and either an explicit metadata URL or an issuer (from which
    the standard discovery URL is derived)."""
    client_id = _env("CONSOLE_ROOM_OIDC_CLIENT_ID")
    client_secret = _env("CONSOLE_ROOM_OIDC_CLIENT_SECRET")
    session_secret = _env("CONSOLE_ROOM_SESSION_SECRET")
    metadata_url = _env("CONSOLE_ROOM_OIDC_METADATA_URL")
    issuer = _env("CONSOLE_ROOM_OIDC_ISSUER")
    if not metadata_url and issuer:
        metadata_url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    if not (client_id and client_secret and session_secret and metadata_url):
        return None
    return AuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        session_secret=session_secret,
        metadata_url=metadata_url,
        scopes=_env("CONSOLE_ROOM_OIDC_SCOPES") or "openid email profile",
        public_url=_env("CONSOLE_ROOM_PUBLIC_URL"),
    )


def identity_from_userinfo(userinfo: dict[str, Any] | None) -> str:
    """Pick a stable display identity from an OIDC userinfo claim set (email →
    preferred_username → name → sub). ``""`` if none are present."""
    if not userinfo:
        return ""
    for claim in _USER_CLAIMS:
        val = userinfo.get(claim)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def session_user(conn: Any) -> str:
    """The logged-in user on a request/websocket (``conn.session['user']``), or
    ``""`` if there's no session / no user. Safe on connections without a session:
    in dev mode there's no ``SessionMiddleware``, and Starlette's ``request.session``
    *asserts* it's installed — raising ``AssertionError`` (not ``AttributeError``),
    which a plain ``getattr`` default wouldn't swallow — so guard the access."""
    try:
        session = getattr(conn, "session", None)
    except Exception:
        return ""
    if not isinstance(session, dict):
        return ""
    user = session.get("user")
    return user if isinstance(user, str) else ""


def session_name(conn: Any) -> str:
    """The signed-in user's display name (``conn.session['name']``), or ``""``. Set
    by the email-verify door at verify time (the self-asserted full name); absent in
    OIDC / dev mode. Guarded like :func:`session_user` for connections with no
    session."""
    try:
        session = getattr(conn, "session", None)
    except Exception:
        return ""
    if not isinstance(session, dict):
        return ""
    name = session.get("name")
    return name if isinstance(name, str) else ""


def build_oauth(cfg: AuthConfig) -> Any:
    """Construct an authlib Starlette ``OAuth`` registry with one ``oidc`` client
    from ``cfg``. Imported lazily so the web app loads without authlib in dev."""
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    oauth.register(
        name="oidc",
        server_metadata_url=cfg.metadata_url,
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        client_kwargs={"scope": cfg.scopes},
    )
    return oauth
