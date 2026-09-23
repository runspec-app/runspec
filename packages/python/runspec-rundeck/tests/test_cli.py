"""Dispatch tests: sync / push / list wiring, with the emitter and Rundeck HTTP mocked."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from runspec.testing import ParseHarness
from runspec_rundeck_core import RundeckConfig
from runspec_rundeck_core import client as client_mod

from runspec_rundeck import cli

SPEC = Path(__file__).parent.parent / "runspec_rundeck" / "runspec.toml"
CFG = RundeckConfig(url="https://rd", token="tok", project="ops", api_version=41)


def _harness() -> ParseHarness:
    return ParseHarness(script_name="rundeck", config_path=SPEC)


def _mock_http(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)

    def fake_request(method, url, **kwargs):
        kwargs.pop("verify", None)
        kwargs.pop("proxy", None)
        follow = kwargs.pop("follow_redirects", True)
        with httpx.Client(transport=transport, follow_redirects=follow) as c:
            return c.request(method, url, **kwargs)

    monkeypatch.setattr(client_mod.httpx, "request", fake_request)


def test_dispatch_sync_imports_visible_jobs(monkeypatch):
    monkeypatch.setattr(
        cli,
        "emit_jobs",
        lambda project, **kw: [
            {"name": "greeter", "uuid": "u1", "group": "runspec"},
            {"name": "secret_run", "uuid": "u2", "group": "runspec"},
        ],
    )
    monkeypatch.setattr(cli, "discover_visibility", lambda *a, **k: ({"greeter", "secret"}, {"secret"}))

    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        posted["body"] = json.loads(request.content)
        return httpx.Response(200, json={"succeeded": [{"name": "greeter"}], "failed": [], "skipped": []})

    _mock_http(monkeypatch, handler)

    with _harness() as h:
        spec = h.expect_ok(["sync", "--project", "ops"])
    result = cli._dispatch(spec, CFG)

    assert result["ok"] is True
    assert result["imported"] == ["greeter"]
    assert result["hidden_skipped"] == ["secret_run"]  # hidden runnable filtered out
    assert [j["name"] for j in posted["body"]] == ["greeter"]  # only the visible job posted


def test_dispatch_sync_reports_failure(monkeypatch):
    monkeypatch.setattr(cli, "emit_jobs", lambda project, **kw: [{"name": "greeter", "uuid": "u1", "group": "runspec"}])
    monkeypatch.setattr(cli, "discover_visibility", lambda *a, **k: (set(), set()))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"succeeded": [], "failed": [{"name": "greeter", "error": "bad script"}], "skipped": []})

    _mock_http(monkeypatch, handler)

    with _harness() as h:
        spec = h.expect_ok(["sync", "--project", "ops"])
    result = cli._dispatch(spec, CFG)
    assert result["ok"] is False
    assert result["failed"] == ["greeter: bad script"]


def test_dispatch_sync_no_jobs(monkeypatch):
    monkeypatch.setattr(cli, "emit_jobs", lambda project, **kw: [])
    monkeypatch.setattr(cli, "discover_visibility", lambda *a, **k: (set(), set()))
    with _harness() as h:
        spec = h.expect_ok(["sync", "--project", "ops"])
    result = cli._dispatch(spec, CFG)
    assert result["ok"] is True
    assert result["imported"] == []
    assert result["note"] == "no jobs to sync"


def test_dispatch_push_reads_files(monkeypatch, tmp_path):
    (tmp_path / "greeter.json").write_text(json.dumps([{"name": "greeter", "uuid": "u1", "group": "runspec"}]))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"succeeded": [{"name": "greeter"}], "failed": [], "skipped": []})

    _mock_http(monkeypatch, handler)
    with _harness() as h:
        spec = h.expect_ok(["push", str(tmp_path / "greeter.json"), "--project", "ops"])
    result = cli._dispatch(spec, CFG)
    assert result["ok"] is True
    assert result["imported"] == ["greeter"]


def test_dispatch_list(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "u1", "name": "greeter", "group": "runspec"}])

    _mock_http(monkeypatch, handler)
    with _harness() as h:
        spec = h.expect_ok(["list", "--project", "ops"])
    result = cli._dispatch(spec, CFG)
    assert result["action"] == "list"
    assert result["jobs"] == [{"id": "u1", "name": "greeter", "group": "runspec"}]


def test_main_missing_config_emits_error(monkeypatch, capsys):
    for var in ("RUNDECK_URL", "RUNDECK_TOKEN", "RUNDECK_PROJECT", "RUNDECK_ENV"):
        monkeypatch.delenv(var, raising=False)
    with _harness() as h:
        spec = h.expect_ok(["list", "--project", "ops"])
    monkeypatch.setattr(cli.rs, "parse", lambda name: spec)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "RundeckError"
    assert "url" in payload["error"].lower() or "token" in payload["error"].lower()
