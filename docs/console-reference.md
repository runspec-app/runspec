# Settings & config reference

Everything the console persists lives in one folder:
`%APPDATA%\runspec-console\` (macOS/Linux: `~/AppData/Roaming/runspec-console/`).
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
| `conversations/`, `workspaces/`, `repos/` | Chat history, session scratch, repo checkouts | No | — |

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
| `max_tokens` | Per-reply output ceiling (default 8192); replies that hit it auto-continue, up to `max_continuations` (default 3) |
| `max_network_retries` | How many times a model call is retried after a **transient** failure — read/connect timeout, dropped connection, 429/5xx — before any text has streamed (default 4; 0 disables). Spaced by exponential backoff, so a proxy blip no longer ends a turn on a bare "Error" |
| `max_self_recoveries` | How many times a turn that still failed after its network retries is handed back to the agent to **analyse and resume** (default 1; 0 disables). Injects a short reflection turn with the full conversation still in context, then re-enters the loop after a backoff |
| `system` | System prompt — **replaces** the built-in default wholesale (including its clarify-don't-guess guidance) |
| `system_append` | Extra system-prompt text **added to** whichever base applies. Prefer this for site policy: you keep the built-in guidance instead of trading it away |
| `tool_mode` | `dedup` (default — one tool per runnable with a `host` enum), `per_host`, or `search` (meta-tools + catalogue index; `tool_index_limit` caps the index) |
| `tool_output_cap` | Truncation cap for tool results returned to the model |

Bedrock adds the standard AWS credential chain (`aws_region`,
`aws_access_key`, `aws_secret_key`, `aws_session_token`) or a
`base_url`+`api_key` proxy path. LangServe gateways add mapping knobs
(`input_messages_key`, `input_tools_key`, `tools_in_config`, `auth_header`,
`auth_scheme`, `max_tokens_key`) — see [adapters](console-adapters.md).

## `[console]`

| Key | Meaning |
|---|---|
| `docs_url` | Overrides the in-app Documentation link |
| `update_check_timeout` | Seconds for the footer update check (default 20) |
| `confirm_timeout` | Seconds an agent tool-confirmation gate waits for the operator in **chat** before treating no-answer as a deny (default 300; `0` = wait indefinitely) |
| `trigger_confirm_timeout` | Same, for unattended **trigger/schedule** agent turns (default 3600, longer because the operator may be away when it fires; `0` = wait indefinitely) |
| `output_line_cap` / `output_byte_cap` | Streaming caps for direct-run output (defaults 5000 lines / 2 MB; `0` disables) |
| `theme.*` | White-label branding — see the [walkthrough](white-label.md#theme) |

## `[ssh]` and `[refresh]`

Connection pooling, timeouts, proxies, and the background refresh cadence —
documented with the fleet setup in [Hosts & fleet](console-hosts.md#ssh-tuning).

## `[workspace]`

| Key | Default | Meaning |
|---|---|---|
| `spill_threshold` | 32768 | Tool results above this many bytes spill to workspace files |
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

## `[chat_history]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Auto-save conversations locally |
| `max_conversations` / `max_age_days` | 200 / 90 | Retention pruning |
| `compact_on_resume` | `false` | Use the model to summarise the middle of a long resumed chat (otherwise a mechanical note) |
| `resume_keep_first` / `resume_keep_recent` | 1 / 20 | Turns kept verbatim when resuming a long chat |

## `[plugins]`

`modules = ["mycorp_adapters"]` — modules imported at startup so they can
register custom LLM adapters; see [adapters](console-adapters.md).

---

## Launch flags

```
runspec-console              # normal start
runspec-console --devtools   # Chromium inspector in the packaged build
runspec-console --dev        # attach to a running Vite dev server (UI development)
```

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
