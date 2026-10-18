# Changelog

All notable changes to `runspec-grafana` are documented here.

## [0.2.0]

- **runspec-console credentials apply again without a scope.** The auth args
  now declare the variables they read as `env` aliases: `--url` →
  `GRAFANA_URL`, `--org-id` → `GRAFANA_ORG_ID`, `--grafana-token` →
  `GRAFANA_SERVICE_ACCOUNT_TOKEN`/`TOKEN`/`PAT`/`API_KEY`. The console applies an
  unscoped credential only to a runnable that declares one of its variables, so
  a credential such as `Grafana Service Account` was never offered to the
  agent. The console's Env files window now lists these variables too.
- New `--grafana-user` / `--grafana-password` (`password` type) for basic auth,
  declaring `GRAFANA_USERNAME`/`USER` and `GRAFANA_PASSWORD`, so a username +
  password credential labelled `Grafana` applies the same way.
- `--env <label>` still reads `GRAFANA_<LABEL>_*` before the unprefixed
  variables: a value runspec read from a declared alias is left to the core,
  and only a value typed on the command line or sent on the console's secret
  channel (`RUNSPEC_GRAFANA_ARG_*`) overrides the environment.
- Needs `runspec-grafana-core >= 0.2.0`.

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
