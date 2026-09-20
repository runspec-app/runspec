# runspec-logops

Log-condensing and code-mapping runnables for
[runspec](https://pypi.org/project/runspec/) — cheap application-support triage
**from a log file**. The runnables do the heavy lifting on the host where the log
lives and return small structured JSON, so the runspec-console agent reads
condensed conclusions, not raw logs or code. `pip install` it into a venv and the
runnables are discoverable by `runspec local`, `runspec serve` (MCP), and
runspec-console.

## Install

```
pip install runspec-logops
```

## Runnables

| Runnable | Autonomy | What it does |
|---|---|---|
| `summarize-log` | autonomous | Streams a log and clusters repeated errors by a normalised *signature*, returning a bounded top-N digest (counts, first/last timestamp, one sample each) whose size is independent of input size. Supports a `--level` floor and a `--since`/`--until` window. |
| `map-trace` | autonomous | Resolves a stack trace — or a `summarize-log` signature like `NullPointerException@com.acme.OrderSvc.price` — to just the source snippets around each frame in a checkout (`--repo`). |
| `bundle-digest` | confirm | Zips a digest + snippets + manifest for transfer (e.g. via runspec-console's `download_file`). |

The operator's flow: use the console's filtered fetch to colocate the log with
the code (remote or local), then run these over it.

```
summarize-log --path /var/log/app/app.log --level error --top 20
map-trace --repo /srv/checkouts/orders --trace "NullPointerException@com.acme.OrderSvc.price"
```

## Public Python API

The pure logic is in **[`runspec-logops-core`](../runspec-logops-core)** — no
`runspec` dependency, no `runspec.toml`, no entry points, so installing it
exposes the helpers for import without surfacing any runnables:

```python
from runspec_logops_core import summarize_log, map_trace_to_sources, build_bundle

digest = summarize_log("/var/log/app/app.log", level="error", top=20)
```

All params are plain (`summarize_log(path, *, level, since, until, top,
max_sample_lines, max_bytes)`, etc.), so a private (e.g. Nexus-hosted) wrapper
can bake in corporate defaults / paths / redaction and publish only its own
runnables.

## Development

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest
```
