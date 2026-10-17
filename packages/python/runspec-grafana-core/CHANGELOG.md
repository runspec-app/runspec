# Changelog — runspec-grafana-core

## [0.1.0]

First release. Pure-Python helpers over the Grafana HTTP API, ported from the
read tools of the [Grafana MCP server](https://github.com/grafana/mcp-grafana)
(no runspec dependency, no runnables). Results are compact by default:

- **Identity and search:** `whoami`, `health`, `search_dashboards`,
  `search_folders`.
- **Dashboards:** `get_dashboard` returns a summary (folder, tags, variables,
  one row per panel, rows unpacked); `include=["queries"]` adds each panel's
  query text and datasource, `panel_id` narrows to one panel, `version` loads a
  saved version, `raw=True` returns the API response. `list_dashboard_versions`
  handles both the list and the Grafana 12 `{"versions": [...]}` shapes.
- **Datasources:** `list_datasources`, `get_datasource` (secrets named, never
  shown), `check_datasources` (health checks, failures reported not raised),
  and `resolve_datasource`, which takes a UID or a name, or picks the default
  (or only) datasource of a type when none is given.
- **Prometheus through Grafana:** `query_prometheus` (instant or range; range
  results summarised per series unless `include_values`),
  `query_prometheus_histogram` (builds the `histogram_quantile` query),
  `list_prometheus_metric_names` (with a regex filter),
  `list_prometheus_label_names`, `list_prometheus_label_values`,
  `list_prometheus_metric_metadata`.
- **Loki through Grafana:** `query_loki_logs` (lines newest first, stream
  labels listed once, each line cut to `max_line_chars`; metric queries
  summarised), `list_loki_label_names`, `list_loki_label_values`,
  `query_loki_stats`, `query_loki_patterns`.
- **Alerting:** `list_alert_rules` (firing first, counts by state, Grafana-managed
  or a datasource's own rules, only active alert instances counted),
  `get_alert_rule`, `list_contact_points`, `list_silences`.
- **Annotations:** `get_annotations`, `get_annotation_tags`, `create_annotation`.
- `api_get`: GET any `/api/` path for what the helpers don't cover.
- Queries go through the datasource proxy, retried through the plugin's
  `/resources` route on 401/403/404/500/502, so only the Grafana token is needed.
- `GrafanaConfig` / `config_from_env`: `GRAFANA_URL` (default
  `http://127.0.0.1:3000`), `GRAFANA_SERVICE_ACCOUNT_TOKEN` (or `TOKEN`, `PAT`,
  `API_KEY`) or basic `GRAFANA_USERNAME`/`GRAFANA_PASSWORD`, `GRAFANA_ORG_ID`,
  `GRAFANA_CA_BUNDLE`, `GRAFANA_TLS_VERIFY`, `GRAFANA_PROXY`, with free-form
  `GRAFANA_<ENV>_*` prefixes.
- Times accept Unix seconds or milliseconds, RFC 3339, `now-1h`, or a duration
  back from now.
