# Settings & config reference

Everything the console persists lives in one folder under the **writable
per-user app-data root** — `%APPDATA%\<name>\` on Windows (macOS/Linux:
`~/AppData/Roaming/<name>/`). `<name>` is `runspec-console` by default; a
white-label package that ships a config seed brands the folder with **its own
distribution name** (e.g. `%APPDATA%\jason-console\`), the same source that names
the window title — so two different white-label installs stay isolated under one
writable root without any flag, while two copies of the same package share one
profile. (The app-data root is used deliberately rather than the venv, which can
be read-only for a non-admin operator.) `--config-dir <dir>` (or
`RUNSPEC_CONSOLE_DIR`) overrides the location entirely.

> **Run history is machine/venv-wide, not config-scoped.** The **History**,
> **Analytics** and **Logs** tabs read each runnable's audit logs from the venv's
> `logs/` dir and `~/logs` — written by the runnable itself, not by the console —
> so they show what actually ran on this host/venv regardless of the config
> folder. Point the console at a different venv for a separate run history.

The Settings UI manages these files; this page is the map for when you want
to read, seed, or sync them directly. All files are plain TOML.

## Config file map

| File | Holds | Synced? | Edited in |
|---|---|---|---|
| `config.toml` | All `[section]` settings below | Yes (deep-overlay) | Settings tabs |
| `runspec_hosts.toml` | `[[host]]` fleet entries | Yes (by `name`) | Settings → Jump Hosts |
| `runspec_groups.toml` | Group-name registry | Yes | Host editor → Manage groups |
| `runspec_profiles.toml` | Profile definitions | Yes (active pick is machine-local) | Sidebar / host editor |
| `runspec_triggers.toml` | `[triggers.<name>]` tables | Yes | Triggers tab |
| `runspec_rota.toml` | Working-hours rota | No — machine-local | Rota tab |
| `runspec_filters.toml` | [Transfer filters](console-hosts.md#file-transfer-filters) — source-side pipelines | Yes | Settings → Filters |
| `runspec_credentials.toml` | Credential **metadata** (secrets live in the OS keychain) | Yes | Settings → Credentials |
| `runspec_runbooks.toml` | `[[runbook]]` collection | Yes (by `id`) | Agent / Settings → Runbooks |
| `runspec_repos.toml` | Agent-accessible repo registry | Yes (checkout paths are machine-local) | Settings → Repos |
| `runspec_self_service.toml` | Room self-service catalogue | Yes | Settings → Self-Service |
| `runspec_memory.toml` | Agent memory | No — machine-local | Agent (`remember`) |
| `conversations/`, `workspaces/`, `repos/`, `improve/` | Chat history, session scratch, repo checkouts, [Improve](console-improve.md) reports | No | — |

"Synced" means the file participates in [Config Sync](console-team.md) and
package [config seeds](white-label.md); machine-local files never leave the
machine.

---

## `[llm]` — model & API {#llm}

Set in **Settings → Model / API**.

| Key | Meaning |
|---|---|
| `provider` | `anthropic`, `openai`, `bedrock`, `langserve`, or a [plugin adapter](console-adapters.md) |
| `api_key` | Static API key |
| `api_key_command` / `api_key_ttl_ms` | Instead of a static key: a command whose stdout is a short-lived token, re-run after the TTL |
| `base_url` | Corporate proxy / gateway endpoint |
| `model` / `models` | Active model id / the dropdown's list of available ids |
| `max_tokens` | Per-reply output ceiling (default 8192); replies that hit it auto-continue, up to `max_continuations`. Left unset, the console **raises** it to the model's real `max_output_tokens` when that is higher (see [`[llm.model_limits]`](#llm-model-limits)) — set it explicitly only to hold a lower ceiling |
| `max_continuations` | How many times a reply cut off at `max_tokens` is auto-continued (default **12**). A runaway backstop, not a per-task dial |
| `max_tool_rounds` | How many tool-execution rounds one turn may take (default **25**). Independent of `max_continuations`. Hitting it ends the turn with a visible, resumable *paused after N tool steps* marker — say "continue" to resume |
| `effort` | Reasoning effort before the model answers: `off` (default), `low`, `medium`, `high`, `xhigh`, `max` — see [below](#llm-effort) |
| `max_network_retries` | How many times a model call is retried after a **transient** failure — read/connect timeout, dropped connection, 429/5xx — before any text has streamed (default 4; 0 disables). Spaced by exponential backoff, so a proxy blip no longer ends a turn on a bare "Error" |
| `max_self_recoveries` | How many times a turn that still failed after its network retries is handed back to the agent to **analyse and resume** (default 1; 0 disables). Injects a short reflection turn with the full conversation still in context, then re-enters the loop after a backoff |
| `system` | System prompt — **replaces** the built-in default wholesale (including its clarify-don't-guess guidance). Settings → Model / API pre-fills the field with the built-in default; leaving it equal to the default keeps the install on future default updates (the key is not written) |
| `system_append` | Extra system-prompt text **added to** whichever base applies. Prefer this for site policy: you keep the built-in guidance instead of trading it away |
| `tool_mode` | `dedup` (default — one tool per runnable with a `host` enum), `per_host`, or `search` (meta-tools + catalogue index; `tool_index_limit` caps the index) |
| `tool_index` | Search mode only: fold the always-on one-line-per-runnable catalogue menu into the prompt (default **true**). `false` ⇒ the agent discovers purely via `search_runnables` |
| `tool_index_limit` | Search mode only: max runnables listed in the catalogue menu (default **300**; `0` = no limit). Beyond it the agent still finds runnables via `search_runnables`; the console logs the overflow and the context gauge shows *N beyond menu*. To *reduce* the surface instead, hide runnables via `[tool_visibility] hidden_from_agent` |
| `tool_output_cap` | Truncation cap for tool results returned to the model |

Bedrock adds the standard AWS credential chain (`aws_region`,
`aws_access_key`, `aws_secret_key`, `aws_session_token`) or a
`base_url`+`api_key` proxy path. LangServe gateways add mapping knobs
(`input_messages_key`, `input_tools_key`, `tools_in_config`, `auth_header`,
`auth_scheme`, `max_tokens_key`), plus `stream_events` (default **on** — stream
over `/stream_events`, falling back to `/invoke` if unavailable) and
`cache_markers` (default **off** — add Anthropic `cache_control` breakpoints for a
gateway that forwards them) — see [adapters](console-adapters.md).

### `effort` — how hard the model thinks {#llm-effort}

`[llm] effort` buys deliberation before the model answers (adaptive extended
thinking — see [adapters](console-adapters.md) → *`effort`*
for the wire format and gateway knobs). It trades latency and output tokens for a
better-reasoned answer, so the level worth paying for depends on the work.

**For ops work — day-to-day fleet operation — `low` or `medium` is the band that
pays, and `medium` is the sensible default when you're not sure.** `off` is fine
for one-shot lookups; above `medium` the extra thinking mostly buys latency on
tasks whose difficulty is in *reaching* the right host, not in reasoning about it.

| Level | Where it earns its keep |
|---|---|
| `off` | Single-step, unambiguous requests — "what's disk usage on web-01", "list the crontab". The answer is one tool call; thinking adds latency and nothing else. On Sonnet 5 / Opus 5 / Fable, *omitting* the parameter would run high-effort thinking by default, so the console makes `off` genuinely off where the model allows it (an explicit disable), falling back to `low` on Fable — see [adapters](console-adapters.md) → *`effort`* |
| `low` | **The everyday ops default.** Picking the right runnable from a large fleet catalogue, reading a `search_runnables` row and building the call (choosing the subcommand, mapping a request onto typed args), interpreting one tool's output |
| `medium` | **Multi-step and consequential work.** Chaining several runnables, triage that reads output and decides what to do next, writing or following a runbook, anything destructive or fanned out across a host group where getting the target wrong is expensive |
| `high` and above | Genuinely hard reasoning — diagnosing an unfamiliar failure from logs, designing an automation. Rarely the right setting for a standing console |

Two things to keep in mind when turning it up:

- Thinking counts toward `max_tokens`, since it shares the output budget. At
  `medium` and above, leave `max_tokens` unset (the console then raises it to the
  model's real ceiling) or set it generously — otherwise a deep reply spends its
  budget thinking and gets cut off, then auto-continues.
- It composes with tool use: thinking blocks survive tool-call turns, so the
  effort applies to every step of a multi-tool turn, not just the first.

Effort is dropped automatically on a model or gateway that doesn't accept it
(`adaptive_thinking = false` in `[llm.model_limits]`), and `off` is made genuinely
off per model (`thinking_default_on` / `thinking_disable`, below) rather than
silently leaving high-effort thinking on.

### `[llm.model_limits]` — model capabilities {#llm-model-limits}

A table of **model-id substring → capability fields**, so the console knows the
limits of a model it can't otherwise interrogate (e.g. one behind a LangServe/
Bedrock gateway, where the Anthropic Models API isn't reachable). Entries are
matched by substring and merged **broad → specific** (the longest matching key wins
per field; an empty `""` key is a catch-all). It rides the config seed / Config
Sync like the rest of `config.toml`.

```toml
[llm.model_limits."claude"]
context_window    = 200000
max_output_tokens = 8192

[llm.model_limits."claude-sonnet-4-6"]
max_output_tokens = 64000
adaptive_thinking = true
```

| Field | Meaning |
|---|---|
| `context_window` | Total prompt+output token budget — feeds the context-window gauge (overrides the model-id substring guess) |
| `max_output_tokens` | The model's real per-reply ceiling. `[llm] max_tokens` is **clamped down** to it so a generous setting can't 400 — and, when `max_tokens` is unset, **raised up** to it, so a model that allows 64000 isn't held at the conservative 8192 default (an explicit `max_tokens` is never raised) |
| `adaptive_thinking` | Whether the model/gateway accepts thinking / reasoning **effort**. `false` ⇒ the console sends none |
| `thinking_default_on` | Whether *omitting* the thinking parameter runs extended thinking anyway (at effort **high**) — true for Sonnet 5 / Opus 5 / Fable. When true, `effort = off` sends an explicit disable instead of nothing. Seeded per-family by the Anthropic/Bedrock built-ins; set it here only to correct a model they don't know |
| `thinking_disable` | Whether the model accepts `thinking = {"type": "disabled"}`. `true` for most Claude models; `false` for Fable, where `effort = off` falls back to `effort = low` instead. Only consulted when `thinking_default_on` is true |
| `cache_markers` | Whether prompt-cache breakpoints are honoured (informational) |
| `strict_tools` | Whether the endpoint enforces strict JSON-schema tool arguments (informational) |

The `anthropic` built-in fills any unset numeric limit from the Models API when
reachable, falling back to this table. See [adapters](console-adapters.md) →
*Model capabilities*.

## `[console]`

| Key | Meaning |
|---|---|
| `docs_url` | Overrides the in-app Documentation link |
| `ai` | **AI off-switch** (default `true`; see [Console without AI](console-ai-off.md)). `false` runs the console as a non-AI fleet tool: the AI tabs/controls are hidden, no chat/runbooks, agent-mode triggers and `prompt`/`runbook`/`eval` schedules are refused as invalid, no model adapter is ever built, external MCP servers / tracing / sandbox aren't started; Forms, History, saved runs, rule triggers, rooms and the console's **own** MCP server keep working. Read once at start (restart to apply); meant to ship from a config seed / Config Sync (deliberately not `preserve`d, so it re-asserts on every pull). See `docs/design/console-ai-off-switch.md` |
| `update_check_timeout` | Seconds for the footer update check (default 20) |
| `confirm_timeout` | Seconds an agent tool-confirmation gate waits for the operator in **chat** before treating no-answer as a deny (default 300; `0` = wait indefinitely) |
| `trigger_confirm_timeout` | Same, for unattended **trigger/schedule** agent turns (default 3600, longer because the operator may be away when it fires; `0` = wait indefinitely) |
| `output_line_cap` / `output_byte_cap` | Streaming caps for direct-run output (defaults 5000 lines / 2 MB; `0` disables) |
| `theme.*` | White-label branding — see the [walkthrough](white-label.md#theme) |
| `confirm_notifications` | Desktop toast + taskbar flash when an agent tool-confirmation gate is waiting (default true) |
| `room_notifications` | Toast + flash on a chat/helpdesk message while the console isn't focused (default true) |

## `[console.sounds]` — notification sounds {#sounds}

Audio cues so you stay aware while the console is minimized or in the background
(Windows only; a no-op elsewhere). Edit these in **Settings → Sounds &
Notifications**.

| Key | Meaning |
|---|---|
| `enabled` | Master toggle for all cues (default true) |
| `volume` | 0–100 for synthesized + file cues (default 100) |
| `pack` | A folder of category-named files (`confirm.wav`, `host_down.mp3`, …) applied to any category left on its default — machine-local, never synced |
| *`<category>`* | Per-event override — one key per category (`confirm`, `trigger`, `completion_success`, …) set to a **sound token** |

A **sound token** is `synth:<name>` (a bundled cue), `system:<Alias>` (a Windows
system sound), `file:<path>` (your own `.wav`/`.mp3`), `sound:<id>` (a **named
sound**, below), or `off`.

### Named sounds — add your own, reuse by name {#named-sounds}

Add your own `.wav`/`.mp3` in **Settings → Sounds → My sounds**, give it a name,
and it gets a stable id you can reuse anywhere the console plays a sound — as a
per-category pick, on a trigger or schedule action, and by the agent's
`play_sound` tool. Named sounds live in `runspec_sounds.toml`:

```toml
[[sound]]
id    = "deploy-done"          # stable identity (referenced as sound:deploy-done)
label = "Deploy done"          # shown in the UI, named by the agent
file  = "C:/…/AppData/…/runspec-console/sounds/deploy-done.mp3"
```

The **registry rides Config Sync** (merged by `id`), but the audio files stay
**machine-local** (the console copies an imported file into its own app-data
store, like a `pack` folder). So a synced id plays a cue only on a machine where
the file is present, and degrades gracefully to silence elsewhere — Settings
flags such an entry as `missing`.

**Where a named sound is used:**

- A trigger plays its `sound` when it fires (rule *and* agent mode) — set it in
  the Triggers editor's *Play a sound* field, or `sound = "deploy-done"` on the
  `[triggers.<name>]` table.
- A schedule plays its `sound` when it fires (any action) — the Schedules
  modal's *Play a sound* field, or `sound = "deploy-done"` on the `[[schedule]]`
  entry.
- The chat agent can play one with the confirm-free **`play_sound`** action tool
  (e.g. "play the alarm when the deploy finishes"); it's allowlistable per
  trigger via `action_allow`. All of these respect the master `enabled` toggle.

## `[ssh]` and `[refresh]`

Connection pooling, timeouts, proxies, and the background refresh cadence —
documented with the fleet setup in [Hosts & fleet](console-hosts.md#ssh-tuning).

## `[workspace]`

| Key | Default | Meaning |
|---|---|---|
| `spill_threshold` | 32768 | Tool results above this many bytes spill to workspace files |
| `inline_max` | 131072 | Ceiling for a call's `deliver = "inline"` request (hand the raw output back whole); above it the call falls back to `auto` (spill). Also the extract-field cap |
| `code_exec` | `true` | Master switch for the `run_python_sandboxed` / `run_sql_sandboxed` tools |
| `exec_timeout` | 30 | Seconds before a sandboxed run is killed (hard limit — the run is a killable subprocess) |
| `headless_retention_days` | 7 | How long trigger/schedule-run workspaces are kept |

## `[sandbox]`

Settings for the WASM/WASI code-execution sandbox behind `run_python_sandboxed` /
`run_sql_sandboxed` (see [Sandboxed code execution](console-sandbox.md)). Needs
the `[sandbox]` extra (`pip install runspec-console[sandbox]`).

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Kill switch (with `[workspace] code_exec`); either false drops the tools |
| `mem_mb` | 512 | Guest memory ceiling (MB) |
| `fuel` | *(unset)* | Optional wasmtime CPU-instruction cap; unset ⇒ the wall-clock timeout is the guard |
| `wasm_path` | *(unset)* | Explicit path to a WASI CPython `python.wasm` (air-gapped installs) |
| `auto_download` | `true` | Fetch the pinned binary on first use when none is found locally |
| `url` / `sha256` | *(pinned)* | Override the download source (private mirror) |

## `[web]`

Settings for the agent's read-only `fetch_url` web-page tool (see
[the agent guide](console-agent.md#read-a-web-page)).

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch for the `fetch_url` tool (set `false` to remove it) |
| `timeout` | 20 | Seconds before a single fetch is aborted |
| `max_bytes` | 2000000 | Cap on bytes downloaded per fetch |
| `ca_bundle` | — | Path to a private-CA bundle for internal HTTPS hosts |
| `tls_verify` | `true` | Set `false` to skip TLS verification (TLS-intercepting proxies) |

## `[voice]` — dictation into the prompt box {#voice}

Settings for the chat composer's **mic button** — on-device speech-to-text via
[Moonshine Voice](https://github.com/moonshine-ai/moonshine), installed with the
`[voice]` extra (`pip install runspec-console[voice]`).
Recognition runs host-side in the console process — no audio leaves the machine
and the web view never touches the microphone. Dictation only *fills* the prompt
box; Enter still sends, so voice sits below every autonomy/confirm gate. The
button is hidden until the extra is installed. Ctrl+M toggles it. Every key
below is editable from **Settings → Voice** (which also shows whether dictation
is available here, why not if so, and lists the microphones it can open).

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch — `false` hides the mic button |
| `language` | `"en"` | Moonshine language code (English is the MIT-licensed streaming model) |
| `model` | `medium-streaming` | Streaming model size: `tiny-streaming` / `base-streaming` / `small-streaming` / `medium-streaming` (a bare size such as `small` is accepted) |
| `model_dir` | — | Path to a **vendored** model directory. Set on a fleet that blocks outbound downloads — the engine then never fetches anything. Unset ⇒ the model is fetched from `download.moonshine.ai` on first use into Moonshine's cache (`MOONSHINE_VOICE_CACHE` overrides the location) |
| `device` | system default | Capture device index or name |
| `update_interval` | 0.5 | Seconds between refreshes of the in-progress line |
| `silence_timeout` | 8 | The mic turns itself off after this many seconds with no recognised speech; `0` keeps it open until you stop it or send |
| `keep_loaded` | `true` | Keep the speech engine resident between sessions so the second press opens the mic instantly (costs the model's RAM while idle); `false` reloads on every press and frees it after |

To vendor the model, run `moonshine-voice download --language en` once on a
machine with access, copy the resulting model directory alongside the package
(or ship it through the internal index), and point `model_dir` at it.

## `[mailbox]` and `[[watch]]` — email monitoring {#mailbox}

The Outlook `new_mail` trigger source. Set in **Settings → Mailbox**. See
[Monitoring a mailbox](console-automation.md#monitoring-a-mailbox-folders).

`[mailbox]` — the primary monitored mailbox (also the room / sign-up identity):

| Key | Default | Meaning |
|---|---|---|
| `address` | your default account | The account or shared mailbox to monitor (blank = your own default account) |
| `folders` | `["Inbox"]` | `/`-separated folder paths to watch under `address`; empty/absent = the Inbox only. The legacy scalar `folder = "…"` is still accepted. **Machine-local** on a Config Sync pull. |
| `label` | — | Optional display label |
| `once_per_thread` | `false` | Mailbox-wide default: fire on the first message in a thread, ignore later replies (a trigger can override) |

`[[watch]]` — additional watch-only mailboxes/folders (feed `new_mail` triggers
only; no room/sign-up role): same `address` / `folders` / `label` keys, no
`once_per_thread`.

Shared mailboxes watched by several consoles should enable a **claim** on the
matching trigger (below) so exactly one operator handles each email.

## `[triggers.<name>]` — mailbox claim keys {#trigger-claim}

Triggers live in `runspec_triggers.toml` (Triggers tab). The claim gate for a
shared-mailbox `outlook` / `new_mail` trigger adds two keys — see
[Shared mailboxes — claim](console-automation.md#shared-mailboxes-claim-one-operator-wins):

| Key | Default | Meaning |
|---|---|---|
| `claim` | `false` | Park each matched mail behind a **Claim** button; the winning operator's console claims it and runs the action |
| `claim_folder` | — | **Required when `claim = true`**: an **existing** folder a claimed email is moved into (the move is the claim). Also requires `match.folder` (the folder to scope to) |

## `[chat_history]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Auto-save conversations locally |
| `max_conversations` / `max_age_days` | 200 / 90 | Retention pruning |
| `compact_on_resume` | `false` | Use the model to summarise the middle of a long resumed chat (otherwise a mechanical note) |
| `resume_keep_first` / `resume_keep_recent` | 1 / 20 | Turns kept verbatim when resuming a long chat |

## `[improve]`

Settings for the [Improve](console-improve.md) evaluation & problem-report
feature. Lives in `config.toml`, so it rides Config Sync and the config seed —
a white-label deployment can bundle who its developers are.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch for the Improve tab + `run_improve_report` agent tool |
| `product_name` | — | Brand shown in the report and mail subject (falls back to the app title) |
| `developer_emails` | `[]` | Recipients the report is addressed to and the **Email developers** mailto pre-fills (a list, or a comma/space-separated string) |
| `window_days` | 30 | Default look-back window mined from conversations + history |
| `max_conversations` | 200 | Cap on conversations sampled per run |
| `max_incidents` | 40 | Cap on flagged incidents in a report |
| `llm_synthesis` | `true` | Offer the LLM recommendation pass (needs an `[llm]` provider) |

Reports themselves are machine-local and **never synced** (each is derived from
this install's own transcripts).

## `[plugins]`

`modules = ["mycorp_adapters"]` — modules imported at startup so they can
register custom LLM adapters; see [adapters](console-adapters.md).

---

## Launch flags

```
runspec-console                       # normal start
runspec-console --devtools            # Chromium inspector in the packaged build
runspec-console --dev                 # attach to a running Vite dev server (UI development)
runspec-console --config-dir ./cfg    # isolate all console state in a directory (testing)
```

`--config-dir <dir>` overrides the default location (`%APPDATA%\<name>\`) for
**all** console *config* state (hosts, credentials, triggers, `config.toml`, …)
so a test instance stays isolated from your real profile. Equivalent env var:
`RUNSPEC_CONSOLE_DIR` (the flag takes precedence when both are set). The
directory is created if it doesn't exist. Note it does **not** isolate the
History/Analytics/Logs tabs — those are venv-scoped run logs (see the note at
the top of this page).

## Troubleshooting

- **"Error reading SSH protocol banner" / connection bursts** — see
  [SSH tuning](console-hosts.md#ssh-tuning); the pool and backoff settings
  are the levers.
- **"the remote sent non-SSH data on connect"** — a proxy/middlebox is
  intercepting the SSH port; set `[ssh] proxy` or `use_ssh_config = true`.
- **Footer stuck on "update unknown"** — the package-index query timed out;
  raise `[console] update_check_timeout` (slow internal mirrors).
- **A runnable doesn't appear** — it must be `pip`-installed into the
  console's venv (local) or a venv listed in the host's `runspec_paths`
  (remote); check with `runspec local` in that venv. `discoverable = false`
  runnables are hidden by design.
