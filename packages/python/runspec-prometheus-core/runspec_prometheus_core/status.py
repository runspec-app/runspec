"""status.py — read-only server state: alerts, rules, targets, build/runtime info, config, flags, TSDB stats."""

from __future__ import annotations

from typing import Any

from runspec_prometheus_core.client import PrometheusConfig, prometheus_request, truncate


def _data(config: PrometheusConfig, path: str, params: dict[str, Any] | None = None) -> Any:
    return prometheus_request("GET", path, config=config, params=params)["data"]


def list_alerts(config: PrometheusConfig, state: str | None = None) -> dict[str, Any]:
    """Active alerts (firing and pending). ``state`` filters to ``firing`` or ``pending``."""
    alerts = (_data(config, "/api/v1/alerts") or {}).get("alerts") or []
    if state:
        alerts = [a for a in alerts if a.get("state") == state]
    return {"count": len(alerts), "alerts": alerts}


def list_rules(
    config: PrometheusConfig,
    rule_type: str | None = None,
    rule_names: list[str] | None = None,
    rule_groups: list[str] | None = None,
    exclude_alerts: bool = False,
) -> dict[str, Any]:
    """Loaded alerting and recording rules, by group. ``rule_type`` is ``alert``
    or ``record``; ``rule_names`` / ``rule_groups`` narrow by name;
    ``exclude_alerts`` drops each alerting rule's active-alert list."""
    params = {
        "type": rule_type,
        "rule_name[]": list(rule_names or []),
        "rule_group[]": list(rule_groups or []),
        "exclude_alerts": True if exclude_alerts else None,
    }
    groups = (_data(config, "/api/v1/rules", params) or {}).get("groups") or []
    return {"group_count": len(groups), "rule_count": sum(len(g.get("rules") or []) for g in groups), "groups": groups}


def list_targets(config: PrometheusConfig, state: str | None = "active", scrape_pool: str | None = None, health: str | None = None, limit: int | None = 200) -> dict[str, Any]:
    """Scrape targets. ``state`` is ``active`` (default), ``dropped`` or ``any``;
    ``health`` filters active targets to ``up``/``down``/``unknown``; at most
    ``limit`` targets of each kind (0 = all)."""
    data = _data(config, "/api/v1/targets", {"state": state, "scrapePool": scrape_pool}) or {}
    out: dict[str, Any] = {}
    active = data.get("activeTargets") or []
    if health:
        active = [t for t in active if t.get("health") == health]
    if state in (None, "active", "any") or active:
        kept, cut = truncate(active, limit)
        out.update(active_count=len(active), active_truncated=cut, active=kept)
    dropped = data.get("droppedTargets") or []
    if state in ("dropped", "any") or dropped:
        kept, cut = truncate(dropped, limit)
        out.update(dropped_count=data.get("droppedTargetCounts") or len(dropped), dropped_truncated=cut, dropped=kept)
    return out


def alertmanagers(config: PrometheusConfig) -> Any:
    """Active and dropped Alertmanager endpoints Prometheus has discovered."""
    return _data(config, "/api/v1/alertmanagers")


def build_info(config: PrometheusConfig) -> Any:
    """Version, revision, branch and Go version of the server."""
    return _data(config, "/api/v1/status/buildinfo")


def runtime_info(config: PrometheusConfig) -> Any:
    """Start time, storage retention, goroutines, reload status, and similar."""
    return _data(config, "/api/v1/status/runtimeinfo")


def get_config(config: PrometheusConfig) -> dict[str, Any]:
    """The loaded configuration file, as YAML text under ``yaml``."""
    return _data(config, "/api/v1/status/config") or {}


def flags(config: PrometheusConfig) -> Any:
    """Command-line flag values the server was started with."""
    return _data(config, "/api/v1/status/flags")


def tsdb_stats(config: PrometheusConfig, limit: int | None = 10) -> Any:
    """Head-block cardinality stats: top metric names, label names and label
    pairs by series count (``limit`` entries each)."""
    return _data(config, "/api/v1/status/tsdb", {"limit": limit if limit and limit > 0 else None})


def wal_replay_status(config: PrometheusConfig) -> Any:
    """Progress of the write-ahead-log replay at startup."""
    return _data(config, "/api/v1/status/walreplay")


def list_stores(config: PrometheusConfig) -> Any:
    """Thanos Query only: the store API servers it fans out to."""
    return _data(config, "/api/v1/stores")
