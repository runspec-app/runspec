# runspec-grafana

A Grafana runnable for [runspec](https://pypi.org/project/runspec/), ported from
the read tools of the [Grafana MCP server](https://github.com/grafana/mcp-grafana)
onto the Grafana HTTP API — **not** proxying that server. One runnable —
**`grafana`** — with a verb per tool. `pip install` it into a venv and it's
discoverable by `runspec local`, `runspec serve` (MCP), and runspec-console.

`runspec serve` flattens each verb into a leaf MCP tool
(`grafana_search-dashboards`, `grafana_query-prometheus`, …).

## Install

```
pip install runspec-grafana
```

## Verbs

All `autonomous` (read-only) except `create-annotation`, which is `confirm`.

| Area | Verbs |
|---|---|
| Identity | `whoami`, `health` |
| Dashboards | `search-dashboards`, `search-folders`, `get-dashboard`, `list-dashboard-versions` |
| Datasources | `list-datasources`, `get-datasource`, `check-datasource` |
| Prometheus | `query-prometheus`, `query-prometheus-histogram`, `list-prometheus-metric-names`, `list-prometheus-label-names`, `list-prometheus-label-values`, `list-prometheus-metric-metadata` |
| Loki | `query-loki-logs`, `list-loki-label-names`, `list-loki-label-values`, `query-loki-stats`, `query-loki-patterns` |
| Alerting | `list-alert-rules`, `get-alert-rule`, `list-contact-points`, `list-silences` |
| Annotations | `get-annotations`, `get-annotation-tags`, `create-annotation` |
| Anything else | `api-get` (GET any `/api/` path, returned as-is) |

PromQL and LogQL run through Grafana's datasource proxy, so only the Grafana
token is needed — never the datasource's own credentials. `--datasource` takes a
UID or a name; without it the default (or only) Prometheus or Loki datasource is
used.

## Output

Results are compact by default, because an agent re-reads every tool result on
each later step:

- `get-dashboard` returns a summary: folder, tags, variables, and one row per
  panel. `--include queries` adds each panel's query text, `--panel <id>` keeps
  one panel, `--raw` returns the full dashboard JSON.
- `query-prometheus --query-type range` summarises each series (points, first,
  last, min, max); `--include-values` returns every sample.
- `query-loki-logs` lists each stream's labels once and gives the lines newest
  first, each cut to `--max-line-chars` (500).
- `list-alert-rules` puts firing rules first with counts by state;
  `--include alerts` adds the active alert instances.
- Every list takes `--limit` and says `truncated: true` when it cut something.

A person at a terminal gets readable text; agents, pipes, the console's Forms
tab and schedulers get JSON.

Times accept Unix seconds or milliseconds, RFC 3339, `now-1h`, or a duration
back from now (`5m`, `1h30m`).

## Auth — env vars

```
export GRAFANA_URL=https://grafana.example.com            # default http://127.0.0.1:3000
export GRAFANA_SERVICE_ACCOUNT_TOKEN=glsa_...             # or GRAFANA_TOKEN, or:
export GRAFANA_USERNAME=<user> GRAFANA_PASSWORD=<password>
export GRAFANA_ORG_ID=2                                   # X-Grafana-Org-Id
export GRAFANA_CA_BUNDLE=/etc/pki/internal-ca.pem         # private CA
```

Use a [service account](https://grafana.com/docs/grafana/latest/administration/service-accounts/)
token. The **Viewer** role covers every read except `list-dashboard-versions`;
that and `create-annotation` need **Editor**.

`--env <label>` reads `GRAFANA_<LABEL>_*` first (`--env prod-eu` →
`GRAFANA_PROD_EU_URL`). `--url`, `--org-id`, `--grafana-token`,
`--grafana-user` and `--grafana-password` override the environment; the token
and password args are `password` type, so they come through the
runspec-console secret channel and never on a command line.

Each auth arg declares the variables above as its `env`, so runspec-console
applies a matching credential with no extra configuration: a token-kind
credential labelled `Grafana` (→ `GRAFANA_TOKEN`) or `Grafana Service Account`
(→ `GRAFANA_SERVICE_ACCOUNT_TOKEN`), or a username + password credential
labelled `Grafana` (→ `GRAFANA_USERNAME`/`PASSWORD`). The console's Env files
window lists the same variables for a venv's `.runspec_env`.

## Building your own runnables

The logic lives in [`runspec-grafana-core`](../runspec-grafana-core), which has
no runspec dependency and surfaces no runnables. A private package can depend on
it alone, build a `GrafanaConfig` in code, and wrap the helpers as narrower
runnables — for example a `checkout-errors` runnable that runs one fixed LogQL
query for a team:

```python
from runspec_grafana_core import config_from_env, query_loki_logs

cfg = config_from_env("prod")
errors = query_loki_logs(cfg, '{app="checkout"} |= "level=error"', start="30m", limit=20)
```
