# Changelog

All notable changes to `runspec-grafana` are documented here.

## [0.1.0]

First release. One runnable, `grafana`, with a verb per read tool of the
[Grafana MCP server](https://github.com/grafana/mcp-grafana), ported onto the
Grafana HTTP API (no proxy process):

- **Reads** (`autonomous`): `whoami`, `health`, `search-dashboards`,
  `search-folders`, `get-dashboard`, `list-dashboard-versions`,
  `list-datasources`, `get-datasource`, `check-datasource`,
  `query-prometheus`, `query-prometheus-histogram`,
  `list-prometheus-metric-names`, `list-prometheus-label-names`,
  `list-prometheus-label-values`, `list-prometheus-metric-metadata`,
  `query-loki-logs`, `list-loki-label-names`, `list-loki-label-values`,
  `query-loki-stats`, `query-loki-patterns`, `list-alert-rules`,
  `get-alert-rule`, `list-contact-points`, `list-silences`,
  `get-annotations`, `get-annotation-tags`, and `api-get` (GET any `/api/`
  path).
- **Changes** (`confirm`): `create-annotation`.
- Compact output by default, with `--include` / `--raw` / `--include-values` /
  `--limit` for more; a person at a terminal gets text, everyone else JSON.
- Globals `--env` (free-form instance label → `GRAFANA_<ENV>_*`), `--url`,
  `--org-id`, and a `password`-typed `--grafana-token` for the runspec-console
  secret channel. With nothing set it talks to `http://127.0.0.1:3000`.
- Not ported yet: dashboard and alert-rule writes, silences writes, OnCall,
  Incident, Tempo, Pyroscope, SQL/CloudWatch/Elasticsearch queries, panel
  rendering, and the Grafana Assistant.
