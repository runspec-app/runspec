"""Shared test helpers: a clean RUNDECK_* env and an httpx.MockTransport router."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from runspec_rundeck_core import client as client_mod

_SUFFIXES = ("URL", "TOKEN", "PROJECT", "API_VERSION", "CA_BUNDLE", "PROXY")
_ALL_VARS = ["RUNDECK_ENV"] + [f"RUNDECK_{s}" for s in _SUFFIXES]
for _env in ("DEV", "UAT", "PROD", "PROD_EU"):
    _ALL_VARS += [f"RUNDECK_{_env}_{s}" for s in _SUFFIXES]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)


def mock_request(monkeypatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """Route the module-level ``httpx.request`` through a MockTransport handler."""
    transport = httpx.MockTransport(handler)

    def fake_request(method, url, **kwargs):
        # Drop kwargs MockTransport's Client doesn't accept per-request.
        kwargs.pop("verify", None)
        kwargs.pop("proxy", None)
        follow = kwargs.pop("follow_redirects", True)
        with httpx.Client(transport=transport, follow_redirects=follow) as c:
            return c.request(method, url, **kwargs)

    monkeypatch.setattr(client_mod.httpx, "request", fake_request)
