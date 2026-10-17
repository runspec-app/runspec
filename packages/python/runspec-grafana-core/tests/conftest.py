"""Shared test helpers: a clean GRAFANA_* env and an httpx.MockTransport router."""

from __future__ import annotations

import json
import os

import httpx
import pytest

from runspec_grafana_core import client as client_mod

_REAL_CLIENT = httpx.Client

PROM = {"uid": "prom1", "name": "Prometheus", "type": "prometheus", "isDefault": True}
LOKI = {"uid": "loki1", "name": "Loki", "type": "loki"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in list(os.environ):
        if var.startswith("GRAFANA_"):
            monkeypatch.delenv(var, raising=False)


def route(monkeypatch, routes: dict[str, object]) -> list[httpx.Request]:
    """Answer each request by its path: a value is returned as JSON (status
    200), an ``httpx.Response`` as-is; an unknown path is a 404. Returns the
    list of requests seen, so tests can check params / headers / bodies."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = routes.get(request.url.path)
        if answer is None:
            return httpx.Response(404, json={"message": "Not found"})
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def fake_client(**kwargs):
        kwargs.pop("proxy", None)
        kwargs.pop("verify", None)
        return _REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(client_mod.httpx, "Client", fake_client)
    return seen


def params(request: httpx.Request) -> list[tuple[str, str]]:
    return list(request.url.params.multi_items())


def body(request: httpx.Request) -> object:
    return json.loads(request.content.decode())


def prom_ok(data, **extra) -> dict:
    return {"status": "success", "data": data, **extra}
