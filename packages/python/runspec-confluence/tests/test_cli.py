"""Wrapper logic: real parsed spec → `_dispatch` → core helper, with the core's
atlassian Confluence client faked, plus the main() error path."""

import json
from pathlib import Path
from typing import Any

import pytest
from runspec.testing import ParseHarness
from runspec_confluence_core import ConfluenceConfig
from runspec_confluence_core import client as core_client

from runspec_confluence import cli

SPEC = Path(__file__).parent.parent / "runspec_confluence" / "runspec.toml"
CFG = ConfluenceConfig(url="https://acme.atlassian.net/wiki", cloud=True, username="me@x.com", api_token="tok", space="DOCS")


class FakeClient:
    def __init__(self, returns: dict[str, Any] | None = None):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._returns = returns or {}

    def __getattr__(self, name: str):
        def method(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            return self._returns.get(name)

        return method

    def last(self, name: str) -> tuple[tuple, dict]:
        for called, args, kwargs in reversed(self.calls):
            if called == name:
                return args, kwargs
        raise AssertionError(f"{name} not called; saw {[c[0] for c in self.calls]}")


@pytest.fixture
def fake(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(core_client, "Confluence", lambda **kw: client)
    return client


def _harness() -> ParseHarness:
    return ParseHarness(script_name="confluence", config_path=SPEC)


def test_dispatch_get_page_by_id(fake) -> None:
    fake._returns["get_page_by_id"] = {"id": "1"}
    h = _harness()
    spec = h.expect_ok(["get-page", "--page-id", "1", "--expand", "body.storage"])
    assert cli._dispatch(spec, CFG) == {"id": "1"}
    h.close()
    args, kwargs = fake.last("get_page_by_id")
    assert args == ("1",)
    assert kwargs["expand"] == "body.storage"


def test_dispatch_create_page_representation(fake) -> None:
    h = _harness()
    spec = h.expect_ok(["create-page", "--space", "DOCS", "--title", "New", "--content", "<p>x</p>", "--content-format", "wiki"])
    cli._dispatch(spec, CFG)
    h.close()
    args, kwargs = fake.last("create_page")
    assert args == ("DOCS", "New", "<p>x</p>")
    assert kwargs["representation"] == "wiki"


def test_dispatch_search(fake) -> None:
    fake._returns["cql"] = {"results": []}
    h = _harness()
    spec = h.expect_ok(["search", "--cql", "type = page", "--limit", "3"])
    cli._dispatch(spec, CFG)
    h.close()
    args, kwargs = fake.last("cql")
    assert args == ("type = page",)
    assert kwargs["limit"] == 3


def test_dispatch_delete_page(fake) -> None:
    h = _harness()
    spec = h.expect_ok(["delete-page", "--page-id", "9", "--recursive"])
    result = cli._dispatch(spec, CFG)
    h.close()
    assert result == {"page_id": "9", "deleted": True}
    _, kwargs = fake.last("remove_page")
    assert kwargs["recursive"] is True


def test_main_missing_config_emits_error(monkeypatch, capsys) -> None:
    for var in ("CONFLUENCE_URL", "CONFLUENCE_USERNAME", "CONFLUENCE_API_TOKEN", "CONFLUENCE_PERSONAL_TOKEN", "CONFLUENCE_ENV"):
        monkeypatch.delenv(var, raising=False)
    h = _harness()
    spec = h.expect_ok(["get-page", "--page-id", "1"])
    h.close()
    monkeypatch.setattr(cli.rs, "parse", lambda name: spec)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "ConfluenceError"
