"""Helper tests — each hits the right endpoint with the right params and shapes a
compact result, routed through an httpx.MockTransport (no real network)."""

import httpx
import pytest

from runspec_grafana_core import (
    GrafanaConfig,
    GrafanaError,
    api_get,
    check_datasources,
    create_annotation,
    get_alert_rule,
    get_annotation_tags,
    get_annotations,
    get_dashboard,
    get_datasource,
    histogram_query,
    list_alert_rules,
    list_contact_points,
    list_dashboard_versions,
    list_datasources,
    list_loki_label_values,
    list_prometheus_label_values,
    list_prometheus_metric_metadata,
    list_prometheus_metric_names,
    list_silences,
    parse_params,
    query_loki_logs,
    query_loki_patterns,
    query_loki_stats,
    query_prometheus,
    query_prometheus_histogram,
    resolve_datasource,
    search_dashboards,
    whoami,
)
from tests.conftest import LOKI, PROM, body, params, prom_ok, route

CFG = GrafanaConfig(url="https://graf")
PROXY = "/api/datasources/proxy/uid"

DASHBOARD = {
    "dashboard": {
        "uid": "abc",
        "title": "API",
        "tags": ["prod"],
        "version": 7,
        "time": {"from": "now-6h", "to": "now"},
        "templating": {"list": [{"name": "job", "type": "query", "query": {"query": "label_values(job)"}, "current": {"text": "api"}}]},
        "panels": [
            {"id": 1, "title": "Rate", "type": "timeseries", "datasource": {"uid": "prom1", "type": "prometheus"}, "targets": [{"refId": "A", "expr": "rate(x[5m])"}]},
            {"id": 2, "type": "row", "title": "Logs", "panels": [{"id": 3, "title": "Errors", "type": "logs", "targets": [{"refId": "A", "expr": '{app="api"}', "datasource": {"uid": "loki1"}}]}]},
        ],
    },
    "meta": {"folderTitle": "Team", "folderUid": "f1", "url": "/d/abc/api", "updated": "2026-10-01T00:00:00Z"},
}


# ── identity / dashboards ──────────────────────────────────────────────────


def test_whoami(monkeypatch) -> None:
    route(monkeypatch, {"/api/user": {"login": "sa-1", "orgId": 1, "isGrafanaAdmin": False, "email": ""}, "/api/user/orgs": [{"orgId": 1, "name": "Main", "role": "Viewer"}]})
    out = whoami(CFG)
    assert out["login"] == "sa-1" and out["orgs"] == [{"org_id": 1, "name": "Main", "role": "Viewer"}]
    assert "email" not in out and out["is_grafana_admin"] is False


def test_search_dashboards_params_and_truncation(monkeypatch) -> None:
    hits = [{"uid": f"u{i}", "title": f"D{i}", "url": f"/d/u{i}", "tags": [], "folderTitle": "F"} for i in range(3)]
    seen = route(monkeypatch, {"/api/search": hits})
    out = search_dashboards(CFG, query="api", tags=["prod", "eu"], starred=True, limit=2)
    sent = params(seen[0])
    assert ("type", "dash-db") in sent and ("tag", "prod") in sent and ("tag", "eu") in sent and ("starred", "true") in sent and ("limit", "3") in sent
    assert out["count"] == 2 and out["truncated"] is True
    assert out["dashboards"][0] == {"uid": "u0", "title": "D0", "folder": "F", "url": "https://graf/d/u0"}


def test_get_dashboard_summary(monkeypatch) -> None:
    route(monkeypatch, {"/api/dashboards/uid/abc": DASHBOARD})
    out = get_dashboard(CFG, "abc")
    assert out["title"] == "API" and out["folder"] == "Team" and out["url"] == "https://graf/d/abc/api"
    assert out["panel_count"] == 2 and [p["title"] for p in out["panels"]] == ["Rate", "Errors"]
    assert out["panels"][1]["row"] == "Logs" and "queries" not in out["panels"][0]
    assert out["variables"] == [{"name": "job", "type": "query", "query": "label_values(job)", "current": "api"}]


def test_get_dashboard_queries_and_panel_filter(monkeypatch) -> None:
    route(monkeypatch, {"/api/dashboards/uid/abc": DASHBOARD})
    out = get_dashboard(CFG, "abc", include=["queries"], panel_id=3)
    assert out["panels"] == [{"id": 3, "title": "Errors", "type": "logs", "row": "Logs", "query_count": 1, "queries": [{"ref_id": "A", "query": '{app="api"}', "datasource": {"uid": "loki1"}}]}]
    with pytest.raises(GrafanaError, match="no panel"):
        get_dashboard(CFG, "abc", panel_id=99)
    with pytest.raises(GrafanaError, match="Unknown include"):
        get_dashboard(CFG, "abc", include=["everything"])


def test_get_dashboard_raw_and_version(monkeypatch) -> None:
    route(monkeypatch, {"/api/dashboards/uid/abc": DASHBOARD, "/api/dashboards/uid/abc/versions/3": {"version": 3, "data": {"uid": "abc", "title": "Old", "panels": []}}})
    assert get_dashboard(CFG, "abc", raw=True) == DASHBOARD
    old = get_dashboard(CFG, "abc", version=3)
    assert old["title"] == "Old" and old["version"] == 3 and old["folder"] == "Team"


@pytest.mark.parametrize("payload", [[{"version": 2, "createdBy": "bob"}], {"versions": [{"version": 2, "createdBy": "bob"}], "continueToken": ""}])
def test_list_dashboard_versions_both_shapes(monkeypatch, payload) -> None:
    route(monkeypatch, {"/api/dashboards/uid/abc/versions": payload})
    assert list_dashboard_versions(CFG, "abc")["versions"] == [{"version": 2, "created_by": "bob"}]


# ── datasources ────────────────────────────────────────────────────────────


def test_list_datasources_type_filter(monkeypatch) -> None:
    route(monkeypatch, {"/api/datasources": [PROM, LOKI]})
    out = list_datasources(CFG, ds_type="loki")
    assert out["datasources"] == [{"uid": "loki1", "name": "Loki", "type": "loki"}]


def test_get_datasource_by_name_and_secrets_named_only(monkeypatch) -> None:
    record = {**PROM, "id": 4, "url": "http://prom:9090", "jsonData": {"httpMethod": "POST"}, "secureJsonFields": {"basicAuthPassword": True, "x": False}}
    route(monkeypatch, {"/api/datasources/name/Prometheus": record})
    out = get_datasource(CFG, "Prometheus")
    assert out["uid"] == "prom1" and out["secure_fields"] == ["basicAuthPassword"] and "id" not in out


def test_resolve_datasource_default_and_ambiguity(monkeypatch) -> None:
    route(monkeypatch, {"/api/datasources": [PROM, {**PROM, "uid": "prom2", "isDefault": False}, LOKI]})
    assert resolve_datasource(CFG, None, "prometheus")["uid"] == "prom1"
    assert resolve_datasource(CFG, None, "loki")["uid"] == "loki1"
    with pytest.raises(GrafanaError, match="No tempo"):
        resolve_datasource(CFG, None, "tempo")


def test_resolve_datasource_ambiguous_names_candidates(monkeypatch) -> None:
    route(monkeypatch, {"/api/datasources": [LOKI, {**LOKI, "uid": "loki2", "name": "Loki EU"}]})
    with pytest.raises(GrafanaError, match="Loki EU"):
        resolve_datasource(CFG, None, "loki")


def test_resolve_datasource_wrong_type(monkeypatch) -> None:
    route(monkeypatch, {"/api/datasources/uid/loki1": LOKI})
    with pytest.raises(GrafanaError, match="not 'prometheus'"):
        resolve_datasource(CFG, "loki1", "prometheus")


def test_check_datasources_reports_failures(monkeypatch) -> None:
    route(
        monkeypatch,
        {
            "/api/datasources": [PROM, LOKI],
            "/api/datasources/uid/prom1/health": {"status": "OK", "message": "Successfully queried"},
            "/api/datasources/uid/loki1/health": httpx.Response(400, json={"message": "connection refused"}),
        },
    )
    out = check_datasources(CFG)
    assert out["checked"] == 2 and out["failing"] == 1
    assert out["results"][1]["status"] == "ERROR" and "connection refused" in out["results"][1]["message"]


# ── prometheus via grafana ─────────────────────────────────────────────────


def test_query_prometheus_instant(monkeypatch) -> None:
    vector = [{"metric": {"job": f"j{i}"}, "value": [60, "1"]} for i in range(3)]
    seen = route(monkeypatch, {"/api/datasources/uid/prom1": PROM, f"{PROXY}/prom1/api/v1/query": prom_ok({"resultType": "vector", "result": vector})})
    out = query_prometheus(CFG, "up == 0", datasource="prom1", at="60", limit=2)
    assert ("query", "up == 0") in params(seen[-1]) and ("time", "60.0") in params(seen[-1])
    assert out["datasource"] == {"uid": "prom1", "name": "Prometheus"} and out["count"] == 3 and out["truncated"] is True
    assert out["result"][0] == {"metric": {"job": "j0"}, "time": "1970-01-01T00:01:00Z", "value": 1.0}


def test_query_prometheus_range_summarises_series(monkeypatch) -> None:
    matrix = [{"metric": {"job": "api"}, "values": [[0, "1"], [60, "5"], [120, "NaN"], [180, "3"]]}]
    seen = route(monkeypatch, {"/api/datasources": [PROM], f"{PROXY}/prom1/api/v1/query_range": prom_ok({"resultType": "matrix", "result": matrix})})
    out = query_prometheus(CFG, "x", query_type="range", start="0", end="3600")
    sent = dict(params(seen[-1]))
    assert sent["start"] == "0.0" and sent["end"] == "3600.0" and sent["step"] == "14.0"
    row = out["result"][0]
    assert row["points"] == 4 and row["min"] == 1.0 and row["max"] == 5.0 and row["last"] == ["1970-01-01T00:03:00Z", 3.0]
    assert "values" not in row
    full = query_prometheus(CFG, "x", query_type="range", start="0", end="3600", include_values=True)
    assert full["result"][0]["values"][2] == ["1970-01-01T00:02:00Z", "NaN"]


def test_histogram_query_builds_promql() -> None:
    assert histogram_query("http_duration_seconds_bucket", 99, '{job="api"}', ["route"]) == ('histogram_quantile(0.99, sum by (le, route) (rate(http_duration_seconds_bucket{job="api"}[5m])))')
    assert histogram_query("x", 50) == "histogram_quantile(0.5, sum by (le) (rate(x_bucket[5m])))"
    with pytest.raises(GrafanaError):
        histogram_query("x", 120)
    with pytest.raises(GrafanaError):
        histogram_query("x) or vector(1", 50)


def test_query_prometheus_histogram_returns_query(monkeypatch) -> None:
    route(monkeypatch, {"/api/datasources": [PROM], f"{PROXY}/prom1/api/v1/query": prom_ok({"resultType": "vector", "result": []})})
    out = query_prometheus_histogram(CFG, "lat", percentile=95)
    assert out["query"].startswith("histogram_quantile(0.95,") and out["count"] == 0


def test_metric_names_regex_filter(monkeypatch) -> None:
    route(monkeypatch, {"/api/datasources": [PROM], f"{PROXY}/prom1/api/v1/label/__name__/values": prom_ok(["http_requests_total", "up", "HTTP_errors"])})
    out = list_prometheus_metric_names(CFG, regex="^http")
    assert out["metrics"] == ["http_requests_total", "HTTP_errors"]


def test_label_values_passes_matches(monkeypatch) -> None:
    seen = route(monkeypatch, {"/api/datasources": [PROM], f"{PROXY}/prom1/api/v1/label/job/values": prom_ok(["api", "node"])})
    out = list_prometheus_label_values(CFG, "job", matches=["up"])
    assert ("match[]", "up") in params(seen[-1]) and out["values"] == ["api", "node"] and out["label"] == "job"


def test_metric_metadata_one_row_per_metric(monkeypatch) -> None:
    route(monkeypatch, {"/api/datasources": [PROM], f"{PROXY}/prom1/api/v1/metadata": prom_ok({"up": [{"type": "gauge", "help": "Up", "unit": ""}, {"type": "gauge"}]})})
    assert list_prometheus_metric_metadata(CFG)["metadata"] == [{"metric": "up", "type": "gauge", "help": "Up"}]


# ── loki via grafana ───────────────────────────────────────────────────────


def test_query_loki_logs_flattens_streams(monkeypatch) -> None:
    streams = [
        {"stream": {"app": "api"}, "values": [["2000000000", "older"], ["4000000000", "x" * 30]]},
        {"stream": {"app": "web"}, "values": [["3000000000", "middle"]]},
    ]
    seen = route(monkeypatch, {"/api/datasources": [LOKI], f"{PROXY}/loki1/loki/api/v1/query_range": prom_ok({"resultType": "streams", "result": streams})})
    out = query_loki_logs(CFG, '{app=~".+"}', start="0", end="10", limit=2, max_line_chars=10)
    sent = dict(params(seen[-1]))
    assert sent["start"] == "0" and sent["end"] == "10000000000" and sent["limit"] == "3" and sent["direction"] == "backward"
    assert out["streams"] == [{"labels": {"app": "api"}, "lines": 2}, {"labels": {"app": "web"}, "lines": 1}]
    assert [e["stream"] for e in out["entries"]] == [0, 1] and out["truncated"] is True
    assert out["entries"][0]["line"] == "x" * 10 + "…" and out["entries"][0]["line_chars"] == 30


def test_query_loki_metric_query(monkeypatch) -> None:
    route(monkeypatch, {"/api/datasources": [LOKI], f"{PROXY}/loki1/loki/api/v1/query_range": prom_ok({"resultType": "matrix", "result": [{"metric": {"app": "api"}, "values": [[1, "2"]]}]})})
    out = query_loki_logs(CFG, 'rate({app="api"}[5m])')
    assert out["result_type"] == "matrix" and out["result"][0]["points"] == 1


def test_loki_label_values_stats_patterns(monkeypatch) -> None:
    route(
        monkeypatch,
        {
            "/api/datasources": [LOKI],
            f"{PROXY}/loki1/loki/api/v1/label/app/values": prom_ok(["api"]),
            f"{PROXY}/loki1/loki/api/v1/index/stats": {"streams": 2, "chunks": 5, "entries": 100, "bytes": 2048},
            f"{PROXY}/loki1/loki/api/v1/patterns": prom_ok([{"pattern": "a <_>", "samples": [[1, 2], [2, 3]]}, {"pattern": "b <_>", "samples": [[1, 9]], "level": "error"}]),
        },
    )
    assert list_loki_label_values(CFG, "app")["values"] == ["api"]
    assert query_loki_stats(CFG, '{app="api"}')["entries"] == 100
    assert query_loki_patterns(CFG, '{app="api"}')["patterns"] == [{"pattern": "b <_>", "count": 9, "level": "error"}, {"pattern": "a <_>", "count": 5}]


# ── alerting ───────────────────────────────────────────────────────────────

RULES = prom_ok(
    {
        "groups": [
            {
                "name": "api",
                "file": "Team",
                "rules": [
                    {"uid": "r1", "name": "Latency high", "state": "inactive", "health": "ok", "type": "alerting", "alerts": []},
                    {
                        "uid": "r2",
                        "name": "API down",
                        "state": "firing",
                        "health": "ok",
                        "type": "alerting",
                        "query": "up == 0",
                        "alerts": [{"labels": {"job": "api"}, "state": "Alerting", "activeAt": "t"}],
                    },
                    {"name": "rec", "type": "recording"},
                ],
            }
        ]
    }
)


def test_list_alert_rules_firing_first_and_filters(monkeypatch) -> None:
    route(monkeypatch, {"/api/prometheus/grafana/api/v1/rules": RULES})
    out = list_alert_rules(CFG)
    assert [r["uid"] for r in out["rules"]] == ["r2", "r1"] and out["by_state"] == {"firing": 1, "inactive": 1}
    assert out["rules"][0]["active_alerts"] == 1 and "alerts" not in out["rules"][0]
    firing = list_alert_rules(CFG, state="firing", include=["alerts", "query"])
    assert firing["count"] == 1 and firing["rules"][0]["query"] == "up == 0" and firing["rules"][0]["alerts"][0]["labels"] == {"job": "api"}
    assert list_alert_rules(CFG, search="latency")["rules"][0]["uid"] == "r1"


def test_list_alert_rules_from_datasource(monkeypatch) -> None:
    seen = route(monkeypatch, {"/api/datasources/uid/prom1": PROM, "/api/prometheus/prom1/api/v1/rules": prom_ok({"groups": []})})
    assert list_alert_rules(CFG, datasource="prom1")["count"] == 0
    assert seen[-1].url.path == "/api/prometheus/prom1/api/v1/rules"


def test_get_alert_rule_compact(monkeypatch) -> None:
    rule = {
        "uid": "r1",
        "title": "T",
        "condition": "C",
        "for": "5m",
        "data": [{"refId": "A", "datasourceUid": "prom1", "model": {"expr": "up"}}, {"refId": "C", "datasourceUid": "__expr__", "model": {"type": "threshold"}}],
    }
    route(monkeypatch, {"/api/v1/provisioning/alert-rules/r1": rule})
    out = get_alert_rule(CFG, "r1")
    assert out["queries"] == [{"ref_id": "A", "datasource_uid": "prom1", "query": "up"}, {"ref_id": "C", "datasource_uid": "__expr__", "type": "threshold"}]


def test_contact_points_hide_settings_by_default(monkeypatch) -> None:
    route(monkeypatch, {"/api/v1/provisioning/contact-points": [{"uid": "c1", "name": "ops", "type": "email", "settings": {"addresses": "ops@x"}}]})
    assert list_contact_points(CFG)["contact_points"] == [{"uid": "c1", "name": "ops", "type": "email"}]
    assert list_contact_points(CFG, include_settings=True)["contact_points"][0]["settings"] == {"addresses": "ops@x"}


def test_list_silences_state_filter_and_matchers(monkeypatch) -> None:
    silences = [
        {
            "id": "s1",
            "status": {"state": "active"},
            "matchers": [{"name": "job", "value": "api", "isEqual": True, "isRegex": False}, {"name": "env", "value": "p.*", "isEqual": False, "isRegex": True}],
            "endsAt": "2",
        },
        {"id": "s2", "status": {"state": "expired"}, "matchers": [], "endsAt": "1"},
    ]
    route(monkeypatch, {"/api/alertmanager/grafana/api/v2/silences": silences})
    out = list_silences(CFG)
    assert out["count"] == 1 and out["silences"][0]["matchers"] == 'job="api", env!~"p.*"'
    assert list_silences(CFG, state="any")["count"] == 2


# ── annotations / api ──────────────────────────────────────────────────────


def test_get_annotations(monkeypatch) -> None:
    seen = route(monkeypatch, {"/api/annotations": [{"id": 1, "time": 60000, "timeEnd": 60000, "text": "deploy v2", "tags": ["deploy"], "dashboardUID": "abc", "login": "bob"}]})
    out = get_annotations(CFG, start="0", end="120", tags=["deploy"])
    sent = params(seen[0])
    assert ("from", "0") in sent and ("to", "120000") in sent and ("tags", "deploy") in sent
    assert out["annotations"] == [{"id": 1, "time": "1970-01-01T00:01:00Z", "text": "deploy v2", "tags": ["deploy"], "dashboard_uid": "abc", "by": "bob"}]


def test_get_annotation_tags(monkeypatch) -> None:
    route(monkeypatch, {"/api/annotations/tags": {"result": {"tags": [{"tag": "a", "count": 1}, {"tag": "deploy", "count": 9}]}}})
    assert get_annotation_tags(CFG)["tags"][0] == {"tag": "deploy", "count": 9}


def test_create_annotation_posts_ms(monkeypatch) -> None:
    seen = route(monkeypatch, {"/api/annotations": {"id": 42, "message": "Annotation added"}})
    out = create_annotation(CFG, "deploy v3", tags=["deploy"], dashboard_uid="abc", at="60", end="120")
    assert seen[0].method == "POST" and body(seen[0]) == {"text": "deploy v3", "tags": ["deploy"], "dashboardUID": "abc", "time": 60000, "timeEnd": 120000}
    assert out["id"] == 42 and out["time_end"] == "1970-01-01T00:02:00Z"


def test_create_annotation_validation() -> None:
    with pytest.raises(GrafanaError, match="empty"):
        create_annotation(CFG, "  ")
    with pytest.raises(GrafanaError, match="dashboard_uid"):
        create_annotation(CFG, "x", panel_id=2)


def test_api_get(monkeypatch) -> None:
    seen = route(monkeypatch, {"/api/org": {"id": 1, "name": "Main"}})
    assert api_get(CFG, "/api/org", ["a=1", "b=2", "b=3"]) == {"id": 1, "name": "Main"}
    assert params(seen[0]) == [("a", "1"), ("b", "2"), ("b", "3")]
    with pytest.raises(GrafanaError, match="/api/"):
        api_get(CFG, "/public/x")
    with pytest.raises(GrafanaError, match=r"\.\."):
        api_get(CFG, "/api/../admin")
    with pytest.raises(GrafanaError, match="key=value"):
        parse_params(["novalue"])
