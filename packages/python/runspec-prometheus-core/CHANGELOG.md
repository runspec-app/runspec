# Changelog — runspec-prometheus-core

## [0.2.0]

- `config_from_env` takes `username` / `password` keyword overrides, which win
  over `PROMETHEUS_USERNAME` / `PROMETHEUS_PASSWORD` like `url` / `token`.

## [0.1.0]

First release. Pure-Python helpers over the Prometheus HTTP API, ported from the
tools of the official [Prometheus MCP server](https://github.com/prometheus/prometheus-mcp)
(no runspec dependency, no runnables):

- **Queries:** `query`, `range_query`, `exemplar_query`, `series`, `label_names`,
  `label_values` — times accept Unix seconds, RFC 3339, or a duration back from
  now (`5m`, `1h30m`); a range query with no `step` picks one that gives about
  250 points; result lists are capped by `limit` (`truncated` says so).
- **Metadata:** `metric_metadata`, `targets_metadata`.
- **Status:** `list_alerts`, `list_rules`, `list_targets`, `alertmanagers`,
  `build_info`, `runtime_info`, `get_config`, `flags`, `tsdb_stats`,
  `wal_replay_status`, and Thanos `list_stores`.
- **Management:** `healthy`, `ready`, `reload`, `quit`.
- **TSDB admin:** `snapshot`, `delete_series` (refuses without both start and
  end, like the MCP server), `clean_tombstones`.
- `PrometheusConfig` / `config_from_env`: `PROMETHEUS_URL` (default
  `http://127.0.0.1:9090`), bearer `PROMETHEUS_TOKEN` (or `PROMETHEUS_PAT`) or
  basic `PROMETHEUS_USERNAME`/`PROMETHEUS_PASSWORD`, `PROMETHEUS_ORG_ID`
  (`X-Scope-OrgID` for Mimir/Cortex), `PROMETHEUS_CA_BUNDLE`,
  `PROMETHEUS_TLS_VERIFY`, `PROMETHEUS_PROXY`, with free-form
  `PROMETHEUS_<ENV>_*` prefixes.
