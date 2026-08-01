# Local Phoenix — private, on-machine trace analysis

The console already emits an OpenTelemetry [trace per agent
turn](console-telemetry.md) (`turn → LLM call → tool call`). The **Phoenix tab**
lets you view and analyse those traces **entirely locally**: it launches a
private [Arize Phoenix](https://phoenix.arize.com/) server on your machine — an
OTLP collector *and* an LLM-aware trace-analysis web UI in one process — routes
the console's own traces to it, and embeds Phoenix's UI right in the tab.

Nothing leaves the machine: no external backend, no API keys, no config. Install
the extra, click **Start**, and your agent's activity shows up in a trace
explorer built for LLM apps.

This is the zero-config local counterpart to the general
[telemetry](console-telemetry.md) feature (which exports to a remote backend of
your choice). You can use either or both.

## Install

The Phoenix tab needs the optional `phoenix` extra:

```bash
pip install "runspec-console[phoenix]"
```

Phoenix is a **heavy dependency** (it pulls in pandas, SQLAlchemy, uvicorn, and
more), so it is isolated behind this opt-in extra — a base install is
unaffected. Until it's installed, the Phoenix tab shows an install hint.

## Use it

1. Open the **Phoenix** tab.
2. Click **Start**. The console launches a local Phoenix server (default port
   **6006**) and, once it's up, points its own trace exporter at it.
3. Run an agent turn (chat, a trigger, a schedule). Within a few seconds the
   span tree appears in the embedded Phoenix UI.
4. Click **Stop** when you're done. The console shuts the server down and
   restores whatever trace route your saved `[telemetry]` config had (if any).

The embedded UI is the real Phoenix app. If your environment blocks embedding a
local server in a frame, use **Open in browser** — the tab detects the failure
and offers it automatically.

### What gets traced

Exactly what the [telemetry](console-telemetry.md) feature traces — one
connected trace per agent turn:

```
runspec.agent.turn                     ← the whole turn
└─ chat <model>                        ← each LLM request
   └─ execute_tool <tool>              ← each tool call, with its autonomy decision
```

Content (prompts / completions / tool-argument text) is **not** attached unless
you set `capture_content = true` in `[telemetry]` — the same privacy gate as the
export path. Structure (models, token counts, tool names, decisions, durations)
is always present.

## Configuration — `[phoenix]`

All optional; sensible defaults mean you normally don't need this section.

```toml
[phoenix]
port = 6006                 # where Phoenix serves its collector + UI
auto_start = false          # launch Phoenix automatically when the console starts
working_dir = ""            # persistent trace store (defaults to {app_dir}/phoenix)
assistant = false           # Phoenix's built-in agent assistant (off by default)

# Escape hatch: any extra PHOENIX_* variables to pass to the server at launch.
[phoenix.env]
PHOENIX_ENABLE_MCP_SERVER = "false"
```

| Key | Default | Meaning |
|---|---|---|
| `port` | `6006` | The port Phoenix serves on (collector **and** UI). Change it if 6006 is taken. |
| `auto_start` | `false` | Start Phoenix at app launch instead of on demand from the tab. |
| `working_dir` | `{app_dir}/phoenix` | Where Phoenix persists its SQLite trace store, so traces survive restarts. |
| `assistant` | `false` | Enable Phoenix's built-in agent assistant (its `/chat` endpoint). **Off by default** — see [Privacy](#privacy-the-assistant-is-off-by-default) below. |
| `[phoenix.env]` | `{}` | A sub-table of extra `PHOENIX_*` environment variables passed to the server at launch — the escape hatch for any Phoenix setting without a typed knob. Non-`PHOENIX_*` keys are ignored. Applied last, so it overrides the knobs above. |

### Privacy: the assistant is off by default

Phoenix ships a built-in **agent assistant** (a chat-over-your-traces agent on its
`/chat` endpoint) that can call an **external LLM**, do web search/fetch, and run
a server-side bash tool. Because this feature is about keeping everything **on
your machine**, the console launches Phoenix with that assistant **disabled**
(`PHOENIX_DISABLE_AGENT_ASSISTANT=true`) unless you opt back in with
`assistant = true`.

If you do enable it and want to keep its reach tight, use `[phoenix.env]` to set
Phoenix's own guards, e.g.:

```toml
[phoenix.env]
PHOENIX_AGENTS_DISABLE_WEB_ACCESS = "true"   # no web search / fetch
PHOENIX_AGENTS_DISABLE_BASH = "true"         # no server-side bash tool
```

All of `[phoenix]` (including `assistant` and `[phoenix.env]`) rides Config Sync
and the config seed like the rest of `config.toml`, so a white-label / corporate
deployment can pin its own stance.

### How the auto-wire works (and what it won't touch)

Starting local Phoenix **live-routes** the console's OTLP exporter to
`http://127.0.0.1:<port>/v1/traces` for the duration of the session. This route
is **not written to `config.toml`** — it's an in-memory overlay on top of your
saved `[telemetry]` config (service name, sampling, and `capture_content` carry
over; the endpoint and enabled-flag are overridden). When you stop Phoenix, the
console re-reads your saved `[telemetry]` and restores whatever route it
described. So if you normally export to a remote backend (LangSmith, Tempo, …),
a local Phoenix session never clobbers that configuration.

If a Phoenix server is **already running** on the port (for example one you
started by hand), the console **adopts** it rather than launching a second one —
and, since it doesn't own that process, **Stop** leaves it running.

## Troubleshooting

- **"Phoenix isn't installed"** — run `pip install "runspec-console[phoenix]"` in
  the console's virtualenv and reopen the tab.
- **The embedded UI is blank / "Couldn't embed the Phoenix UI"** — your webview
  is blocking the frame. Use **Open in browser**; everything else works the same.
- **Port already in use** — set `[phoenix] port` to a free port, or stop the
  other server. If another Phoenix is serving on the configured port the console
  will adopt it.
- **No traces appear** — confirm the tab shows **running**, then run a fresh
  agent turn. Traces are batched, so allow a few seconds. Very short sessions
  flush on stop.
