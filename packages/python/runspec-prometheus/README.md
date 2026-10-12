# runspec-prometheus

A Prometheus runnable for [runspec](https://pypi.org/project/runspec/), ported
from the official [Prometheus MCP server](https://github.com/prometheus/prometheus-mcp)
onto the Prometheus HTTP API — **not** proxying that server. One runnable —
**`prometheus`** — with a verb per MCP tool. `pip install` it into a venv and
it's discoverable by `runspec local`, `runspec serve` (MCP), and runspec-console.

`runspec serve` flattens each verb into a leaf MCP tool (`prometheus_query`,
`prometheus_list-alerts`, …). It works against Prometheus and API-compatible
servers (Thanos Query, Mimir, Cortex).

## Install

```
pip install runspec-prometheus
```

## Verbs

Reads (all `autonomous`):

| Area | Verbs |
|---|---|
| Queries | `query`, `range-query`, `exemplar-query` |
| Discovery | `series`, `label-names`, `label-values`, `metric-metadata`, `targets-metadata` |
| Alerting | `list-alerts`, `list-rules`, `alertmanagers` |
| Targets | `list-targets` (filter by `--health down`, `--scrape-pool`, `--state`) |
| Server | `build-info`, `runtime-info`, `config`, `flags`, `tsdb-stats`, `wal-replay-status`, `healthy`, `ready` |
| Thanos | `list-stores` |

Changes: `reload`, `snapshot`, `clean-tombstones` are `confirm` (an agent asks
first); `delete-series` and `quit` are `manual` (refused to an agent, run from the
command line or the console's Forms tab). Prometheus itself gates these too:
`reload`/`quit` need `--web.enable-lifecycle`, the TSDB admin verbs need
`--web.enable-admin-api`. `delete-series` refuses to run without both `--start`
and `--end`.

Times accept Unix seconds, RFC 3339, or a duration back from now (`5m`,
`1h30m`, `2d`). A `range-query` with no `--step` picks one that gives about 250
points. Query and list results are capped by `--limit` (default 100, `0` = all);
the output says `truncated: true` when it cut something.

## Auth — env vars

```
export PROMETHEUS_URL=https://prometheus.example.com   # default http://127.0.0.1:9090
export PROMETHEUS_TOKEN=<bearer token>                 # or:
export PROMETHEUS_USERNAME=<user> PROMETHEUS_PASSWORD=<password>
export PROMETHEUS_ORG_ID=<tenant>                      # Mimir/Cortex X-Scope-OrgID
export PROMETHEUS_CA_BUNDLE=/etc/pki/internal-ca.pem   # private CA
```

With no URL set it talks to `http://127.0.0.1:9090`, so on a fleet host that
runs Prometheus it works with no configuration at all. `--env <label>` reads
`PROMETHEUS_<LABEL>_*` first (`--env prod-eu` → `PROMETHEUS_PROD_EU_URL`).
`--url` and `--prometheus-token` override the environment; the token arg is a
`password` type, so it comes through the runspec-console secret channel and
never on a command line. A runspec-console credential labelled `Prometheus`
supplies `PROMETHEUS_TOKEN` (token kind) or `PROMETHEUS_USERNAME`/`PASSWORD`
(username + password kind) with no extra configuration.

## Building your own runnables

The logic lives in [`runspec-prometheus-core`](../runspec-prometheus-core), which
has no runspec dependency and surfaces no runnables. A private package can
depend on it alone, build a `PrometheusConfig` in code, and wrap the helpers as
narrower runnables — for example a `node-health` runnable that runs a fixed set
of PromQL queries for one team:

```python
from runspec_prometheus_core import config_from_env, query

cfg = config_from_env("prod")
down = query(cfg, 'up{team="payments"} == 0', limit=0)["result"]
```
