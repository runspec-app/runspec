"""Shared test helpers: a clean GITLAB_* env and an httpx.MockTransport router."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from runspec_gitlab_core import client as client_mod

_SUFFIXES = ("URL", "TOKEN", "PAT", "PROJECT")
_ALL_VARS = ["GITLAB_ENV"] + [f"GITLAB_{s}" for s in _SUFFIXES]
for _env in ("DEV", "UAT", "PROD"):
    _ALL_VARS += [f"GITLAB_{_env}_{s}" for s in _SUFFIXES]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)


def mock_request(monkeypatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """Route runspec_gitlab_core.client's module-level httpx.request through a
    MockTransport so tests exercise real URL/param/header building."""
    transport = httpx.MockTransport(handler)

    def fake_request(method, url, **kwargs):
        with httpx.Client(transport=transport) as c:
            return c.request(method, url, **kwargs)

    monkeypatch.setattr(client_mod.httpx, "request", fake_request)
