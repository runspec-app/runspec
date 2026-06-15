"""
test_auth.py — the session identity helpers (pure, dependency-free).

The gated-app behaviour is in test_web_app.py / test_signup_web.py.
"""

from __future__ import annotations

from console_room.web.auth import session_name, session_user

# ── session_user / session_name ───────────────────────────────────────────────


class _Conn:
    def __init__(self, session) -> None:
        self.session = session


def test_session_user_reads_session():
    assert session_user(_Conn({"user": "ana@b.com"})) == "ana@b.com"


def test_session_name_reads_session():
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
