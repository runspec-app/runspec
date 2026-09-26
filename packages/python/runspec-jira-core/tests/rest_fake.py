"""A path-routing fake for the raw-REST helpers (roles, customers, request types).

The newer admin/request-type helpers call the SDK's generic ``get``/``post``/
``put``/``delete`` with REST paths rather than named SDK methods, so the fake
routes on ``(verb, path)`` and records every call. A route value may be a
callable ``(path, kwargs) -> response`` for per-call answers (e.g. paging).
"""

from __future__ import annotations

from typing import Any

import pytest

from runspec_jira_core import client as client_mod
from runspec_jira_core import requesttypes


class RestFake:
    experimental_headers = {"X-ExperimentalApi": "opt-in"}

    def __init__(self, routes: dict[tuple[str, str], Any]):
        self.routes = routes
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _call(self, verb: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((verb, path, kwargs))
        if (verb, path) not in self.routes:
            if verb == "get":
                raise AssertionError(f"unrouted GET {path}")
            return {}
        value = self.routes[(verb, path)]
        return value(path, kwargs) if callable(value) else value

    def get(self, path: str, **kw: Any) -> Any:
        return self._call("get", path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self._call("post", path, **kw)

    def put(self, path: str, **kw: Any) -> Any:
        return self._call("put", path, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self._call("delete", path, **kw)

    def sent(self, verb: str) -> list[tuple[str, dict[str, Any]]]:
        return [(p, kw) for v, p, kw in self.calls if v == verb]


@pytest.fixture
def rest(monkeypatch):
    """Install one RestFake as both the Jira and ServiceDesk client."""

    def _install(routes: dict[tuple[str, str], Any]) -> RestFake:
        fake = RestFake(routes)
        monkeypatch.setattr(client_mod, "Jira", lambda **kw: fake)
        monkeypatch.setattr(client_mod, "ServiceDesk", lambda **kw: fake)
        requesttypes.clear_signature_cache()
        return fake

    return _install


def page(values: list[Any]) -> dict[str, Any]:
    return {"values": values, "isLastPage": True, "size": len(values)}
