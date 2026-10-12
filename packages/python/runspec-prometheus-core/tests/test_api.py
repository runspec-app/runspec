"""Helper tests — each hits the right endpoint with the right params and shapes
its result, routed through an httpx.MockTransport (no real network)."""

import httpx
import pytest

from runspec_prometheus_core import (
    PrometheusConfig,
    PrometheusError,
    clean_tombstones,
    delete_series,
    exemplar_query,
    get_config,
    healthy,
    label_names,
    label_values,
    list_alerts,
    list_rules,
    list_stores,
    list_targets,
    metric_metadata,
    query,
    range_query,
    ready,
    reload,
    series,
    snapshot,
    targets_metadata,
    tsdb_stats,
)
from tests.conftest import capture, ok

CFG = PrometheusConfig(url="https://prom")


def test_query_posts_form_and_truncates(monkeypatch) -> None:
    vector = [{"metric": {"job": f"j{i}"}, "value": [1, "1"]} for i in range(5)]
    seen = capture(monkeypatch, ok({"resultType": "vector", "result": vector}, warnings=["w"]))
    out = query(CFG, 'up{job="x"}', at="1733000000", limit=2)
    assert seen["method"] == "POST" and seen["path"] == "/api/v1/query"
    assert ("query", 'up{job="x"}') in seen["form"] and ("time", "1733000000.0") in seen["form"]
    assert out["result_type"] == "vector" and out["count"] == 5 and out["truncated"] is True
    assert len(out["result"]) == 2 and out["warnings"] == ["w"]


def test_query_scalar_result_is_listed(monkeypatch) -> None:
    capture(monkeypatch, ok({"resultType": "scalar", "result": [1, "2"]}))
    out = query(CFG, "1+1")
    assert out["result"] == [[1, "2"]] and out["count"] == 1


def test_range_query_auto_step_and_window(monkeypatch) -> None:
    seen = capture(monkeypatch, ok({"resultType": "matrix", "result": []}))
    out = range_query(CFG, "rate(x[5m])", start="0", end="3600")
    form = dict(seen["form"])
    assert seen["path"] == "/api/v1/query_range"
    assert form["start"] == "0.0" and form["end"] == "3600.0" and form["step"] == "14.0"
    assert out["start"] == "1970-01-01T00:00:00Z" and out["step"] == 14.0


def test_range_query_explicit_step(monkeypatch) -> None:
    seen = capture(monkeypatch, ok({"resultType": "matrix", "result": []}))
    range_query(CFG, "x", start="0", end="3600", step="5m")
    assert dict(seen["form"])["step"] == "300.0"


def test_range_query_rejects_inverted_window() -> None:
    with pytest.raises(PrometheusError, match="before"):
        range_query(CFG, "x", start="3600", end="0")


def test_exemplars(monkeypatch) -> None:
    seen = capture(monkeypatch, ok([{"seriesLabels": {}, "exemplars": []}]))
    out = exemplar_query(CFG, "x", start="0", end="60")
    assert seen["path"] == "/api/v1/query_exemplars" and out["count"] == 1


def test_series_repeats_match_and_asks_server_limit(monkeypatch) -> None:
    seen = capture(monkeypatch, ok([{"__name__": "up"}]))
    series(CFG, ["up", '{job="node"}'], limit=10)
    assert [v for k, v in seen["form"] if k == "match[]"] == ["up", '{job="node"}']
    assert ("limit", "11") in seen["form"]


def test_series_needs_a_matcher() -> None:
    with pytest.raises(PrometheusError, match="at least one"):
        series(CFG, [])


def test_label_names_and_values(monkeypatch) -> None:
    seen = capture(monkeypatch, ok(["a", "b", "c"]))
    assert label_names(CFG, limit=0)["result"] == ["a", "b", "c"]
    out = label_values(CFG, "job", matches=["up"], limit=2)
    assert seen["method"] == "GET" and seen["path"] == "/api/v1/label/job/values"
    assert ("match[]", "up") in seen["query"] and out["truncated"] is True and out["label"] == "job"


def test_metadata(monkeypatch) -> None:
    seen = capture(monkeypatch, ok({"up": [{"type": "gauge"}]}))
    assert metric_metadata(CFG, metric="up")["count"] == 1
    assert ("metric", "up") in seen["query"]
    seen = capture(monkeypatch, ok([{"target": {}}]))
    targets_metadata(CFG, match_target='{job="node"}', limit=0)
    assert seen["path"] == "/api/v1/targets/metadata" and ("limit", "0") not in seen["query"]


def test_alerts_filter_state(monkeypatch) -> None:
    capture(monkeypatch, ok({"alerts": [{"state": "firing"}, {"state": "pending"}]}))
    assert list_alerts(CFG)["count"] == 2
    assert list_alerts(CFG, state="firing")["alerts"] == [{"state": "firing"}]


def test_rules_params_and_counts(monkeypatch) -> None:
    seen = capture(monkeypatch, ok({"groups": [{"name": "g", "rules": [{}, {}]}]}))
    out = list_rules(CFG, rule_type="alert", rule_groups=["g"], exclude_alerts=True)
    assert ("type", "alert") in seen["query"] and ("rule_group[]", "g") in seen["query"] and ("exclude_alerts", "true") in seen["query"]
    assert out == {"group_count": 1, "rule_count": 2, "groups": [{"name": "g", "rules": [{}, {}]}]}


def test_targets_health_filter_and_truncation(monkeypatch) -> None:
    active = [{"health": "up"}, {"health": "down"}, {"health": "down"}]
    capture(monkeypatch, ok({"activeTargets": active, "droppedTargets": []}))
    out = list_targets(CFG, health="down", limit=1)
    assert out["active_count"] == 2 and out["active_truncated"] is True and "dropped" not in out


def test_status_endpoints(monkeypatch) -> None:
    seen = capture(monkeypatch, ok({"yaml": "global: {}"}))
    assert get_config(CFG) == {"yaml": "global: {}"}
    assert seen["path"] == "/api/v1/status/config"
    tsdb_stats(CFG, limit=5)
    assert seen["path"] == "/api/v1/status/tsdb" and ("limit", "5") in seen["query"]
    list_stores(CFG)
    assert seen["path"] == "/api/v1/stores"


def test_management(monkeypatch) -> None:
    seen = capture(monkeypatch, httpx.Response(200, text="Prometheus Server is Healthy.\n"))
    assert healthy(CFG) == {"healthy": True, "message": "Prometheus Server is Healthy."}
    reload(CFG)
    assert seen["method"] == "POST" and seen["path"] == "/-/reload"


def test_not_ready_is_reported_not_raised(monkeypatch) -> None:
    capture(monkeypatch, httpx.Response(503, text="Service Unavailable"))
    assert ready(CFG)["ready"] is False


def test_lifecycle_flag_hint(monkeypatch) -> None:
    capture(monkeypatch, httpx.Response(403, text="Lifecycle API is not enabled."))
    with pytest.raises(PrometheusError, match="web.enable-lifecycle"):
        reload(CFG)


def test_admin_snapshot_and_hint(monkeypatch) -> None:
    seen = capture(monkeypatch, ok({"name": "20261012T000000Z-abc"}))
    assert snapshot(CFG, skip_head=True)["snapshot"] == "20261012T000000Z-abc"
    assert ("skip_head", "true") in seen["form"]
    capture(monkeypatch, httpx.Response(404, text="404 page not found"))
    with pytest.raises(PrometheusError, match="enable-admin-api"):
        clean_tombstones(CFG)


def test_delete_series_requires_bounds(monkeypatch) -> None:
    with pytest.raises(PrometheusError, match="both start and end"):
        delete_series(CFG, ["up"], start="1h", end="")
    seen = capture(monkeypatch, httpx.Response(204))
    out = delete_series(CFG, ['{job="old"}'], start="0", end="60")
    assert seen["path"] == "/api/v1/admin/tsdb/delete_series" and out["deleted"] is True
