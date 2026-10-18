"""Shared test helpers: a clean JIRA_* env and a fake atlassian client installer.

The core is tested by monkeypatching ``client.Jira`` / ``client.ServiceDesk`` with
a recording fake — no live server and no real ``atlassian-python-api`` calls. The
fake records every method call and returns preset values (or raises a preset
exception, used to exercise error translation).
"""

from __future__ import annotations

from typing import Any

import pytest

from runspec_jira_core import client as client_mod

_SUFFIXES = ("USER", "PASSWORD", "PAT", "TOKEN", "URL", "USERNAME", "API_TOKEN", "PERSONAL_TOKEN", "CLOUD", "SERVICE_DESK_ID", "PROJECT_KEY", "VERIFY_SSL")
_ALL_VARS = ["JIRA_ENV"] + [f"JIRA_{s}" for s in _SUFFIXES]
for _env in ("DEV", "UAT", "PROD"):
    _ALL_VARS += [f"JIRA_{_env}_{s}" for s in _SUFFIXES]


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
    """Minimal stand-in for a ``requests.Response`` in error-translation tests."""

    def __init__(self, status: int, payload: Any):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self) -> Any:
        return self._payload


@pytest.fixture
def install(monkeypatch):
    """Install fake Jira/ServiceDesk clients; return (jira, sd, made_kwargs)."""

    def _install(returns: dict[str, Any] | None = None, sd_returns: dict[str, Any] | None = None):
        jira = FakeClient(returns)
        sd = FakeClient(sd_returns)
        made: dict[str, dict] = {}

        def make_jira(**kw):
            made["jira"] = kw
            return jira

        def make_sd(**kw):
            made["sd"] = kw
            return sd

        monkeypatch.setattr(client_mod, "Jira", make_jira)
        monkeypatch.setattr(client_mod, "ServiceDesk", make_sd)
        return jira, sd, made

    return _install


from tests.rest_fake import rest  # noqa: E402,F401  (fixture for the raw-REST helper tests)
