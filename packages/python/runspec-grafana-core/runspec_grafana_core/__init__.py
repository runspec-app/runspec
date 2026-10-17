"""runspec-grafana-core — pure-Python Grafana HTTP API helpers.

This package depends only on ``httpx`` — it has **no dependency on runspec** and
ships **no runspec.toml and no entry points**, so installing it exposes the
helpers for import without surfacing any runnables (it is invisible to
``runspec local`` / ``runspec serve`` discovery). ``runspec-grafana`` depends on
it and wraps each helper as a verb of the ``grafana`` runnable.

The helpers port the read tools of the Grafana MCP server
(https://github.com/grafana/mcp-grafana) onto the Grafana HTTP API: dashboards,
datasources, PromQL and LogQL through the datasource proxy, alert rules,
silences, contact points, and annotations. Every operation takes an explicit
:class:`GrafanaConfig` first argument, so a private wrapper package can inject
credentials, bake in an instance or org, and build narrower runnables on top.
Each function returns plain, compact data; failures raise :class:`GrafanaError`.
"""

from runspec_grafana_core.alerting import RULE_INCLUDE_OPTIONS, RULE_STATES, SILENCE_STATES, get_alert_rule, list_alert_rules, list_contact_points, list_silences
from runspec_grafana_core.annotations import ANNOTATION_TYPES, create_annotation, get_annotation_tags, get_annotations
from runspec_grafana_core.api import api_get, parse_params
from runspec_grafana_core.client import (
    DEFAULT_URL,
    GrafanaConfig,
    GrafanaError,
    config_from_env,
    datasource_request,
    grafana_request,
    parse_duration,
    parse_time,
)
from runspec_grafana_core.dashboards import (
    DASHBOARD_INCLUDE_OPTIONS,
    dashboard_summary,
    get_dashboard,
    health,
    list_dashboard_versions,
    panel_queries,
    search_dashboards,
    search_folders,
    whoami,
)
from runspec_grafana_core.datasources import check_datasources, fetch_datasource, get_datasource, list_datasources, resolve_datasource
from runspec_grafana_core.loki import compact_streams, list_loki_label_names, list_loki_label_values, query_loki_logs, query_loki_patterns, query_loki_stats
from runspec_grafana_core.prometheus import (
    compact_series,
    histogram_query,
    list_prometheus_label_names,
    list_prometheus_label_values,
    list_prometheus_metric_metadata,
    list_prometheus_metric_names,
    query_prometheus,
    query_prometheus_histogram,
)

__all__ = [
    # client / config
    "GrafanaConfig",
    "GrafanaError",
    "config_from_env",
    "grafana_request",
    "datasource_request",
    "parse_time",
    "parse_duration",
    "DEFAULT_URL",
    # identity / search / dashboards
    "whoami",
    "health",
    "search_dashboards",
    "search_folders",
    "get_dashboard",
    "dashboard_summary",
    "panel_queries",
    "list_dashboard_versions",
    "DASHBOARD_INCLUDE_OPTIONS",
    # datasources
    "list_datasources",
    "get_datasource",
    "fetch_datasource",
    "resolve_datasource",
    "check_datasources",
    # prometheus via grafana
    "query_prometheus",
    "query_prometheus_histogram",
    "histogram_query",
    "compact_series",
    "list_prometheus_metric_names",
    "list_prometheus_label_names",
    "list_prometheus_label_values",
    "list_prometheus_metric_metadata",
    # loki via grafana
    "query_loki_logs",
    "compact_streams",
    "list_loki_label_names",
    "list_loki_label_values",
    "query_loki_stats",
    "query_loki_patterns",
    # alerting
    "list_alert_rules",
    "get_alert_rule",
    "list_contact_points",
    "list_silences",
    "RULE_STATES",
    "RULE_INCLUDE_OPTIONS",
    "SILENCE_STATES",
    # annotations
    "get_annotations",
    "get_annotation_tags",
    "create_annotation",
    "ANNOTATION_TYPES",
    # escape hatch
    "api_get",
    "parse_params",
]
