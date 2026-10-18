# Changelog

All notable changes to `runspec-prometheus` are documented here.

## [0.2.0]

- **runspec-console credentials apply again without a scope.** `--url` and
  `--prometheus-token` now declare `PROMETHEUS_URL` and
  `PROMETHEUS_TOKEN`/`PAT` as `env` aliases (and `--env` declares
  `PROMETHEUS_ENV`). The console applies an unscoped credential only to a
  runnable that declares one of its variables, so a `Prometheus` credential was
  never offered to the agent.
- New `--prometheus-user` / `--prometheus-password` (`password` type) for basic
  auth, declaring `PROMETHEUS_USERNAME`/`USER` and `PROMETHEUS_PASSWORD`.
- `--env <label>` still reads `PROMETHEUS_<LABEL>_*` first; only a value typed
  on the command line or sent on the console's secret channel
  (`RUNSPEC_PROMETHEUS_ARG_*`) overrides the environment.
- Needs `runspec-prometheus-core >= 0.2.0`.

## [0.1.0]

First release. One runnable, `prometheus`, with a verb per tool of the official
[Prometheus MCP server](https://github.com/prometheus/prometheus-mcp), ported
onto the Prometheus HTTP API (no proxy process):

- **Reads** (`autonomous`): `query`, `range-query`, `exemplar-query`, `series`,
  `label-names`, `label-values`, `metric-metadata`, `targets-metadata`,
  `list-alerts`, `list-rules`, `list-targets`, `alertmanagers`, `build-info`,
  `runtime-info`, `config`, `flags`, `tsdb-stats`, `wal-replay-status`,
  `healthy`, `ready`, and Thanos `list-stores`.
- **Changes** (`confirm`): `reload`, `snapshot`, `clean-tombstones`.
- **Destructive** (`manual`, never run by an agent): `delete-series`, `quit`.
- Globals `--env` (free-form instance label → `PROMETHEUS_<ENV>_*`), `--url`,
  and a `password`-typed `--prometheus-token` for the runspec-console secret
  channel. With nothing set it talks to `http://127.0.0.1:9090`.
- Not ported: the MCP server's `docs_*` tools (an embedded copy of the
  prometheus/docs repo) and `runbooks_*` tools (Agent Skills written against its
  own tool names).
