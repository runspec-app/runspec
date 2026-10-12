# Changelog

All notable changes to `runspec-prometheus` are documented here.

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
