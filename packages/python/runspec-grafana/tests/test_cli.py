"""Wrapper logic: real parsed spec → `_dispatch` → core helper, with the core's
httpx routed through a MockTransport (no real network), plus main()'s output paths."""

import json
import os
from pathlib import Path

import httpx
import pytest
from runspec.testing import ParseHarness, no_run_as_check
from runspec_grafana_core import GrafanaConfig
from runspec_grafana_core import client as client_mod

from runspec_grafana import cli

SPEC = Path(__file__).parent.parent / "runspec_grafana" / "runspec.toml"
CFG = GrafanaConfig(url="https://graf")
_REAL_CLIENT = httpx.Client
PROM = {"uid": "prom1", "name": "Prometheus", "type": "prometheus", "isDefault": True}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in list(os.environ):
        if var.startswith("GRAFANA_"):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("RUNSPEC_AUDIT", "off")


def _route(monkeypatch, routes: dict) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = routes.get(request.url.path)
        if answer is None:
            return httpx.Response(404, json={"message": "Not found"})
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)

    def fake_client(**kwargs):
        kwargs.pop("proxy", None)
        kwargs.pop("verify", None)
        return _REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(client_mod.httpx, "Client", fake_client)
    return seen


def _run(argv: list[str], config: GrafanaConfig = CFG):
    h = ParseHarness(script_name="grafana", config_path=SPEC)
    spec = h.expect_ok(argv)
    try:
        return cli._dispatch(spec, config)
    finally:
        h.close()


def test_dispatch_search_dashboards(monkeypatch) -> None:
    seen = _route(monkeypatch, {"/api/search": [{"uid": "a", "title": "API", "url": "/d/a"}]})
    out = _run(["search-dashboards", "--query", "api", "--tag", "prod", "--limit", "5"])
    assert ("query", "api") in seen[0].url.params.multi_items() and ("tag", "prod") in seen[0].url.params.multi_items()
    assert out["dashboards"] == [{"uid": "a", "title": "API", "url": "https://graf/d/a"}]


def test_dispatch_get_dashboard_with_queries(monkeypatch) -> None:
    dash = {"dashboard": {"uid": "a", "title": "API", "panels": [{"id": 1, "title": "Rate", "type": "timeseries", "targets": [{"refId": "A", "expr": "up"}]}]}, "meta": {}}
    _route(monkeypatch, {"/api/dashboards/uid/a": dash})
    out = _run(["get-dashboard", "--uid", "a", "--include", "queries"])
    assert out["panels"][0]["queries"] == [{"ref_id": "A", "query": "up"}]


def test_dispatch_query_prometheus_range(monkeypatch) -> None:
    seen = _route(
        monkeypatch,
        {
            "/api/datasources": [PROM],
            "/api/datasources/proxy/uid/prom1/api/v1/query_range": {"status": "success", "data": {"resultType": "matrix", "result": []}},
        },
    )
    out = _run(["query-prometheus", "--query", "up", "--query-type", "range", "--start", "0", "--end", "3600", "--step", "1m"])
    assert dict(seen[-1].url.params.multi_items())["step"] == "60.0"
    assert out["result_type"] == "matrix" and out["datasource"]["uid"] == "prom1"


def test_dispatch_histogram(monkeypatch) -> None:
    _route(monkeypatch, {"/api/datasources": [PROM], "/api/datasources/proxy/uid/prom1/api/v1/query": {"status": "success", "data": {"resultType": "vector", "result": []}}})
    out = _run(["query-prometheus-histogram", "--metric", "lat", "--percentile", "99", "--by", "route"])
    assert out["query"] == "histogram_quantile(0.99, sum by (le, route) (rate(lat_bucket[5m])))"


def test_dispatch_list_datasources_type(monkeypatch) -> None:
    _route(monkeypatch, {"/api/datasources": [PROM, {"uid": "l", "name": "Loki", "type": "loki"}]})
    assert _run(["list-datasources", "--type", "loki"])["datasources"] == [{"uid": "l", "name": "Loki", "type": "loki"}]


def test_dispatch_create_annotation(monkeypatch) -> None:
    seen = _route(monkeypatch, {"/api/annotations": {"id": 9, "message": "Annotation added"}})
    out = _run(["create-annotation", "--text", "deploy", "--tag", "deploy", "--time", "60"])
    assert seen[0].method == "POST" and json.loads(seen[0].content) == {"text": "deploy", "tags": ["deploy"], "time": 60000}
    assert out["id"] == 9


def test_dispatch_api_get(monkeypatch) -> None:
    seen = _route(monkeypatch, {"/api/org": {"name": "Main"}})
    assert _run(["api-get", "--path", "/api/org", "--param", "x=1"]) == {"name": "Main"}
    assert seen[0].method == "GET"


def _main(monkeypatch, argv: list[str], *, tty: bool = False) -> int:
    monkeypatch.setattr("sys.argv", ["grafana", *argv])
    monkeypatch.setattr(cli, "_terminal", lambda: tty)
    code = 0
    with no_run_as_check():
        try:
            cli.main()
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code


def test_main_reports_errors_as_json(monkeypatch, capsys) -> None:
    _route(monkeypatch, {"/api/user": httpx.Response(401, json={"message": "invalid API key"})})
    code = _main(monkeypatch, ["--url", "https://graf", "whoami"])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 1 and payload["kind"] == "GrafanaError" and "invalid API key" in payload["error"]


def test_main_prints_compact_json_when_piped(monkeypatch, capsys) -> None:
    _route(monkeypatch, {"/api/datasources": [PROM]})
    _main(monkeypatch, ["--url", "https://graf", "list-datasources"])
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(out)["datasources"][0]["uid"] == "prom1" and "\n" not in out


def test_main_prints_text_at_a_terminal(monkeypatch, capsys) -> None:
    _route(monkeypatch, {"/api/datasources": [PROM]})
    _main(monkeypatch, ["--url", "https://graf", "list-datasources"], tty=True)
    out = capsys.readouterr().out
    assert "UID" in out and "prom1" in out and "{" not in out
