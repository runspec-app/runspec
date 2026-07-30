# Telemetry — OpenTelemetry tracing

The console can export a **trace per agent turn** over
[OpenTelemetry](https://opentelemetry.io/) (OTLP) so you can see, in your
observability backend, exactly what the agent did: the model calls it made, the
tokens they cost, the tools it ran, and how each was gated. Spans follow the
OpenTelemetry **GenAI semantic conventions** (`gen_ai.*`), so a GenAI-aware
backend renders the model and tool calls natively.

It is **vendor-neutral**: point it at any OTel backend —
[LangSmith](https://docs.smith.langchain.com/observability/how_to_guides/trace_with_opentelemetry),
Langfuse, Arize Phoenix, Jaeger, Grafana Tempo, an OpenTelemetry Collector, …

Tracing is **off by default** and strictly opt-in: it exports data to an
external backend, so nothing leaves the machine until you enable it.

## Install

Telemetry needs the `otel` extra (the OpenTelemetry SDK + OTLP/HTTP exporter):

```
pip install runspec-console[otel]
```

Until it's installed, the Telemetry tab shows an "install the extra" hint and
tracing stays a no-op even if enabled.

## Enable it

**Settings → Telemetry**, or edit `config.toml` directly:

```toml
[telemetry]
enabled = true
endpoint = "https://api.smith.langchain.com/otel/v1/traces"
protocol = "http/protobuf"
service_name = "runspec-console"
sample_ratio = 1.0
capture_content = false
# For an internal backend behind a private CA / TLS-intercepting proxy:
# ca_bundle = "C:/certs/internal-ca.pem"
# tls_verify = true

[telemetry.headers]
"x-api-key" = "ls-..."
"Langsmith-Project" = "runspec-ops"
```

| Key | Meaning |
|---|---|
| `enabled` | Master switch. Default **false**. |
| `endpoint` | OTLP **traces** URL. Blank ⇒ the SDK reads `OTEL_EXPORTER_OTLP_ENDPOINT`. |
| `headers` | Extra HTTP headers on every export — e.g. a LangSmith API key + project. |
| `protocol` | `http/protobuf` (the shipped exporter). |
| `service_name` | `service.name` resource attribute. Default `runspec-console`. |
| `sample_ratio` | Head sampling ratio, 0–1. Default 1.0 (trace everything). |
| `ca_bundle` | Path to a CA bundle for a private-CA / TLS-proxy endpoint. |
| `tls_verify` | Verify the server certificate. Default true. |
| `capture_content` | Attach prompt / completion / tool-argument **text**. Default **false** — see [Privacy](#privacy). |

### Standard `OTEL_*` environment variables

The OpenTelemetry SDK reads the usual environment variables
(`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`,
`OTEL_SERVICE_NAME`, …). The rule is **config wins, env fills the gaps**: any
`[telemetry]` value you set is passed to the exporter; anything you leave blank
falls through to the matching `OTEL_*` variable. The one exception is `enabled`
— it has no OTel analog, so tracing is never turned on by an env var alone.

This means you can drive everything from env vars if you prefer: set
`[telemetry] enabled = true` and leave `endpoint`/`headers` blank.

## What gets traced

Each agent turn — a chat turn, a **trigger** turn, or a **schedule** turn —
produces one trace:

```
runspec.agent.turn                     (root; runspec.turn.kind = chat|trigger|schedule)
├─ chat <model>                        (one LLM request per loop iteration)
│    gen_ai.request.model, gen_ai.response.finish_reasons,
│    gen_ai.usage.input_tokens / output_tokens (+ runspec.usage.cache_*)
└─ execute_tool <tool>                 (one per tool call)
     gen_ai.tool.name, gen_ai.tool.call.id,
     runspec.tool.autonomy_decision (run|confirm|supervised|manual),
     runspec.tool.operator_decision (approved|edited|denied),
     runspec.provenance.* (trigger / agent / runbook)
```

Tool execution dispatches onto a worker thread, but the tool span is opened
before that hand-off, so a runnable's execution stays **parented to its turn's
trace** — the whole turn is one connected tree.

## Choosing a backend

Because the console emits **standard OTLP**, switching backends is only an
`endpoint` change — no code, no lock-in. You can point it at several in turn and
compare. The main axes are *hosted vs self-hosted* and *LLM-aware vs general
tracing*:

| Backend | Hosting | LLM-aware | Best for |
|---|---|---|---|
| **LangSmith** | SaaS (enterprise self-host) | Yes — conversation view, token/cost, evals | Deep LLM debugging & evaluation |
| **Langfuse**, **Arize Phoenix** | Self-hosted, OSS | Yes — render prompts/completions, token/cost | A self-hosted, LangSmith-*like* experience |
| **Grafana Tempo**, **Jaeger** | Self-hosted, OSS | No — general trace UI | Operational / audit tracing; already running Grafana |

All of them ingest OTLP, so any of the above is just a different `endpoint`.
A general trace UI (Tempo / Jaeger) shows the full `turn → llm → tool` tree with
attributes, timings, decisions, and provenance — it just won't render a pretty
conversation or token/cost rollups the way an LLM-native tool does.

### Grafana + Tempo

[Grafana](https://grafana.com/) doesn't store traces itself — it queries a
tracing backend. Pair it with **[Grafana Tempo](https://grafana.com/oss/tempo/)**
(or Jaeger), which accepts OTLP directly. Point the console at Tempo's OTLP/HTTP
receiver:

```toml
[telemetry]
enabled = true
endpoint = "http://tempo:4318/v1/traces"   # Tempo's OTLP/HTTP port (4318)
service_name = "runspec-console"
```

Then add Tempo as a data source in Grafana (Connections → Data sources → Tempo)
and open **Explore** to search traces — filter by `service.name =
runspec-console`, or by any span attribute the console sets
(`runspec.turn.kind`, `gen_ai.request.model`, `runspec.tool.autonomy_decision`,
`runspec.provenance.trigger`, …) via TraceQL. Each trace is one agent turn.

### A note on Prometheus

[Prometheus](https://prometheus.io/) is a **metrics** store — it does not ingest
traces, and this feature exports traces only, so Prometheus is not a backend for
it. If you later want aggregate metrics (request rate / error rate / duration per
tool or model), the standard path is to run an **OpenTelemetry Collector** with
the `spanmetrics` connector (or Tempo's metrics-generator) to *derive* those
metrics from the same trace stream and expose them to Prometheus — no change to
the console. That's an ops add-on, separate from this tracing feature.

## Providing the API key without shipping it

A shared backend like LangSmith needs an API key, and there's no good answer that
puts that key in `config.toml` in the clear — a config seed ships in package data
and a Config-Sync repo is checked out on every machine, so a key placed directly
in `[telemetry.headers]` would leak. Two patterns solve this; the first removes
the problem entirely.

### Relay through an internal OTel Collector (recommended)

Point every console at an internal **[OpenTelemetry
Collector](https://opentelemetry.io/docs/collector/)** and let *it* hold the one
LangSmith key and add the auth header on export. The desktops carry **no secret
at all** — only the Collector's internal address:

```toml
[telemetry]
enabled = true
endpoint = "http://otel-collector.internal:4318/v1/traces"
```

The Collector holds the key once (from its own environment / secret store) and
forwards to LangSmith — and can fan out to Tempo/Grafana at the same time:

```yaml
# otel-collector config (on the Collector host, not the desktops)
receivers:
  otlp:
    protocols:
      http:
exporters:
  otlphttp/langsmith:
    endpoint: https://api.smith.langchain.com/otel
    headers:
      x-api-key: ${env:LANGSMITH_API_KEY}   # the key lives here, once
  otlp/tempo:
    endpoint: tempo:4317
service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [otlphttp/langsmith, otlp/tempo]
```

Now the key lives in one trusted place, rotating it is one change (not a fleet
re-push), and desktop→Collector auth is a cheap *internal* problem (network
restriction, or an internal-only token/cert you can seed freely because it isn't
the real key). This is also the standard production OTel topology.

### Resolve the key from a command (`api_key_command`)

If a Collector isn't available, resolve the key **at runtime** from a command
instead of storing it — mirroring the model `[llm] api_key_command`. The config
carries only the *command*, never the key:

```toml
[telemetry]
enabled = true
endpoint = "https://api.smith.langchain.com/otel/v1/traces"
api_key_command = "keyring get langsmith api-key"   # or: vault read -field=key secret/langsmith
api_key_header = "x-api-key"                         # default; where the key is injected
```

At startup the console runs the command, takes its stdout as the key, and injects
it under `api_key_header`. So a config seed / Config-Sync repo can distribute the
`api_key_command` string safely — each machine resolves the real key locally from
wherever your org keeps secrets: the OS keychain (`keyring get …`), a secret
manager (`vault read …`, `az keyvault secret show …`, `aws secretsmanager …`), or
a small helper shipped as a console-script in the venv. The command runs with the
venv on `PATH` (and `RUNSPEC_PYTHON` / `RUNSPEC_VENV` set), so a bundled helper
resolves wherever the venv was created. If the command fails, tracing is disabled
for the session rather than exporting unauthenticated.

## Privacy

Sending agent data to an external backend is a real consideration — prompts and
tool arguments can contain credentials or sensitive content. Two defaults keep
this safe:

- **`capture_content = false` (default).** Spans carry **structure only** —
  model names, token counts, tool names, autonomy/operator decisions, finish
  reasons, durations. No prompt text, no completion text, no tool arguments.
  You still get the full shape of every turn.
- **When you turn `capture_content` on**, prompt / completion / tool-argument
  text is attached — and tool arguments are **scrubbed** first: values under
  secret-looking keys (`password`, `token`, `secret`, `api_key`,
  `authorization`, …) become `«redacted»`. Resolved credentials injected by the
  console (the secret env channel, `sudo`/`su` credentials) live *below* the
  tool-input layer the agent sees and never reach a span at all.

Only enable content capture when your backend is trusted. An API key placed
directly in `[telemetry.headers]` is stored in `config.toml` (like the model API
key), **not** the OS keychain — so for a shared key across many machines, prefer
the Collector relay or `api_key_command` above rather than putting the key in the
file.

## Enriching spans from a custom adapter

A [custom model adapter](console-adapters.md) can add provider-specific detail
to the live LLM span without any wiring — the LLM span is the current OTel span
while your adapter's `stream_with_tools` runs. Call the no-op-safe helpers on
`runspec_console.observability`. For a full walkthrough (and an agent-ready
brief), see [Telemetry for a custom adapter](console-adapter-telemetry.md):

```python
from runspec_console import observability

# inside your adapter, after the provider responds:
observability.set_attributes(**{
    "gen_ai.response.id": response_id,
    "mycorp.gateway.request_id": request_id,
})
observability.add_completion(text)   # honours capture_content; no-op when off
```

Every helper is a **complete no-op** when telemetry is disabled or the `otel`
extra isn't installed, so you can call them unconditionally and your plugin
never needs OpenTelemetry as a hard dependency.

## Troubleshooting

- **No spans arrive.** Confirm the `otel` extra is installed (the Telemetry tab
  says so), `enabled = true`, and the `endpoint` is the **traces** path your
  backend expects (often `.../v1/traces`). Spans are batched and flushed on exit.
- **TLS errors to an internal backend.** Set `ca_bundle` to your CA `.pem`. A
  short session's spans flush on shutdown; a crash may drop the last batch.
- **A page behind SSH, not a backend.** Telemetry is only the tracing exporter;
  it doesn't change how the agent reaches hosts.
