"""runspec-prometheus-core — pure-Python Prometheus HTTP API helpers.

This package depends only on ``httpx`` — it has **no dependency on runspec** and
ships **no runspec.toml and no entry points**, so installing it exposes the
helpers for import without surfacing any runnables (it is invisible to
``runspec local`` / ``runspec serve`` discovery). ``runspec-prometheus`` depends
on it and wraps each helper as a verb of the ``prometheus`` runnable.

The helpers port the tools of the official Prometheus MCP server
(https://github.com/prometheus/prometheus-mcp) onto the public HTTP API. Every
operation takes an explicit :class:`PrometheusConfig` first argument, so a
private wrapper package can inject credentials, bake in an instance or tenant,
and build "one runnable per team dashboard" on top. Each function returns plain
data; failures raise :class:`PrometheusError`.
"""

from runspec_prometheus_core.client import (
    DEFAULT_RANGE_POINTS,
    DEFAULT_URL,
    PrometheusConfig,
    PrometheusError,
    auto_step,
    config_from_env,
    parse_duration,
    parse_time,
    prometheus_request,
)
from runspec_prometheus_core.management import clean_tombstones, delete_series, healthy, quit, ready, reload, snapshot
from runspec_prometheus_core.metadata import metric_metadata, targets_metadata
from runspec_prometheus_core.query import exemplar_query, label_names, label_values, query, range_query, series
from runspec_prometheus_core.status import (
    alertmanagers,
    build_info,
    flags,
    get_config,
    list_alerts,
    list_rules,
    list_stores,
    list_targets,
    runtime_info,
    tsdb_stats,
    wal_replay_status,
)

__all__ = [
    # client / config
    "PrometheusConfig",
    "PrometheusError",
    "config_from_env",
    "prometheus_request",
    "parse_time",
    "parse_duration",
    "auto_step",
    "DEFAULT_URL",
    "DEFAULT_RANGE_POINTS",
    # queries
    "query",
    "range_query",
    "exemplar_query",
    "series",
    "label_names",
    "label_values",
    # metadata
    "metric_metadata",
    "targets_metadata",
    # status
    "list_alerts",
    "list_rules",
    "list_targets",
    "alertmanagers",
    "build_info",
    "runtime_info",
    "get_config",
    "flags",
    "tsdb_stats",
    "wal_replay_status",
    "list_stores",
    # management / admin
    "healthy",
    "ready",
    "reload",
    "quit",
    "snapshot",
    "delete_series",
    "clean_tombstones",
]
