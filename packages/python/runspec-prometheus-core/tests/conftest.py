"""Shared test helpers: a clean PROMETHEUS_* env and an httpx.MockTransport router."""

from __future__ import annotations

import os
from collections.abc import Callable

import httpx
import pytest

from runspec_prometheus_core import client as client_mod

_REAL_CLIENT = httpx.Client


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in list(os.environ):
        if var.startswith("PROMETHEUS_"):
            monkeypatch.delenv(var, raising=False)


def mock_transport(monkeypatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """Route the core's httpx.Client through a MockTransport so tests exercise
    real URL / form / header building."""

    def fake_client(**kwargs):
        kwargs.pop("proxy", None)
        kwargs.pop("verify", None)
        return _REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(client_mod.httpx, "Client", fake_client)


def capture(monkeypatch, response: httpx.Response) -> dict:
    """Mock every request with ``response`` and record what was sent."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["query"] = list(request.url.params.multi_items())
        seen["form"] = list(httpx.QueryParams(request.content.decode()).multi_items()) if request.method == "POST" else []
        seen["headers"] = request.headers
        return response

    mock_transport(monkeypatch, handler)
    return seen


def ok(data, **extra) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": data, **extra})
