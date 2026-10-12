"""Wrapper logic: real parsed spec → `_dispatch` → core helper, with the core's
httpx routed through a MockTransport (no real network), plus main()'s error path."""

import json
from pathlib import Path

import httpx
import pytest
from runspec.testing import ParseHarness
from runspec_prometheus_core import PrometheusConfig
from runspec_prometheus_core import client as client_mod

from runspec_prometheus import cli

SPEC = Path(__file__).parent.parent / "runspec_prometheus" / "runspec.toml"
CFG = PrometheusConfig(url="https://prom")
_REAL_CLIENT = httpx.Client


def _harness() -> ParseHarness:
    return ParseHarness(script_name="prometheus", config_path=SPEC)


def _capture(monkeypatch, response: httpx.Response) -> dict:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["query"] = list(request.url.params.multi_items())
        seen["form"] = list(httpx.QueryParams(request.content.decode()).multi_items())
        return response

    def fake_client(**kwargs):
        kwargs.pop("proxy", None)
        kwargs.pop("verify", None)
        return _REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(client_mod.httpx, "Client", fake_client)
    return seen


def _ok(data) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": data})


def test_dispatch_query(monkeypatch) -> None:
    seen = _capture(monkeypatch, _ok({"resultType": "vector", "result": [{"metric": {}, "value": [1, "1"]}]}))
    h = _harness()
    spec = h.expect_ok(["query", "--query", "up == 0", "--limit", "5"])
    out = cli._dispatch(spec, CFG)
    h.close()
    assert seen["path"] == "/api/v1/query" and ("query", "up == 0") in seen["form"]
    assert out["count"] == 1 and out["truncated"] is False


def test_dispatch_label_values_with_matches(monkeypatch) -> None:
    seen = _capture(monkeypatch, _ok(["node", "api"]))
    h = _harness()
    spec = h.expect_ok(["label-values", "--label", "job", "--match", "up"])
    out = cli._dispatch(spec, CFG)
    h.close()
    assert seen["path"] == "/api/v1/label/job/values" and ("match[]", "up") in seen["query"]
    assert out["result"] == ["node", "api"]


def test_dispatch_list_rules(monkeypatch) -> None:
    seen = _capture(monkeypatch, _ok({"groups": []}))
    h = _harness()
    spec = h.expect_ok(["list-rules", "--rule-type", "alert", "--rule-group", "node"])
    cli._dispatch(spec, CFG)
    h.close()
    assert ("type", "alert") in seen["query"] and ("rule_group[]", "node") in seen["query"]


@pytest.mark.parametrize("verb,path", [("build-info", "/api/v1/status/buildinfo"), ("config", "/api/v1/status/config"), ("list-stores", "/api/v1/stores")])
def test_dispatch_no_arg_verbs(monkeypatch, verb, path) -> None:
    seen = _capture(monkeypatch, _ok({}))
    h = _harness()
    spec = h.expect_ok([verb])
    cli._dispatch(spec, CFG)
    h.close()
    assert seen["path"] == path


def test_dispatch_delete_series(monkeypatch) -> None:
    seen = _capture(monkeypatch, httpx.Response(204))
    h = _harness()
    spec = h.expect_ok(["delete-series", "--match", '{job="old"}', "--start", "0", "--end", "60"])
    out = cli._dispatch(spec, CFG)
    h.close()
    assert seen["path"] == "/api/v1/admin/tsdb/delete_series" and out["deleted"] is True


def test_main_reports_errors_as_json(monkeypatch, capsys) -> None:
    _capture(monkeypatch, httpx.Response(400, json={"status": "error", "errorType": "bad_data", "error": "parse error"}))
    monkeypatch.setenv("RUNSPEC_AUDIT", "off")
    monkeypatch.setattr("sys.argv", ["prometheus", "--url", "https://prom", "query", "--query", "up{"])
    with pytest.raises(SystemExit) as info:
        cli.main()
    assert info.value.code == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["kind"] == "PrometheusError" and "parse error" in payload["error"]
