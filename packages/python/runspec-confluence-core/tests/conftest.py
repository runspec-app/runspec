"""Shared test helpers: a clean CONFLUENCE_* env and a fake Confluence installer."""

from __future__ import annotations

from typing import Any

import pytest

from runspec_confluence_core import client as client_mod

_SUFFIXES = (
    "URL",
    "USERNAME",
    "API_TOKEN",
    "PERSONAL_TOKEN",
    "CLOUD",
    "SPACE",
    "VERIFY_SSL",
    "PROXY_URL",
    "RETRY",
    "MAX_BACKOFF_RETRIES",
    "MAX_BACKOFF_SECONDS",
    "KROKI_URL",
    "DIAGRAM_MODE",
    "MERMAID_MACRO",
    "PLANTUML_MACRO",
)
_ALL_VARS = ["CONFLUENCE_ENV"] + [f"CONFLUENCE_{s}" for s in _SUFFIXES]
for _env in ("DEV", "UAT", "PROD"):
    _ALL_VARS += [f"CONFLUENCE_{_env}_{s}" for s in _SUFFIXES]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)


class FakeClient:
    """Records method calls; returns preset values (or raises preset exceptions)."""

    def __init__(self, returns: dict[str, Any] | None = None):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._returns = returns or {}

    def __getattr__(self, name: str):
        def method(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            value = self._returns.get(name)
            if isinstance(value, BaseException):
                raise value
            return value() if callable(value) else value

        return method

    def last(self, name: str) -> tuple[tuple, dict]:
        for called, args, kwargs in reversed(self.calls):
            if called == name:
                return args, kwargs
        raise AssertionError(f"{name} was not called; calls={[c[0] for c in self.calls]}")


class FakeResp:
    def __init__(self, status: int, payload: Any):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self) -> Any:
        return self._payload


@pytest.fixture
def install(monkeypatch):
    """Install a fake Confluence client; return (client, made_kwargs)."""

    def _install(returns: dict[str, Any] | None = None):
        client = FakeClient(returns)
        made: dict[str, dict] = {}

        def make(**kw):
            made["kw"] = kw
            return client

        monkeypatch.setattr(client_mod, "Confluence", make)
        return client, made

    return _install
