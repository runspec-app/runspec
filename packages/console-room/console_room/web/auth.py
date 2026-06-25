"""
web/auth.py — the session identity helpers the app gates on.

The browser door authenticates people via the **email-verify** sign-up flow
(web/signup_routes.py): an internal employee verifies a company email, and the
resulting identity + display name are kept in a signed session cookie. The
WebSocket and the app shell read that identity from the session here. With no
sign-up door the app runs in dev mode (``?user=`` query param), which keeps local
development and the test-suite runnable. See docs/design/console-room-server.md §6.
"""

from __future__ import annotations

from typing import Any


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
    dev mode. Guarded like :func:`session_user` for connections with no
    session."""
    try:
        session = getattr(conn, "session", None)
    except Exception:
        return ""
    if not isinstance(session, dict):
        return ""
    name = session.get("name")
    return name if isinstance(name, str) else ""


def session_team(conn: Any) -> str:
    """The signed-in visitor's chosen team (``conn.session['team']``), or ``""``. Set
    by the email-verify door at verify time; absent in dev mode."""
    try:
        session = getattr(conn, "session", None)
    except Exception:
        return ""
    if not isinstance(session, dict):
        return ""
    team = session.get("team")
    return team if isinstance(team, str) else ""


def session_operator(conn: Any) -> bool:
    """Whether the signed-in user is an **operator** (``conn.session['operator']``),
    i.e. their login email was in the room's operator allowlist — so they get the
    elevated view (full catalogue + runbooks). Set by the email-verify door at verify
    time; ``False`` for a visitor or any connection without a session. Guarded like
    :func:`session_user`."""
    try:
        session = getattr(conn, "session", None)
    except Exception:
        return False
    if not isinstance(session, dict):
        return False
    return bool(session.get("operator"))
