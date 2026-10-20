# Telemetry — OpenTelemetry tracing & metrics

The console can export a **trace per agent turn** over
[OpenTelemetry](https://opentelemetry.io/) (OTLP) so you can see, in your
observability backend, exactly what the agent did: the model calls it made, the
tokens they cost, the tools it ran, and how each was gated. Spans follow the
OpenTelemetry **GenAI semantic conventions** (`gen_ai.*`), so a GenAI-aware
backend renders the model and tool calls natively.

It can **also export metrics** — per-tool-run and per-turn counters and duration
histograms — as a *separate* OTLP signal with its own endpoint, so you can drive
live dashboards/alerts in a metrics backend (Grafana, Prometheus-via-OTLP) even
when your traces go somewhere that only ingests traces (like LangSmith). See
[Metrics](#metrics).

It is **vendor-neutral**: point it at any OTel backend —
[LangSmith](https://docs.smith.langchain.com/observability/how_to_guides/trace_with_opentelemetry),
Langfuse, Arize Phoenix, Jaeger, Grafana Tempo, an OpenTelemetry Collector, …

Tracing is **off by default** and strictly opt-in: it exports data to an
external backend, so nothing leaves the machine until you enable it.

> **Want private, local analysis?** Run [Arize Phoenix](https://github.com/Arize-ai/phoenix)
> yourself (in its own venv) and point this export at it on `localhost` — the
> Settings → Telemetry **Use local Phoenix** button fills it in. Nothing leaves
> the machine. See [Local trace analysis with Phoenix](console-phoenix.md).

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
service_name = "runspec-console"   # blank ⇒ this install's app name (brand)
label = "ops-console-1"            # this instance's service.instance.id (blank ⇒ hostname)
sample_ratio = 1.0
capture_content = false
max_content_chars = 0   # 0 = capture full text; set a ceiling to bound span size
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
| `endpoint` | OTLP **traces** URL. A base URL ending in `/otlp` or with no path gets `/v1/traces` added. Blank ⇒ the SDK reads `OTEL_EXPORTER_OTLP_ENDPOINT`. |
| `headers` | Extra HTTP headers on every export — e.g. a LangSmith API key + project. |
| `protocol` | `http/protobuf` (the shipped exporter). |
| `service_name` | `service.name` resource attribute. Blank (default) ⇒ the install's **branded app slug** (the same source as the config directory + window title), so a white-label build self-identifies; `runspec-console` on a default install. |
| `label` | This console's `service.instance.id`. Blank (default) ⇒ the machine hostname. Set it to run **several distinct consoles** (even on one machine) as separate, named streams. See [Resource identity](#resource-identity). |
| `project_name` | The OpenInference `openinference.project.name` resource attribute — the project Phoenix and Arize AX file traces under. Blank (default) ⇒ unset (the backend's default project). |
| `sample_ratio` | Head sampling ratio, 0–1. Default 1.0 (trace everything). |
| `credential` | A named **Credential** id (secret in the OS keychain) injected as an auth header at init — the recommended per-user path. With `credential_header` set, the secret is sent raw under that header (e.g. `x-api-key`); otherwise `userpass` ⇒ `Authorization: Basic`, `token` ⇒ `Bearer`. See [Credentials](#credentials-recommended). |
| `credential_header` | Header name for `credential`'s secret (blank ⇒ `Authorization` with the scheme above). |
| `header_credentials` | Array of `{header, credential}` for sending several keychain secrets under several headers. |
| `ca_bundle` | Path to a CA bundle for a private-CA / TLS-proxy endpoint. |
| `tls_verify` | Verify the server certificate. Default true. |
| `capture_content` | Attach prompt / completion / tool-argument / tool-result **text**. Default **false** — see [Privacy](#privacy). |
| `max_content_chars` | Ceiling on each captured content value (prompt / completion / tool args). **0 (default) = no limit** — the trace shows the *full* prompt, not a slice. Set a positive value only to bound span size. |

### Standard `OTEL_*` environment variables

The OpenTelemetry SDK reads the usual environment variables
(`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`,
`OTEL_SERVICE_NAME`, …). The rule is **config wins, env fills the gaps**: any
`[telemetry]` value you set is passed to the exporter; anything you leave blank
falls through to the matching `OTEL_*` variable. The one exception is `enabled`
— it has no OTel analog, so tracing is never turned on by an env var alone.

This means you can drive everything from env vars if you prefer: set
`[telemetry] enabled = true` and leave `endpoint`/`headers` blank.

## Resource identity

Every span **and** metric the console exports carries a resource that identifies
*which* console produced it. This matters most for [metrics](#metrics): multiple
consoles are separate processes, and if they all exported under one identical
resource their OTLP cumulative counters would collide into overlapping series
(reads look like counter resets) and per-console breakdown would be impossible.
The resource carries:

| Attribute | Value |
|---|---|
| `service.name` | `[telemetry] service_name`, or (blank) the install's branded app slug — the same source as the config directory and window title. |
| `service.instance.id` | `[telemetry] label`, or (blank) the machine hostname. |
| `host.name` | The console machine's hostname. |
| `process.owner` | The OS user running the console. |
| `openinference.project.name` | `[telemetry] project_name`, only when set. Phoenix and Arize AX route traces to this project. |

Each span also carries `session.id` = the chat it belongs to (the OpenInference
session attribute), so Phoenix and Arize AX group a conversation's turns into
one session.

So each machine is its own clean time series out of the box, and to run **several
consoles on one machine** as distinct streams you give each a different `label`.
Traces get the same identity, so a trace backend can filter by `service.name` /
`service.instance.id` / `host.name` too. (The values are best-effort — an
unreadable hostname or username is simply omitted.)

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

## Metrics

Alongside traces, the console can export **metrics** over OTLP. Metrics are a
**separate signal with their own endpoint and auth**, enabled independently of
tracing — because many trace backends don't ingest metrics. In particular
**LangSmith's OTLP endpoint takes traces, not metrics**, so the common setup is:

- **traces** → your trace backend (e.g. LangSmith), via `[telemetry]`;
- **metrics** → a metrics/OTLP backend (a Grafana / Prometheus OTLP endpoint, or
  a local OpenTelemetry Collector), via `[telemetry.metrics]`.

You record nothing by hand and there's no "emit after each run" plumbing: the
console records into in-memory instruments synchronously (cheap) and a
`PeriodicExportingMetricReader` pushes them to the backend on an interval.

**Metrics cover every runnable run — and work with the AI off-switch.** The
`runspec.tool.*` instruments fire both on the agent tool gate *and* on the
human/automation execution path (Forms, MCP server, schedules, rule triggers), so
they count the whole fleet's activity whether or not the agent is involved. That
means metrics are exported even with `[console] ai = false` (tracing is
agent-turn-only, so it stays off in that mode) — and Settings → Telemetry stays
visible with AI off, showing a **metrics-only** form.

### Instruments

| Metric | Type | Unit | Notes |
|---|---|---|---|
| `runspec.tool.runs` | counter | 1 | One per runnable run (agent tool call, Forms, MCP server, schedule, trigger). |
| `runspec.tool.duration` | histogram | ms | Runnable execution wall-clock. |
| `runspec.agent.turns` | counter | 1 | One per completed chat / trigger / schedule turn. |
| `runspec.agent.turn.duration` | histogram | ms | Turn wall-clock. |
| `runspec.agent.turn.tokens` | counter | 1 | Model tokens, split by `gen_ai.token.type` (input/output). |

Attributes are a **bounded** set so they don't blow up backend cardinality:
`runspec.tool.name`, `runspec.tool.status` (ok/error/cancelled),
`runspec.tool.autonomy_decision`, `runspec.tool.operator_decision`,
`runspec.turn.kind`, `runspec.turn.status`, `runspec.provenance.*`, and
`runspec.host` (only for `run_runnable`, whose host is a bounded fleet host —
other tools may carry an ad-hoc host, which is deliberately left off metrics and
kept on spans). These mirror the dimensions the Analytics tab already buckets on.

### Enable it

**Settings → Telemetry → Metrics**, or edit `config.toml`:

```toml
[telemetry.metrics]
enabled = true
endpoint = "http://127.0.0.1:4318/v1/metrics"   # your collector / Grafana / Prometheus-OTLP
export_interval = 60                             # seconds between exports

# Metrics auth is metrics-specific (NOT inherited from [telemetry]) so the trace
# backend's key is never sent here. e.g. Grafana Cloud basic auth:
[telemetry.metrics.headers]
"Authorization" = "Basic ..."
```

| Key | Meaning |
|---|---|
| `enabled` | Master switch for metrics. Default **false**, independent of trace `enabled`. |
| `endpoint` | OTLP **metrics** URL (`…/v1/metrics`). A base URL — Grafana Cloud's `https://otlp-gateway-<region>.grafana.net/otlp`, or a collector's `http://host:4318` — gets `/v1/metrics` added; any other path is used as given. Blank ⇒ the SDK reads `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`. **Send test metric** reports the backend's answer, so a wrong path shows as `code: 404`. |
| `export_interval` | Seconds between metric exports. Default 60 (min 1). |
| `headers` | Extra HTTP headers on the metrics export. **Not** shared with the trace headers. |
| `api_key_command` / `api_key_header` | Resolve the metrics key at runtime from a command (same "ship the command, not the key" pattern as traces); metrics-specific. |
| `ca_bundle` / `tls_verify` | **Inherited from `[telemetry]`** when unset here (transport trust is environment-wide, not a per-backend secret); set to override. |

The metrics `endpoint` and auth are deliberately **not** inherited from the trace
`[telemetry]` values — pointing metrics at a different backend must never ship the
trace backend's API key to it. (The OTLP/HTTP metrics path is also a different URL
— `/v1/metrics` vs `/v1/traces` — so the endpoints genuinely differ.)

### Verify it — send a test metric

**Settings → Telemetry → Metrics → Send test metric** saves the current settings,
records one sample tool-run + turn metric, and **force-flushes** it so it lands
immediately instead of waiting for `export_interval`. The result panel reports the
instruments and the endpoint. It's the metrics twin of **Send test trace**.

## Verify it works — send a test trace

You don't have to run a real agent turn to check the pipeline. In **Settings →
Telemetry**, click **Send test trace**. It saves the current settings, then emits
one representative `turn → LLM call → tool call` trace — the exact `gen_ai.*` /
`runspec.*` span shape a live turn produces — and **flushes it immediately**, so it
should appear in your backend within a second or two rather than waiting for the
batch interval.

- With **content capture off**, the test trace carries structure only (models,
  token counts, tool names, decisions) — enough to confirm connectivity and the
  span tree renders.
- With **content capture on**, it also attaches sample prompt / completion /
  tool-argument text — including a secret-looking argument, so you can see the
  [scrubbing](#privacy) in action.

The result panel reports the trace id, the endpoint it went to, and whether content
was captured. If it fails, the message says why (tracing not enabled, the `otel`
extra missing, or the endpoint didn't initialise) — fix that and try again.

## Choosing a backend

Because the console emits **standard OTLP**, switching backends is only an
`endpoint` change — no code, no lock-in. You can point it at several in turn and
compare. The main axes are *hosted vs self-hosted* and *LLM-aware vs general
tracing*:

| Backend | Hosting | LLM-aware | Best for |
|---|---|---|---|
| **LangSmith** | SaaS (enterprise self-host) | Yes — conversation view, token/cost, evals | Deep LLM debugging & evaluation |
| **Langfuse**, **Arize Phoenix** | Self-hosted, OSS | Yes — render prompts/completions, token/cost | A self-hosted, LangSmith-*like* experience |
| **Arize AX** | SaaS (free tier) | Yes — sessions, prompts/completions, evals | Hosted LLM tracing and evaluation; see [Arize AX](#arize-ax) |
| **Grafana Tempo**, **Jaeger** | Self-hosted, OSS | No — general trace UI | Operational / audit tracing; already running Grafana |

All of them ingest OTLP, so any of the above is just a different `endpoint`.
A general trace UI (Tempo / Jaeger) shows the full `turn → llm → tool` tree with
attributes, timings, decisions, and provenance — it just won't render a pretty
conversation or token/cost rollups the way an LLM-native tool does.

### Arize AX

[Arize AX](https://arize.com/docs/ax) takes plain OTLP over HTTP, so it is
configured with the same generic fields as any other backend — no Arize SDK.
Settings → Telemetry → **Use Arize AX** fills in everything except the
endpoint, which depends on your space's region:

```toml
[telemetry]
enabled = true
endpoint = "https://otlp.eu-west-1a.arize.com/v1/traces"  # your region; US: https://otlp.arize.com/v1/traces
project_name = "runspec-console"
capture_content = true
credential = "arize-api-key"   # a `token` Credential holding the API key
credential_header = "api_key"  # sent raw, no "Bearer"

[telemetry.headers]
space_id = "U3BhY2U6…"         # your Space ID (Space settings)
```

Arize reads the API key from the `api_key` header and the Space ID from
`space_id`. Without a project name, Arize files traces under its default
project. A wrong key, space ID or region makes the export fail, and **Send test
trace** reports that as a failure with the backend's answer (for example
`code: 401`), rather than "sent".

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

### A note on Prometheus / metrics backends

[Prometheus](https://prometheus.io/) is a **metrics** store — it does not ingest
traces, so it's not a *traces* backend. For **metrics**, the console now exports
them natively over OTLP (see [Metrics](#metrics)) — point `[telemetry.metrics]
endpoint` at a metrics/OTLP receiver:

- **Grafana Cloud / Grafana + Prometheus:** send OTLP metrics to Grafana Cloud's
  OTLP endpoint (with its basic-auth header under `[telemetry.metrics.headers]`),
  or to a local **OpenTelemetry Collector** with a `prometheus` /
  `prometheusremotewrite` exporter that forwards to your Prometheus.
- **Deriving metrics from traces** (the old approach) still works and is
  complementary: a Collector's `spanmetrics` connector (or Tempo's
  metrics-generator) can *also* derive request/error/duration metrics from the
  trace stream. Use whichever fits — the native metrics above are the direct path.

## Providing the API key without shipping it

A backend like LangSmith or Grafana needs an API key, and there's no good answer
that puts that key in `config.toml` in the clear — a config seed ships in package
data and a Config-Sync repo is checked out on every machine, so a key placed
directly in `[telemetry.headers]` would leak. The patterns below solve this.

### Credentials (recommended)

Because the console is a **single-user machine app**, the simplest answer is a
per-user key: during onboarding each operator logs in to LangSmith / Grafana,
mints their own API key, and adds it as a **Credential** (Settings → Credentials)
whose secret lives in the **OS keychain**. Then point telemetry at it — the id is
all that's stored in config:

```toml
[telemetry]
enabled = true
endpoint = "https://api.smith.langchain.com/otel/v1/traces"
credential = "langsmith"          # a Credentials id; secret stays in the keychain
credential_header = "x-api-key"   # LangSmith wants the key under x-api-key

[telemetry.metrics]
enabled = true
endpoint = "https://otlp-gateway.grafana.net/otlp/v1/metrics"
credential = "grafana-otlp"       # a userpass credential ⇒ Authorization: Basic
```

The mapping mirrors the MCP client: a `userpass` credential becomes
`Authorization: Basic base64(user:pass)` (Grafana Cloud's instance-id + token), a
`token` credential becomes `Bearer`, or — with `credential_header` set — the raw
secret is sent under that header (LangSmith's `x-api-key`). Traces and metrics each
take their **own** `credential`, so per-user keys for two different backends never
mix. An explicit `[telemetry.headers]` entry always wins, and `header_credentials`
= `[{ header, credential }, …]` sends several secrets under several headers. Pick
the credential from the dropdown in **Settings → Telemetry** (traces and metrics
sections). This is the right fit when every operator authenticates as themselves;
the two patterns below suit a shared/fleet key instead.

### Relay through an internal OTel Collector

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
  reasons, durations. No prompt text, no completion text, no tool arguments or
  results. You still get the full shape of every turn.
- **When you turn `capture_content` on**, prompt / completion / tool-argument
  and tool-result text is attached — **in full by default** (`max_content_chars = 0`), so a trace
  shows the real prompt rather than a truncated slice — and tool arguments are
  **scrubbed** first: values under secret-looking keys (`password`, `token`,
  `secret`, `api_key`, `authorization`, …) become `«redacted»`. Resolved
  credentials injected by the console (the secret env channel, `sudo`/`su`
  credentials) live *below* the tool-input layer the agent sees and never reach a
  span at all. Set `max_content_chars` to a positive value only if you want to
  bound very large spans.

  A tool span's input is the call the agent made: for a search-mode
  `run_runnable` call that is `{command, host, args}` (the runnable is the span
  name). Its output (`output.value` / `gen_ai.tool.call.result`) is the text the
  model was given as the tool result, which also appears in the next LLM span's
  prompt.

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
  Click **Send test trace** (Settings → Telemetry) to emit + flush one immediately
  and isolate whether the problem is connectivity or a quiet agent.
- **TLS errors to an internal backend.** Set `ca_bundle` to your CA `.pem`. A
  short session's spans flush on shutdown; a crash may drop the last batch.
- **A page behind SSH, not a backend.** Telemetry is only the tracing exporter;
  it doesn't change how the agent reaches hosts.
- **The captured prompt contains "What you can reach right now, by category:"**
  That's the [search-mode](console-agent.md) **catalog index** — a compact
  one-line-per-runnable menu the console composes into the **system prompt** each
  turn so the agent knows what exists without every runnable's full tool schema.
  (Before 0.262.0 it rode the first *user* message; it moved into the system prompt
  so a gateway that caches only the system block actually caches it. `gen_ai.prompt`
  captures the system prompt alongside the messages, so the index still shows up in
  the recorded prompt.) It's expected (and it's the *lean* path — the alternative is
  shipping every schema in the tool list), and it's the same each turn because it's
  deterministic for a given fleet. If the prompt looked *cut off* in your backend on
  an older build, that was the capture attribute's old 4000-char cap, not the real
  request — `max_content_chars = 0` (the default) now records the full prompt.
