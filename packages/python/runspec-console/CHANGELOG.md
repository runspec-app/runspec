# Changelog — runspec-console

All notable changes to this package are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers follow [Semantic Versioning](https://semver.org/).

---


## [0.227.1] — 2026-08-02

### Fixed
- **Corrected the "Arize Phoenix" link.** The Local Phoenix control (and the
  docs) linked to `https://phoenix.arize.com/`, which doesn't resolve; it now
  points at the canonical GitHub repo `https://github.com/Arize-ai/phoenix`.


## [0.227.0] — 2026-08-02

### Added
- **Multiple local Python venvs (auto-discovered).** The console previously
  discovered runnables only in the single venv it is installed into. It now
  discovers **additional local venvs** too — the same multi-venv support remote
  hosts already have (`runspec_paths` list), for the local machine. Rather than
  per-machine absolute paths (which can't be controlled fleet-wide the way synced
  remote paths can), a new `[local] venv_globs` *convention* in `config.toml`
  names glob patterns (e.g. `C:\venvs\*`) that each console scans at discovery
  time; every matched directory holding a runspec binary
  (`Scripts\runspec.exe` / `bin/runspec`) is surfaced under the synthetic `local`
  host alongside the console's own venv. Because the globs live in `config.toml`
  they ride **Config Sync** and the config seed — one versioned glob works across
  a fleet of identical machines, each resolving its own real venvs. Everything
  downstream was already list-driven (per-venv discovery, group-matched
  execution, per-venv sidebar counts), so extra venvs light up with no other
  change. Edit the globs (with a live preview of what they resolve to on this
  machine) in **Settings → Local venvs**. Default (no config) is byte-identical
  to before: only the console's own venv. New `config.read_local`,
  `hosts.resolve_local_venvs`, bridge `get_local_venv_config` /
  `save_local_venv_config`; the wiring is a single change in
  `bridge_internals._local_host_entry`. See `docs/console-hosts.md`.


## [0.226.0] — 2026-08-02

### Fixed
- **Local Phoenix now launches via the `phoenix` console script, not
  `python -m phoenix.server.main`.** On some hosts (notably Windows) Start failed
  with `server did not come up on port 6006 within 30s` even though
  `phoenix serve` worked by hand. Cause: `python -m phoenix.server.main serve`
  isn't equivalent to the `phoenix` entry point (`phoenix.server.main:main`) — it
  depends on a module `__main__` guard and interacts badly with multiprocessing
  `spawn`, so the process starts but never binds the port. `phoenix.py` now
  prefers the `phoenix`/`phoenix.exe` console script installed next to the
  interpreter (the exact thing `phoenix serve` runs), falling back to `-m` only if
  it isn't found (`_phoenix_argv`).

### Added
- **Phoenix server output is captured to `phoenix-server.log`** in the
  `working_dir` (was `DEVNULL`), so a failed or slow start is diagnosable instead
  of silent — the "did not come up" warning now points at the log.
- **`[phoenix] startup_timeout`** (seconds, default **60**, clamped 5–600, up from
  a hardcoded 30) — raise it on a slow host where first-run migrations take
  longer.


## [0.225.0] — 2026-08-02

### Fixed
- **No more `RuntimeError: cannot schedule new futures after interpreter
  shutdown` traceback on exit.** The fleet-refresh watcher (`bridge_internals`)
  is a daemon thread that periodically submits host-discovery work to a
  `ThreadPoolExecutor`; on app close it could wake and submit *after* the
  interpreter had begun shutting down, dying with that traceback. The loop now
  runs each cycle through `_refresh_tick`, which swallows exactly that
  shutdown-time error and stops the watcher quietly (any other error still
  propagates). Cosmetic — the app already exited cleanly — but it removes the
  scary-looking teardown traceback.
- **Launching local Phoenix no longer drags Phoenix into the console process.**
  `phoenix.available()` now probes with `importlib.util.find_spec("phoenix")`
  instead of `import phoenix`, so checking "is it installed?" (e.g. on
  `[phoenix] auto_start`) no longer *executes* arize-phoenix's heavy module-load
  side effects — ensuring its working dir, registering alembic plugins + sandbox
  adapters — in the console's own process and log. Phoenix still runs only as a
  subprocess; the console process stays clean and starts faster. Together with
  the refresh-watcher fix, this also narrows the shutdown-race window that made
  the traceback above reproducible when closing right after `auto_start`.


## [0.224.0] — 2026-08-02

### Changed
- **The code-execution tools now run in a real WASM sandbox, and are renamed to
  say so: `run_python` → `run_python_sandboxed`, `run_sql` → `run_sql_sandboxed`.**
  Previously `run_python` executed arbitrary code as the operator with full
  privileges — the confirm gate was the *only* security control, and the tool
  was honestly *not* sandboxed. It now runs untrusted Python inside a WASI
  CPython interpreter under `wasmtime` with **no ambient authority**: only the
  current chat session's workspace is visible (preopened as `/ws`, so relative
  `open(...)` still works — the intended "reshape spilled tool output" use is
  preserved and now *enforced*), **no network**, **no host filesystem beyond the
  workspace**, **no subprocess/host access**, a memory cap, and a **hard
  wall-clock timeout + working Cancel** (the run happens in a killable worker
  subprocess, so even a guest blocked in `time.sleep` is stopped at the
  deadline). The Python stdlib — including `sqlite3`, so `run_sql_sandboxed` runs
  in the sandbox too — is available; the console's own installed packages are
  not (the sandbox is isolated). `run_python_sandboxed` stays confirm-gated as
  operator governance; read-only `run_sql_sandboxed` stays autonomous. This is a
  real boundary an evaluator can inspect, not a relabel.
- **`run_python_sandboxed`'s `code` argument now accepts a list of lines** (e.g.
  `["import os", "print(os.getcwd())"]`) in addition to a plain string, so the
  agent can pass a multi-line script without JSON-escaping every newline.
- **The confirm card now shows multi-line code readably.** The read-only "what's
  going to run" preview (shown when a sandboxed call arrives via search-mode
  `use_tool`) renders string / line-array values with their **real newlines**
  instead of a JSON blob full of `\n`, and auto-grows to a taller cap before
  scrolling; the editable arg textarea grows to more rows too.

### Added
- **`[sandbox]` optional extra** (`pip install runspec-console[sandbox]`, pulls
  `wasmtime`) and a `[sandbox]` config section (`enabled`, `mem_mb`, `fuel`,
  `wasm_path`, `auto_download`, `url`, `sha256`). The ~20 MB WASI CPython binary
  is fetched on first use into app-data (SHA-256 verified) or supplied via
  `[sandbox] wasm_path` / `RUNSPEC_SANDBOX_WASM` for air-gapped installs; the
  compiled module is cached (serialized) so warm calls start in ~0.1 s.
- **Fail-closed availability.** When the sandbox runtime/binary is unavailable,
  the tools are dropped from the agent's toolset entirely (mirrors `_outlook_tools`)
  — there is **never** a fallback to un-sandboxed host execution. The existing
  `[workspace] code_exec = false` kill switch still drops them on top. See
  `docs/console-sandbox.md`.

## [0.223.0] — 2026-08-01

### Changed
- **Local Phoenix moved from a top-level tab to a Settings → Telemetry control;
  the embedded iframe is gone.** Phoenix turned out to be a full LLM-ops product
  (prompt playground, AI-provider config, its own agent assistant, an MCP
  server), and embedding all of it in a console tab made runspec-console feel
  busy and off-identity. The console now keeps only the **managed local server +
  auto-wire** (the on-mission part — observing the console's *own* agent), as a
  compact **Local Phoenix** control (Start / Stop / status / **Open in browser**)
  at the top of Settings → Telemetry. Phoenix's own UI opens in the browser
  rather than inside the console window. All backend behaviour is unchanged
  (`phoenix.py` managed server, `phoenix_status`/`_start`/`_stop`/`_open`, the
  live non-persisted trace re-route, `[phoenix]` config, `auto_start`); this is a
  frontend reshape (the `PhoenixView` tab + embed → `PhoenixLocalControl`). If
  you'd rather run Phoenix entirely separately, you don't need the `phoenix`
  extra at all — point Settings → Telemetry's endpoint at a self-run Phoenix.
  See `docs/console-phoenix.md`.


## [0.222.0] — 2026-08-01

### Added
- **Connect to external MCP servers (`[mcp]` extra).** The console is normally an
  MCP *server* (`runspec serve` publishes runnables as leaf tools); it can now
  also be an MCP *client*. Declare outside MCP servers under `[mcp]` /
  `[[mcp.servers]]` in `config.toml` and their tools join the chat agent's
  toolset alongside the runnables and built-ins, named `mcp_<server>_<tool>` and
  routed through the same autonomy/confirm gate. Three transports: local `stdio`
  (spawn a subprocess), remote `http` (streamable-HTTP), and `sse`. Sessions are
  **long-lived** — each server holds an open `ClientSession` on a dedicated
  background event-loop thread owned by the Bridge, and tool calls are marshalled
  onto that loop (the per-turn `asyncio.run` loop dies between turns, so a session
  can't live there). **Safe by default:** an external tool is confirm-gated
  unless the operator sets `autonomy = "autonomous"` on that server (or `manual`
  to make it human-only); in a scoped trigger/schedule turn the tools are
  governed by `action_allow` like the other action tools. Opt-in and
  best-effort — a missing extra or a down server never blocks startup or a turn
  (surfaced in `mcp_status()` / the agent's console context). New `mcp_client.py`
  (`McpManager` + pure helpers), `config.read_mcp()`, bridge `mcp_start` /
  `mcp_status` / `mcp_reload`, and the `mcp` extra (also in `[all]`). See
  `docs/console-mcp.md`.
  - **Named-credential binding.** A server's `credential` (a keychain-backed
    Credentials id) is resolved at connect time — `Authorization: Bearer`
    (`Basic` for a `userpass` credential, or the raw secret under a
    `credential_header` such as `X-API-Key`) for http/sse, and the credential's
    name-derived env vars for a stdio subprocess. Only the id is stored, so a
    bound server is seed/sync-safe (metadata rides `runspec_credentials.toml`,
    the secret stays in the OS keychain). `mcp_credential_options()` returns the
    id/label/kind list a future Settings-tab dropdown will render.
  - **Per-server HTTP proxy.** Each server takes a `proxy` (HTTP CONNECT URL,
    mirroring `[ssh] proxy`) so servers on different corporate networks each get
    their own egress — applied to http/sse, and exported as `*_PROXY` to a stdio
    subprocess.
  - **Seed & sync.** `[mcp]` (in `config.toml`) and bound-credential metadata
    (in `runspec_credentials.toml`) are already part of the Config Sync / config
    seed known-set, so MCP servers distribute from one versioned source.
  - **Settings → MCP tab.** A UI to add/edit/remove servers (transport picker,
    command/args/env or url/headers, a credential dropdown, credential-header,
    proxy, autonomy, enable) with a live per-server connection status (connected
    + tool count / error / disabled), a Reconnect button, a section toggle, and
    the connect/call timeouts.
    Backed by `get_mcp_config()` / `save_mcp_config()` (validates, writes
    `config.toml`, reconnects) alongside `mcp_status` / `mcp_credential_options`.


## [0.221.0] — 2026-08-01

### Added
- **Local Phoenix observability tab (`[phoenix]` extra).** A new **Phoenix** tab
  launches a private, on-machine [Arize Phoenix](https://github.com/Arize-ai/phoenix)
  server — an OTLP collector *and* an LLM-aware trace-analysis web UI in one
  process — and **routes this console's own agent traces to it automatically**, so
  every turn (`turn → LLM call → tool call`) shows up in a fully local trace
  explorer with no external backend, no API keys, and nothing leaving the machine.
  Phoenix's own UI is embedded in the tab via an iframe, with an **Open in
  browser** fallback when framing is blocked. Startup is gated on the server
  actually answering HTTP (not just an open TCP port) and the iframe shows a
  "Loading Phoenix…" overlay until it renders, so there's no connection-refused
  flash while Phoenix boots. Opt-in and safe-by-default: install
  `pip install runspec-console[phoenix]` to enable it (the tab shows an install
  hint until then); the heavy dependency is isolated behind the extra.
  - New `phoenix.py` `PhoenixServer` manages the server as a subprocess (start /
    stop / status, double-start guard, adoption of an already-serving port, crash
    detection, `atexit` teardown) — modelled on the `sources/schedule.py`
    `available()/start()/stop()` idiom.
  - The auto-wire is a **live, non-persisted** trace route: starting Phoenix
    overlays `enabled` + `endpoint = http://127.0.0.1:<port>/v1/traces` onto the
    saved `[telemetry]` config and re-inits the exporter via the new
    `observability.reconfigure()` seam (the existing `init_tracing` is idempotent);
    stopping restores the saved route — a user's real remote `[telemetry]` backend
    is never clobbered.
  - New bridge methods `phoenix_status` / `phoenix_start` / `phoenix_stop` /
    `phoenix_open` (`ObservabilityMixin`), `config.read_phoenix()` (`[phoenix]`:
    `port` default 6006, `auto_start` default false, `working_dir` for the
    persistent SQLite trace store), and an `auto_start` bring-up hook in `app.py`.
  - **Phoenix's built-in agent assistant is off by default.** Phoenix ships a
    chat-over-your-traces agent (its `/chat` endpoint) that can call an external
    LLM + do web/bash; the console launches Phoenix with
    `PHOENIX_DISABLE_AGENT_ASSISTANT=true` to keep the feature fully local. Opt
    back in with `[phoenix] assistant = true`, and pin any other Phoenix setting
    via a `[phoenix.env]` passthrough of `PHOENIX_*` variables — both ride Config
    Sync / the config seed.
  - See `docs/console-phoenix.md`.


## [0.220.1] — 2026-07-31

### Fixed
- **A search-mode `run_runnable` call whose `args` arrived as a string no longer
  crashes the turn.** The model occasionally emits the `args` object as a JSON
  *string* (`'{"host": "web1"}'`) or a bare string; the gate did `dict(<that
  string>)`, which raised `ValueError: dictionary update sequence element #0 has
  length 1; 2 is required`. On a trigger turn that surfaced as a cryptic card
  failure with the tool spinner stuck spinning (the exception escaped the tool
  loop before a `tool_end` was emitted). New lenient `Bridge._coerce_run_args`
  (parses a JSON-string object, else falls back to `{}` so the normal preflight
  reports a readable "missing required argument") is used everywhere the
  search-mode `args` field is read — the autonomy decision, the confirm-card
  view, the group fan-out, and execution.
- **A tool that raises can no longer strand the run.** Both the chat loop
  (`bridge_invoke`) and the headless trigger/schedule loop (`bridge_triggers`)
  now wrap each tool call: an exception becomes a readable `✗ '<tool>' failed:
  …` tool result, always emits `runspec:tool_end` (so the spinner clears), and
  moves an `awaiting_human` card back to `running` — the turn recovers or
  concludes cleanly instead of dying with a bare exception and a spinning card.

## [0.220.0] — 2026-07-30

### Fixed
- **Trigger/schedule agent turns reach parity with the chat turn on four
  fronts** — agent-mode automation behaved worse than the Console (chat) tab
  even though they share most machinery.
  - **Arguments are looked up, not guessed.** In `[llm] tool_mode = "search"` an
    unrestricted trigger/schedule turn folded in the *bare* runnable menu
    (`_build_catalog_index()`), which — unlike the chat turn's
    `_chat_index_text()` — omitted the "call `search_runnables` /
    `describe_runnable` for a runnable's arguments and hosts **before running
    it**" instruction. With no inline arg schema on the search-mode
    `run_runnable`, the agent guessed args, failed a few times, then switched
    tool. The trigger turn now folds in `_chat_index_text()` too
    (`bridge_triggers._run_isolated_agent_turn`); the deferred-built-ins section
    stays empty because headless turns keep built-ins inline.
  - **Automation reaches any *configured* host, not just warm ones.** The
    agent-turn resolvers (`_resolve_id_host`, `_resolve_tool_target`) only
    accepted hosts present in the warm, in-profile discovery cache and returned
    *"host 'x' does not have 'y'"* for a configured-but-currently-disconnected
    host — even though execution (`invoke_runnable`) already lazy-connects to any
    configured host, and the stated invariant is "automation reaches any
    configured host on demand". They now fall back to a configured host by
    reusing a warm host's runnable definition (same interface across the fleet;
    execution opens the connection). A host that isn't configured at all still
    errors. New `_configured_host_names()` helper.
  - **The confirm gate stays visible.** In the run drawer the Approve/Deny block
    rendered above the streaming transcript and scrolled out of view as the agent
    worked. It's now a sticky bar pinned to the bottom of the scrolling drawer
    (`TriggersView` `renderDrawerExtra`), always on screen while a confirm is
    parked.
  - **No more wedged "waiting for human".** A confirm that timed out on the
    backend (5-minute default) silently denied and cleaned up but told the UI
    nothing, so a late Approve hit a dead `request_id` and the run card stayed at
    *waiting for human* until a restart. `_finish_confirm` now emits a
    `runspec:confirm_resolved` event (owning card id + `request_id`) on every
    finalize, and the Triggers view clears a gate on that event, reconciles a
    still-pending gate against the card column (mirroring the App badge tracker),
    and seeds live gates from `get_pending_confirms()` on mount (cold-start /
    re-open). The confirm timeout is now configurable and defaults much longer
    for unattended automation: `[console] confirm_timeout` (chat, default 300) and
    `[console] trigger_confirm_timeout` (trigger/schedule, default 3600; `0` waits
    indefinitely) via `config.confirm_timeout` + `Bridge._confirm_timeout_s`.

## [0.219.0] — 2026-07-30

### Added
- **Paste a block of text as a trigger's input** — a new operator-driven
  `paste` event source (`sources/paste.py`). Every other source turns something
  *happening* into an event; this one has no autonomous stimulus — the operator
  pastes a block of text into the Triggers tab and that **is** the event. Build a
  trigger with `source = "paste"` (event `text`) and a per-row **"Run with pasted
  text"** action (a paste dialog, shown only on paste-source triggers) feeds it:
  agent mode receives the text as its input; rule mode templates `{{ text }}`
  into a runnable's args. It's the answer for "I have this log excerpt / email
  thread / ticket body — run my triage trigger on it" without waiting for the
  text to arrive through a monitored channel.
  - **A real run, not a test.** New `TriggerEngine.fire_paste` (bridge
    `submit_paste_input`) dispatches the *named* trigger through its own gates —
    `match` (a paste that doesn't fit the filter is *reported* as skipped, not
    silently run), the working-hours rota (honouring `ignore_rota`), and
    `once_per_thread` dedup — and returns `{ok, skipped?}` so the UI explains a
    gated no-run. It bypasses only the lock gate (a human actively pasting is
    present by definition, mirroring the schedule source's pre-authorisation),
    unlike the "Trigger now" test (`fire_test`), which bypasses every gate.
  - The source is **inert/bridge-owned** (`external_sources`), so registering it
    is purely what surfaces it in the source dropdown (with `text` autocompleting
    the Event field). The payload mirrors the pasted block into both `text` (for
    rule templating, matching the `chat`/`notifications` sources) and `body` (so
    agent-mode `render_event` shows it once under its "Body:" block). Named
    `paste`, not `manual`, to avoid clashing with the `manual` autonomy level.
    See `docs/design/console-triggers.md` §4.4.


## [0.218.0] — 2026-07-29

### Added
- **Telemetry: resolve the backend API key without shipping it in the clear.**
  A shared backend (e.g. LangSmith) needs an API key, but a config seed / Config-
  Sync repo can't carry one in `[telemetry.headers]` without leaking it. Two
  options now solve this:
  - **`[telemetry] api_key_command`** (+ `api_key_header`, default `x-api-key`):
    the key is resolved **at runtime** from a command's stdout and injected under
    that header, so the config distributes only the *command* (`keyring get …` /
    `vault read …` / a bundled venv helper), never the key. Mirrors `[llm]
    api_key_command` and reuses the same venv-aware runner (`adapters/base.py`);
    a failing command disables tracing for the session rather than exporting
    unauthenticated. Settings → Telemetry gains the two fields.
  - **Internal OTel Collector relay** (docs): point the console at an internal
    Collector that holds the one key and forwards to the backend — the desktops
    carry no secret at all. Documented as the recommended topology in
    `docs/console-telemetry.md` (§ "Providing the API key without shipping it").


## [0.217.0] — 2026-07-29

### Changed
- **The chat agent's standing system prompt no longer carries the rota +
  automation inventory on every turn.** Interactive chat turns used to fold the
  full `_console_context()` block — a working-hours rota line (always
  "INSIDE"/"OUTSIDE" + today's window), a lock-auto-suspend line, an optional
  break line, plus the complete list of configured triggers, schedules, saved
  runs, host groups, mailbox, and git repos — into the system prompt of **every**
  turn. That was pure cost on the common turn (operator present, inside working
  hours) and, because the INSIDE/OUTSIDE flag and break time change within a day,
  it churned the model-side prompt cache that the neighbouring date block is kept
  date-only to preserve.
  - The standing chat turn now injects a lean **`_presence_caveat()`** instead: it
    is **empty** on the common turn and emits a single "## Working hours" caveat
    **only** when the operator is interacting *outside* their configured working
    hours (where "not blocked, but confirm before side effects" actually shapes
    behaviour).
  - Nothing is lost — the full snapshot (rota state, lock/break, and the whole
    triggers/schedules/groups/mailbox/repos inventory) still lives on the
    autonomous **`console_status`** tool, pulled on demand.
  - Reactive trigger/schedule turns were never affected: a firing while the
    operator is away is dropped by the engine's rota/lock gate before any LLM turn
    starts, so presence never needed to be a standing prompt concern.


## [0.216.0] — 2026-07-29

### Fixed
- **`max_continuations` no longer starves tool use.** A single agent turn used
  one shared budget of 10 for *both* tool-execution rounds and output-ceiling
  auto-continuations (`stop_reason == "max_tokens"`), so raising
  `[llm] max_continuations` stole capacity from actual tool work — e.g. setting
  it to 7 could leave a multi-step task only ~3 tool rounds before it stopped.
  Continuations and tool rounds now draw from **independent** budgets: continuing
  a long reply can never reduce how many tools a turn may call.
- **No more silent stops.** When a turn reached the loop ceiling it fell through
  with no marker — the agent appeared to just stop. Hitting the tool-round
  backstop now ends the turn with a **visible, resumable** marker
  (`_(paused after N tool steps — say "continue" to keep going)_`); because the
  tool results are already in the conversation, a "continue" message picks the
  work back up. The output-cut-off marker is reworded the same way. The headless
  trigger/schedule loop surfaces the same note in its run card and event log.

### Changed
- **Generous backstops, not per-task dials.** These caps are runaway guards, not
  tuning knobs — the real safety is the confirm-gate (mutating tools pause for
  the operator) and the Stop button. Defaults raised so normal work never hits
  them: reply-continuations `[llm] max_continuations` **3 → 12** (let a long reply
  finish), tool-round backstop **10 → 25**. New optional `[llm] max_tool_rounds`
  overrides the tool-round backstop (default 25); it is a backstop, not something
  to guess per task.


## [0.215.0] — 2026-07-29

### Added
- **OpenTelemetry (OTLP) agent tracing (Settings → Telemetry / `[telemetry]`).**
  Export one trace per agent turn — **`turn → LLM call → tool call`** — over OTLP
  to any OpenTelemetry backend (LangSmith, Langfuse, Phoenix, Jaeger, Tempo).
  Spans follow the OpenTelemetry **GenAI semantic conventions** (`gen_ai.*`) so a
  GenAI-aware backend renders the model and tool calls natively; runspec-specific
  detail (autonomy/operator decision, provenance, cache-token split) rides under
  a `runspec.*` namespace. Covers chat **and** the previously-untraced headless
  **trigger / schedule** turns. Because tool execution dispatches through
  `asyncio.to_thread`, the tool span is opened before the hand-off so a runnable's
  execution stays parented to its turn's trace (one connected tree).
  - **Opt-in and safe by default.** Off unless `[telemetry] enabled = true`; a new
    `otel` extra (`pip install runspec-console[otel]`) carries the SDK and the
    feature reports itself unavailable until installed. The new
    `runspec_console/observability.py` mirrors `trace.py`'s contract — lazy import,
    best-effort, **never raises into the agent loop**, a complete no-op when
    disabled or the SDK is absent.
  - **Privacy.** `capture_content = false` by default: spans carry structure only
    (models, token counts, tool names, decisions, durations) — never prompt /
    completion / tool-argument *text*. With it on, tool arguments are scrubbed
    (secret-looking keys → `«redacted»`) and resolved credentials never reach a
    span.
  - **Config + env.** `config.read_telemetry()` (mirrors `read_web()`);
    `endpoint` / `headers` / `protocol` / `service_name` / `sample_ratio` /
    `ca_bundle` / `tls_verify`. Standard `OTEL_*` env vars fill any value left
    unset — config wins, env fills the gaps.
  - **Adapter enrichment.** A custom adapter can add provider-specific attributes
    to the live LLM span via `runspec_console.observability.set_attributes(...)` /
    `add_completion(...)` (no-ops when off — no hard OTel dependency leaks into a
    plugin). See `docs/console-telemetry.md` + `docs/console-adapters.md`.


## [0.214.0] — 2026-07-27

### Fixed
- **`save_runbook` is now a true partial update — it no longer wipes fields the
  agent doesn't resend.** Editing a runbook (e.g. "fix step 2") that sent only the
  changed field silently blanked its `symptom` and `tags` (only `args` was carried
  forward). Now every omitted field keeps its current value; presence is keyed on
  the field being in the call, so an explicit `""`/`[]` can still clear one while
  omission preserves it. Creating still requires a title and steps (distinct, clear
  errors); updating needs only the id. The tool schema no longer marks
  `title`/`steps` unconditionally required (that rejected small edits), and the
  result reports `Updated`/`Saved`. Description rewritten to spell out the
  create-vs-update contract.

### Added
- **`load_runbook` agent tool — read a runbook's raw definition to inspect or edit
  it.** Returns the editable source as data (id / title / symptom / tags, the raw
  `[[runbook.step]]` rows with `{{placeholders}}` intact, and the parsed args table),
  distinct from `run_runbook` which renders the steps to *follow*. Restores the
  read → edit → `save_runbook` workflow that felt missing since `load_runbook` was
  folded away in 0.202.0.
- **The reintroduction closes the original governance gap by being chat-only.** The
  removed `load_runbook` returned the *same followable steps* as `run_runbook` and,
  sitting outside the `action_allow` vocabulary, let a trigger follow a runbook its
  allowlist excluded. The new tool is confined to interactive chat with the same
  two-layer guarantee the config tools carry — stripped from every headless
  (trigger/schedule) turn by `_filter_headless_tools` (layer 1) and refused by an
  origin guard in its handler (layer 2) — so that bypass cannot exist. Headless turns
  still read a runbook only via `run_runbook`, which `action_allow` governs. It is
  read-only and autonomous (no confirm) in chat. `run_runbook` / `save_runbook`
  descriptions now point at `load_runbook` for inspecting/editing.


## [0.213.0] — 2026-07-27

### Fixed
- **`run_runbook` can now read a runbook without supplying its parameters.** An arg
  with no default is inferred `required`, so consulting/editing a runbook via
  `run_runbook` used to fail with "Missing required argument" for any parameterised
  runbook. The agent read path is now lenient: it renders with whatever values are
  given (unfilled `{{placeholders}}` stay visible), lists the runbook's parameters,
  and flags the unfilled required ones — while a *supplied* bad choice / out-of-range
  value is still rejected. The Forms / portal / schedule paths stay strict (a human or
  visitor is expected to complete the form). New `require=` flag on
  `runbookargs.validate_runbook_values`.
- **`save_runbook` accepts JSON type spellings for parameters.** `type = "string"`
  (and `integer` / `number` / `boolean`) are mapped to the runspec type names
  (`str` / `int` / `float` / `bool`) instead of bouncing as an unknown type
  (`runbookargs.normalize_arg_types`). The tool schema now also lists the valid types.

### Changed
- `run_runbook`'s description clarifies it is the single read/consult/edit-source tool
  for runbooks (there is no separate `load_runbook`), that `values` is optional for a
  read, and that editing is read-here-then-`save_runbook`. Its result now appends a
  parameter summary (`runbookargs.describe_runbook_params`).


## [0.212.0] — 2026-07-27

### Added
- **Structured runbook steps (`[[runbook.step]]`).** A runbook's steps are now an
  ordered array-of-tables — each step its own block with the prose the agent
  follows (`text`) and an optional short `title` — instead of a single long
  `steps = """…"""` string. This keeps `runspec_runbooks.toml` readable and
  diffable in a Config-Sync repo or config seed, where the old blob was hard to
  create, edit, and review. The **Settings → Runbooks** editor gains an ordered
  "Add step" list with per-step reorder/remove, and the agent's `save_runbook`
  tool can author the array too.
- Execution is **unchanged and stays prose-only**: at run time the steps render
  back to numbered prose (`memory.render_runbook`) and go to the agent exactly as
  before — the agent still judges ordering and any conditional skipping (write
  *"skip if healthy"* in a step). A step carries only `text`/`title`; per-step
  `condition`/`goto`/reference fields are rejected (`validate_runbook_steps`), so
  the change stays under the "no workflow engine" cliff.

### Compatibility
- The legacy single `steps` string is **still read** and runs identically; no
  auto-migration. Opening a legacy runbook in the editor best-effort-splits it
  into rows, and saving converts it to the `[[runbook.step]]` form. Runbooks
  merge by `id` in Config Sync, so the nested step array rides along unchanged.


## [0.211.0] — 2026-07-27

### Added
- **Operator actions saved in chat history (agent-invisible on resume).** A
  reopened conversation now shows two operator actions that were previously
  lost — for the operator's own review, and **never replayed to the agent**:
  - **Manual runnable runs.** A run you launch yourself (Forms tab / command
    bar) is saved to the active conversation as a read-only run card, tagged
    *"operator run · not sent to agent"*. If no chat is open yet, a conversation
    is minted so the run is still captured. The scheduled/trigger path and the
    agent's own tool runs are **not** recorded here.
  - **Confirm-gate decisions.** When the agent proposes a gated tool call, your
    decision (**approved** / **edited** / **denied**) rides on the saved tool
    entry as a badge; an edit also stores the agent's original args. A denied
    call is never replayed as if it had run.

  The guarantee that operator records never reach the model is enforced where
  the resume history is built: `resume_conversation` rebuilds from
  `conversations.chat_turns` only, `_flatten_turns` skips operator records (and
  denied tools), and `turns_to_text` skips them from the resume summary too.
  Storage: a `kind = "operator_run"` turn record plus `operator_decision` /
  `proposed_input` annotations on chat tool entries (a turn with `kind` absent
  stays a chat exchange, so existing conversations are unaffected). Gated on the
  existing `[chat_history] enabled`; the run's output is bounded
  (`_OPERATOR_RUN_OUTPUT_CAP`) and the full result stays in the audit log.

## [0.210.1] — 2026-07-26

### Fixed
- **Saved run: an optional password arg is no longer forced required.** In the
  Settings → Saved runs editor (and the Schedules runnable-action editor), a
  runnable's `password` arg is always **force-Open** (asked at run time — the
  synced saved-run file never stores a secret). When that password arg was
  **optional** in the runnable, the generated `params` entry omitted `required`
  and (as a password) carried no default, so re-parsing the preset through
  runspec inference applied *"no default present ⇒ `required = true`"* and
  silently made the operator type a secret — defeating the point of attaching a
  saved credential to supply it. `argDefToParamDef` now **pins the arg's real
  optionality** (emits `required = false` as well as `true`), so an optional
  password stays optional and can be left blank for a selected credential to
  fill. Any optional-with-no-default arg marked Open is fixed by the same change.

## [0.210.0] — 2026-07-26

### Changed
- **Confirm card verb follows the tool.** The autonomy-gate confirm card in the
  chat panel now phrases its header and Approve button after *what approving
  actually does* rather than a hardcoded "run". A `save_runbook` gate reads "the
  assistant wants to **save** `save_runbook`" with an **Approve & save** button —
  it no longer implies the runbook will be *executed* on approval. The verb is
  keyed by the resolved (display) tool name: `save_runbook` → "save",
  `create_document`/`create_schedule` → "create", `open_url`/`open_file` →
  "open", `reveal_in_folder` → "reveal"; every runnable and executing tool keeps
  "run" (the default). Carried on a new `confirm_verb` field of the
  `runspec:tool_confirm` event (and `get_pending_confirms` rows); an older
  frontend that ignores it falls back to "run". `_CONFIRM_VERBS` in
  `bridge_const`, threaded through `bridge_autonomy._register_confirm`.

## [0.209.0] — 2026-07-26

### Added
- **`fetch_url` agent tool — read a web page.** A read-only, autonomous built-in
  lets the chat agent GET an http(s) page and read its **content**: HTML is
  stripped to clean, lightly-marked-up text (headings as `#`, list items as `-`,
  `<pre>` preserved) so the model reads the doc, not the markup. The motivating
  case is "make a runbook from this GitLab Pages / wiki doc" — the agent can now
  read the page directly instead of the operator pasting it in. A GET has no side
  effects, so it runs without a confirm prompt (like `list_files` / `repo_read`),
  and it's a **hot built-in** (stays inline even in `[llm] tool_mode = "search"`,
  no discovery hop). Follows redirects, is size-bounded, and honours the machine's
  proxy environment; `raw` returns the unstripped HTML and `max_bytes` caps a
  single fetch. Pages behind SSH on a fleet host still go through
  `download_file` / `list_files`. New pure, stdlib-only `webfetch` module
  (`urllib` + `html.parser`, zero new runtime deps) and a `[web]` config section
  (`enabled` default true — set false to drop the capability, plus `timeout` /
  `max_bytes` / `ca_bundle` / `tls_verify` mirroring the Config-Sync HTTPS knobs
  for internal hosts behind a private CA or TLS-intercepting proxy).

## [0.208.0] — 2026-07-26

### Added
- **`>` command-bar sigil for saved runs.** The prompt already had `/` (runnables)
  and `$` (runbooks); typing **`>`** now opens a saved-runs menu — search by
  id/title/description/target, ↑/↓ + Enter to pick. Each row is an aligned grid
  (green `>id` | title | target | params-or-status) showing the preset's baked-in
  targeting and whether it `runs now` (no open params) or lists its open params.
  Picking one reuses the existing fork: a preset with no open params runs
  immediately; one with open params opens the params modal — the same path the
  Forms lane uses (`bridge.run_saved_run`; no backend change). `CommandInput.tsx`
  gains `savedRuns`/`onOpenSavedRun` props and reuses the exported, unit-tested
  `filterSavedRuns` from the Forms view.

## [0.207.0] — 2026-07-26

### Added
- **Slash-menu scope indicator.** A sticky caption at the top of the `/` list
  makes the host↔fleet scope explicit: an empty `/` reads "`<host>` · type to
  search all hosts" (scoped to the selected host, with a nudge to the fleet
  search); typing flips it to "Searching all hosts · N matches". `CommandInput.tsx`.
- **Alt+number host selection.** For a runnable collapsed across several hosts,
  the detail hint's host chips are now numbered, and **Alt+1…9** runs on the
  corresponding host without reaching for the mouse (Alt-modified so plain
  digits still type into the search query). The default host chip stays boxed.

## [0.206.0] — 2026-07-26

### Added
- **Resizable host sidebar.** Drag the sidebar's right edge to widen or narrow
  the whole left column — the brand title, the host list (so long host names
  fit), and the footer info area all share the container width and expand
  together. Width is clamped (150–420px) and persisted across restarts
  (localStorage); a faint accent line marks the draggable edge on hover. The
  white-label content watermark's offset tracks the live width. `App.tsx`.

## [0.205.0] — 2026-07-26

### Added
- **Fleet-wide slash-command search.** The `/` menu is no longer limited to the
  sidebar-selected host. An empty `/` still lists the selected host's runnables
  (in discovery order); the moment you type, the search spans **every host in
  the fleet** — matching name, venv, host, or description — with the selected
  host's matches ranked first. Runnables that exist on several hosts (same venv
  + name + subcommand path) **collapse to one row** with an "N hosts" badge, and
  the detail hint turns its host chips into a **run-on picker** — click a host to
  run there, or press Enter for the default (your selected host if it has the
  runnable, else the first). Execution is unchanged: a pick always runs on *that
  runnable's own host* (the run path already targeted `runnable.host`, not the
  sidebar's), so the sidebar host keeps scoping History, Logs, Forms, and
  Analytics while only the command bar goes fleet-wide. `CommandInput.tsx` gains
  `allRunnables`/`selectedHost` props and identity-grouping (`groupEntries`).

## [0.204.0] — 2026-07-26

### Added
- **Detail hint for the highlighted slash-command.** A panel above the `/`
  command list now shows the full, untruncated signature of whichever item is
  highlighted — description in full, plus every argument with its type,
  required/optional, default, and choices — with chips for host, autonomy
  (green when autonomous), and run-as, and a `global` tag on args inherited
  from a parent command. It follows both keyboard ↑/↓ and hover (hovering a row
  now also moves the highlight) and grows to fit its content, so the row list
  stays a tight aligned table while the complete details are always one glance
  away. All from existing discovery data — no new backend calls. `CommandInput.tsx`.

## [0.203.0] — 2026-07-26

### Fixed
- **Slash-command menu no longer hides a runnable's arguments.** The dropdown
  row was a flexbox in which the description (`flexShrink: 0`) claimed its full
  width while the argument-usage column (`flex: 1`) absorbed all the shrinking —
  so on a narrow window the args collapsed to zero width and a runnable appeared
  to show only its description (its args were there all along; discovery merges
  inherited globals into every leaf). The row is now a four-track CSS grid
  (name | host | args | description); every cell truncates independently with
  its own ellipsis, the args column keeps an 88px floor so it can't vanish, and
  the columns align down the list — the clean-table layout. `CommandInput.tsx`.
- **Keyboard ↑/↓ now scrolls the highlighted menu row into view.** In both the
  `/` runnable menu and the `$` runbook menu the selection could walk past the
  280px viewport while only the mouse wheel scrolled the list; the active row
  now `scrollIntoView({ block: 'nearest' })`s as the highlight moves.

## [0.202.0] — 2026-07-25

Runbook discovery: the agent can now *find* a runbook the same way it finds a
runnable, and the runbook list stops taxing every turn as it grows.

### Added
- **`search_runnables` returns matching runbooks**, under a separate top-level
  `runbooks` key (id, kind, title, symptom, tags, and typed `args` so the model can
  fill `run_runbook`'s `values` without a `load_runbook` hop). One search covers
  both kinds deliberately — an agent only searches for what it thinks to search
  for, so a separate `search_runbooks` tool would need it to decide to look.
  Ranking weights differ from the runnable side: id/title 10, **symptom 5**, tags 3
  — for a runbook the query usually *is* a restatement of the symptom. Suppressed
  when a `host`/`group` filter is set (a runbook belongs to neither, so honouring
  the filter is impossible and ignoring it would be a lie), capped at 5 with
  `runbooks_truncated`, and fails soft: a bad runbook store, or one unparseable
  `[runbook.args]` table, can never take down fleet-wide runnable discovery.
- **A runbook id sent to a runnable tool now names the right tool.** Both
  `run_runnable` and `describe_runnable` carried their own "unknown runnable id"
  dead end; both now point at `run_runbook` when the id is a known runbook (bare or
  `runbook:`-prefixed).

- **An agent-followed runbook is now attributed in History/Analytics.** `run_runbook`
  tags the turn's run origin, so the runnables a runbook drove show up under it —
  provenance the Forms path has always set and the agent path never did. It uses a
  narrow `_tag_run_origin_runbook` rather than `_set_run_origin`, which replaces the
  whole record including `trigger`: clearing that would have let a rule trigger reach
  the chat-only config tools by first following a runbook.

### Changed
- **`load_runbook` is gone; `run_runbook` is the single path to a runbook's body,
  and it no longer asks for approval.** The two tools returned the same steps — one
  gated, one not — so the ungated one was simply the faster path and the confirm was
  decorative. It was also a governance hole: `action_allow` scopes `run_runbook` in
  trigger turns but not `load_runbook`, so a trigger could follow a runbook its
  allowlist excluded. Reading a procedure returns *text*; every side effect happens
  later, when the agent calls the runnables those steps name, each gated at its own
  autonomy level. `run_runbook` stays in `_ACTION_TOOL_NAMES` (that set doubles as
  the `action_allow` vocabulary) with an autonomy exception, and joins the search-mode
  hot kernel so following a procedure the folded index already described costs no
  discovery hops. `save_runbook` remains confirm-gated. The security docs' autonomy
  tables are corrected to match — they still listed `save_runbook` as autonomous
  after 0.201.0 gated it.
- **`memory.list_runbooks()` is sorted by id — a prompt-cache fix, not cosmetics.**
  It feeds the agent's tool schemas, which sit before the cache breakpoint, and
  `save_runbook` replaces an entry by delete-then-append. In file order, *any*
  re-save moved the edited runbook to the end of every enum and menu, changing the
  tools block byte-for-byte and invalidating the cached prefix for the rest of the
  session — so a steps-only edit that changed nothing the model can see still cost
  a full cache rewrite. Storage order is untouched. A new test asserts the whole
  tool list is byte-identical across a steps-only re-save.
- **The runbook menu embedded in the `load_runbook` / `run_runbook` descriptions is
  capped at 25**, mirroring the catalog index, with the overflow pointing at
  `search_runnables`. That text is re-sent every turn, so it must not grow without
  bound; search has no cap pressure because it runs on demand.
- **The folded catalog index's preamble is now a generic header.** It hard-coded
  "Runnables you can execute via run_runnable… This is the full menu", which was
  wrong twice over — it labelled the deferred-built-ins section as runnables, and
  claimed completeness even when the index had truncated at `tool_index_limit`.
  Each section now labels itself.

## [0.201.0] — 2026-07-25

Phase 1 of the multi-model agent work: the prompt and runbook hygiene that has to
be right before models are routed per task.

### Added
- **`[llm] system_append`** — extra system-prompt text *added to* whichever base
  applies, instead of replacing it. `[llm] system` still replaces the built-in
  default wholesale (unchanged, so no existing custom prompt shifts under anyone),
  and that is exactly the problem: setting any site policy of your own silently
  cost you the built-in clarify-don't-guess guidance, which is the behaviour that
  most directly prevents the agent guessing instead of asking. `system_append`
  lets you have both.
- **`save_runbook` can author parameters.** The agent's tool schema gained `args`
  — the same `[runbook.args]` grammar the Settings editor writes — validated
  before the write, with the reason returned to the model so it can correct itself
  in the same turn. Previously the agent could only ever create parameter-less
  runbooks, even though the docs described asking it for one with arguments.
- **`runbookargs.validate_runbook_arg_table()`** — an up-front soundness check on
  a runbook's arg *declaration* (every arg a table, every declared `type` actually
  registered), shared by both writers. `parse_runbook_args` is lenient by design,
  so an unknown type used to sail through and only surface later as a broken form.

### Changed
- **`save_runbook` is now confirm-gated.** A remembered fact is advisory; a
  runbook is a procedure the agent — and every console it syncs to — will later
  follow, so an unreviewed edit propagates. `remember`, `search_memory` and
  `load_runbook` stay autonomous. `save_runbook` remains in `_MEMORY_TOOL_NAMES`
  (that frozenset is also the dispatch key and the search-mode hot-kernel key);
  the gate is a special case ahead of it, matching `manage_denylist` / `run_sql`.
- **The `load_runbook` and `run_runbook` menus name each runbook's `symptom`**,
  not just its title. `memory.list_runbooks()` always returned it and both call
  sites dropped it, leaving the agent with names it couldn't choose between.
- **The default system prompt points the agent at runbooks** — prefer an existing
  procedure over improvising, and offer to capture a correction that would apply
  again. It proposes; the gate means you approve.
- **A long or multi-line argument in a confirm renders as a textarea.** A
  runbook's `steps` (or a document body, or a SQL statement) in a one-line input
  showed only its first line, so approving it was theatre.

### Fixed
- **An agent edit no longer destroys a runbook's parameters.**
  `memory.save_runbook` replaces the whole entry, and the agent's handler passed
  neither `args` nor `discoverable` — so every agent edit of an existing runbook
  silently deleted its `[runbook.args]` table and its publish audience. The
  handler now carries omitted fields forward.
- The `run_runbook` Console card showed a bare `run_runbook` with no id: the label
  read `id`/`name` but the tool's parameter is `runbook_id`. `save_runbook` now
  falls back to the title, which is what identifies a newly created runbook.

## [0.200.0] — 2026-07-24

### Added
- **Custom sound files + sound packs.** A sound category can now point at the
  operator's own audio clip: a new `file:<path>` token (`.wav` **or** `.mp3`)
  joins `synth:`/`system:`/`off`, picked via a native file dialog in
  Settings → Notifications (new bridge `browse_sound_file`). A **sound pack**
  (`[console.sounds] pack = "<dir>"`) maps a whole folder of category-named
  files (`confirm.wav`, `host_down.mp3`, …) in one pick — resolution order is
  explicit token (an explicit `off` still silences) → pack file → built-in
  default (`sounds.pack_lookup`/`pack_coverage`, `config.sound_choice_set`).
  Settings shows the pack's coverage (`sound_pack_info`) and every category row
  gains a ▶ preview button that plays the row's *unsaved* selection
  (`test_sound`). `file:` paths and the pack folder are machine-local — a
  config seed should stick to `synth:`/`system:`/`off` tokens.
- **Connection-awareness cues.** Four new sound categories:
  `host_up`/`host_down` (an SSH host's refresh-loop probe flips its connected
  state) and `room_up`/`room_down` (the active chat/helpdesk room drops or
  comes back). All are debounced through the new
  `sounds.ConnectionCueGate` — first observation silent (no startup chorus),
  cues only on a genuine flip, ≥30 s per-key cooldown so a flapping host can't
  become a metronome. Hooks: `_probe_one` (unattended flips only — interactive
  test/copy-key flows already have visual feedback) and the room supervisor's
  connect/drop dispatches.

### Changed
- **MP3 + per-sound volume via MCI.** File-based cues (bundled synth,
  `file:`, packs) now play through the Windows MCI API (`winmm` via stdlib
  `ctypes`, still zero dependencies): async, mp3-capable, and honouring a new
  `[console.sounds] volume` (0–100, Settings slider) — something `winsound`
  could not do. `system:` aliases stay on `winsound` (no per-sound volume);
  `winsound` remains the fallback for wav if an MCI open fails.
- **Richer default cues.** The bundled synthesized set is regenerated
  (`sounds/_generate.py`) from single short plinks into layered motifs —
  sweeps, chords, harmonic tails (~0.3–0.7 s) — so the cues read as one
  console voice instead of isolated beeps, plus four new cues for the
  connection categories. Still fully synthesized in-repo; no licensing
  exposure.

## [0.199.0] — 2026-07-24

### Changed
- **Muted-grey consolidation (Console UI).** The ~150 scattered inline greys
  (`#666`×40, `#888`×39, `#999`, `#555`, `#8c8c8c`, …) that all served the
  same few "muted text" roles are now three semantic tokens in
  `ThemeContext.tsx` — `textSecondary` (readable labels/descriptions,
  `#aaa`/`#666`), `textMuted` (hints, separators, muted icons,
  `#888`/`#999`), `textFaint` (placeholders, carets, off-state,
  `#555`/`#bbb`) — so identical-purpose text renders identically across tabs
  and the whole hierarchy is tunable from one file. Call sites with `isDark`
  in scope use the functions; the rest use CSS vars
  (`--rs-text-secondary`/`-muted`/`-faint`) stamped on `<html>` by App.tsx
  from the same definitions (with first-paint defaults in `index.css`).
  Deliberately untouched: status colours (connected/disconnected dots, run
  kanban column greys, the paused icon), the fixed-dark log panels
  (LogStream, History full-log) and their log-level colour maps, primary
  text pairs, and border pairs — those are semantic, not muted-text roles.
  Visually a near-no-op: each converted site moves at most one step in
  brightness.

## [0.198.0] — 2026-07-24

### Changed
- **Console UI visual polish** (frontend-only; no behavioural or bridge-API
  changes):
  - **Accent colour centralised.** New `useAccent()` (AccentContext in
    `ThemeContext.tsx`) carries the resolved app accent — a white-label
    `[console.theme] accent_color` now reaches every surface that previously
    hardcoded the built-in blue pair: PathInput browse breadcrumbs, Schedules
    arg-key colouring, Forms runnable-card hover, Analytics chart series
    (`ACCENT`/`BRAND_RANGE`), transcript links (OutputPanel), the command
    palette runnable names, and the auto-switch toggle. Log-level colours
    (INFO), DevView call-direction colouring, and the SavedRunCard's distinct
    cyan are deliberately unchanged. Also fixes the PathInput breadcrumb
    colour in the light theme (was the dark-theme cyan on white).
  - **Two-font system.** Chrome (nav, labels, buttons, prose) now uses the
    system UI font; values, args, host names, logs, and the command bar stay
    monospace via explicit `fontFamily` (the sidebar wordmark keeps its
    monospace terminal identity). The command-bar textarea's monospace is now
    explicit instead of relying on the browser form-control default.
  - **CSS-level interaction polish** (`index.css`, keyed on a new
    `data-theme` attribute + `--rs-accent` var stamped on `<html>`): hover
    feedback on sidebar host rows, group headers and nav tabs (CSS `:hover`,
    no re-renders); thin theme-aware scrollbars; a 140 ms fade when a tab
    becomes visible; a `:focus-visible` accent ring on the nav tabs; and a
    gentle pulse on the footer room indicator while the active room is
    connecting. Animations honour `prefers-reduced-motion`.

## [0.197.0] — 2026-07-24

### Added
- **Per-card "in context" toggle (Console tab).** Each Console card carries a
  reversible checkbox in its header controlling whether its content is part of
  the agent's context, making the previously-invisible in/out state explicit:
  - **Chat cards** default **in context** (checked). Unticking drops that
    exchange from what the model sees on later turns — useful for pruning a
    tangent so it stops steering the agent and stops filling the context
    window. New bridge method `set_turn_included(chat_id, included)`;
    `_chat_history` messages are tagged by turn (`_history_turn_ids`) and a new
    `_visible_history()` filters excluded turns out when assembling the model
    messages, preserving the API's "starts on a user message /
    tool_use·tool_result pairs stay paired" rules.
  - **User-run cards** default **out of context** (unchecked), codifying
    today's behaviour (a manual run never entered chat context). Ticking one
    **passively stages** its output to ride along with your **next** typed
    message — the quiet sibling of the existing "Ask the assistant" button
    (which still fires an immediate turn).
  - **Reversible & non-destructive:** excluding never deletes — the backend
    keeps every message and only filters, so re-ticking restores the exchange
    instantly.

### Changed
- **"Compact now" is the commit point for the in/out state.** Compaction now
  physically drops excluded turns **without** summarising them (so removed
  content can't leak back via the note), slices the aligned turn tags in
  lockstep, and reports the dropped `chat_id`s (`runspec:chat_compacted`
  `dropped_turns`) so their cards' checkboxes are retired (the messages are
  gone and can no longer be re-included).
- **Context-window gauge refreshes after a compaction.** Previously the gauge
  only updated on the next turn (it listens to `runspec:context_estimate`,
  which `compact_chat` didn't emit); it now estimates the drop from the
  compacted-message count immediately, with the next turn replacing it with the
  exact figure.


## [0.196.0] — 2026-07-23

### Added
- **`list_files` built-in agent tool.** Read-only, autonomous directory
  listing on the local machine, a fleet host, or an **ad-hoc SSH host**
  (reached directly by default — `source_via` only when the console can't
  reach it; optional `source_credential`), so the agent can find exact paths
  *before* reaching for `download_file`/`relay_file` instead of guessing.
  Thin wrapper over the Forms path-picker's `list_dir` and the Transfer
  dialog's cached ad-hoc browser `list_dir_adhoc`; entries carry
  name/type/size/mtime, capped at 300 with a visible note. Kept in the
  search-mode **hot kernel** (inline) — browsing is the entry point of most
  file tasks. New `_FILES_TOOL_NAMES`; `_files_tools`/`_run_files_tool` in
  `bridge_agent_tools`.
- **`use_tool` inner-input preflight (search mode).** The deferral wrapper
  now validates the inner input against the deferred tool's real schema —
  unknown arguments, missing required arguments, and enum violations (top
  level, plus enums inside plain string arrays) bounce in-turn with the valid
  shape *before* any confirm, mirroring `run_runnable`'s
  `_preflight_validate`. Previously a model that skipped `describe_tool` got
  opaque handler errors or silently ignored fields. Schemas that opt into
  open input via `additionalProperties` skip the unknown-key check.
  `_preflight_builtin_input` in `bridge_search`.

### Fixed
- **Transfer tools claimed an ad-hoc host needs a jump.** The `download_file`
  and `relay_file` top-level descriptions still said an ad-hoc source is
  "reached through a configured jump" — stale since the jumpless ad-hoc
  connect (#440) made direct the default; only the buried `source_via`
  property said otherwise, so the agent kept inventing jump hosts for
  directly reachable machines. Both descriptions now lead with "reached
  directly — jump only if unreachable", the fact sits in the first sentence
  (which is what the search-mode deferred-tools menu shows), and
  `download_file` points at `list_files` for path discovery.


## [0.195.0] — 2026-07-24

### Added
- **Copy-key bootstrap for `jump` hosts.** "Copy key to host" / "Copy key to all
  hosts" previously always used paramiko, so it failed for a legacy `jump` host
  (paramiko can't negotiate CentOS 6's crypto). The bootstrap now diverts a
  `jump` host off paramiko: with `sshpass` on PATH it feeds the password to the
  system `ssh -J` client (via the `SSHPASS` env var, never argv;
  `PubkeyAuthentication=no` so the password is what's tested,
  `StrictHostKeyChecking=accept-new` matching the console's TOFU default); on
  stock Windows (no `sshpass`) it surfaces the exact `ssh-copy-id -o ProxyJump=…`
  command to run once in a terminal, instead of a confusing paramiko error. After
  that one-time push, the key-auth jump transport connects normally. New
  `executor.has_sshpass`/`ssh_jump_copy_id`/`jump_copy_id_command` and
  `bridge_config_ssh._push_key_over_jump`; non-`jump` hosts are unchanged. See
  `docs/design/console-jump-transport.md`.

---


## [0.194.0] — 2026-07-24

### Added
- **System-ssh jump transport for legacy hosts (CentOS 6).** A host entry can
  now carry a `jump = "<ssh host>"` key. The console reaches that host through
  the **system `ssh` binary** with `-J <jump>` (ProxyJump) instead of paramiko,
  so `~/.ssh/config` legacy-algorithm settings apply — the way to reach hosts
  offering only `ssh-rsa`/`ssh-dss` (OpenSSH 5.3), which paramiko 4+/5 removed
  and can no longer negotiate (`>=3.0` resolves to 5.x today). The final SHA-1/
  DSA handshake is done by OpenSSH, so paramiko never touches the legacy host.
  `jump` selects the transport (bypasses the pool — a per-call `ssh` subprocess)
  and sets the hop; the target's legacy `HostKeyAlgorithms`/`KexAlgorithms`,
  user, port, and any further ProxyJump chain come from `~/.ssh/config`, exactly
  as `runspec jump` already does. Wired through discovery (`runspec local`),
  the connectivity probe, and runnable execution for both **Forms** and the
  **agent** (`run_as`/secrets/cred-env intact, streamed output). New
  `executor._system_ssh_argv`/`ssh_jump_run` and a `run_remote(…, jump=…)`
  branch; routed in `bridge_internals._discover_one`/`_check_connected` and the
  two `run_remote` call sites. The Hosts editor gains a **Jump host (ProxyJump)**
  field and the agent's `manage_hosts` tool exposes `jump`. Requires an OpenSSH
  client on the console machine. **Not yet covered:** SFTP file transfer and
  ad-hoc `direct-tcpip` to a `jump` host (paramiko-channel features). See
  `docs/design/console-jump-transport.md`.

---


## [0.193.0] — 2026-07-23

### Added
- **Subcommand support for runnable-action schedules.** A schedule can now
  target a runnable's subcommand, exactly like a saved run: the Add-schedule
  modal shows the same conditional **Subcommand dropdown** (only when the
  runnable declares subcommands; required when it enforces `require-command`),
  and picking a path switches the rich args block to the **effective arg set**
  at that path (inherited globals + the command's own, child wins) — reusing
  the shared `commandPathOptions`/`effectiveArgsForPath` helpers from the
  saved-run editor. The schedule entry gains a **`command`** key (path list;
  a bare string also accepted), threaded through the fire path end-to-end:
  `ScheduleJob.command` + `parse_schedule_entry`, the `RunFn` timer callback,
  `_run_scheduled`/`run_schedule_now` → `_invoke_scheduled(command_path=…)`
  (the plumbing `_invoke_fanout` added in 0.186.0), and the agent's
  `create_schedule` tool (new optional `command` array). The schedules table
  shows the full `/runnable sub path` per row. Existing schedules without a
  `command` are untouched (empty = root command).


## [0.192.0] — 2026-07-23

### Changed
- **Saved-run editor: subcommand field only when it applies, and no more
  overflowing runnable select.** The runnable select is full-width and its
  *closed* control now shows just the runnable name (`optionLabelProp` — the
  rich `/name` + description label is dropdown-only), so a long description
  can't push the field off the modal; the Schedules modal's runnable select
  gets the same treatment. The free-text "Subcommand (optional)" input is
  gone: a runnable that declares subcommands gets a **dropdown of its command
  paths** (nested paths space-joined, required when the runnable enforces
  `require-command`), and one that doesn't gets no field at all. Picking a
  subcommand switches the rich args block to the **effective arg set at that
  path** (inherited globals + the command's own, child wins — the same set
  `runspec serve` advertises per leaf), so command-level args now get typed
  fields and Fixed/Open toggles instead of needing manual param rows. New
  shared pure helpers `commandPathOptions` / `effectiveArgsForPath` in
  `RunnableAction.tsx` (unit-tested); the free-text input survives only in the
  undiscovered-runnable fallback.


## [0.191.2] — 2026-07-23

### Fixed
- **Command composer cleared its draft when stepping back down out of
  history.** Browsing prior prompts with ArrowUp and then pressing ArrowDown
  back to the bottom blanked whatever was half-typed. The live draft is now
  stashed on the first step up into history and restored when returning to the
  bottom, instead of clearing. (Follow-up to the ArrowDown history-navigation
  fix.)


## [0.191.1] — 2026-07-23

### Fixed
- **Empty runnable dropdown in the Schedules modal and the Saved runs editor**
  (0.191.0 regression). The shared runnable picker rendered its
  `<Select.Option>`s from a custom component child (`RunnableSelectOptions`),
  but antd's Select discovers options by inspecting its direct children and
  never renders a custom component to find them — so the dropdown came up
  empty in both dialogs. The picker is now plain data through the Select's
  `options` prop (`runnableSelectOptions(...)`), with a unit test guarding the
  data shape (incl. the "not currently discovered" fallback entry).


## [0.191.0] — 2026-07-23

### Changed
- **Saved-runs editor at parity with the Schedules modal.** The Settings →
  Saved runs dialog now uses the same components as the Schedules tab's
  run-a-runnable dialog — extracted into a shared **`RunnableAction.tsx`**
  module (`TargetFormItems`, `RunnableArgsBlock`, `CredentialsSection`,
  `RunnableSelectOptions`, `runnablesForTarget`) so the two stay in sync by
  construction. The runnable is a target-aware searchable select (was free
  text); its declared args render as rich typed Forms fields with a per-arg
  **Fixed/Open toggle** — an Open arg is auto-copied into the params table
  (`argDefToParamDef`/`splitFixedAndOpen`), a `password`-typed arg is always
  Open (the synced file never stores a secret), and a required arg must be
  either fixed or Open; the credential picker gains the env-var/arg-binding
  preview. Manual param rows remain for extras the runnable doesn't declare;
  the raw key=value editor reappears when the runnable isn't currently
  discovered, and editing a saved run whose runnable is undiscovered keeps it
  selectable ("not currently discovered") instead of losing it.

### Fixed
- **Add-schedule modal width.** The "When it fires, it will…" segmented control
  (five options since the Saved run action) no longer overflows — the modal is
  620px wide (was 560).


## [0.190.2] — 2026-07-23

### Fixed
- **`use_tool` confirm card stuck on a spinner (search mode).** A confirm-gated
  deferred built-in called through `use_tool` (e.g. `outlook_create_event`)
  never showed its Approve/Deny buttons — the chat card sat on "use_tool" with
  a spinner until the 300 s gate timeout denied it. Cause: 0.190.0 unwrapped
  the wrapper at the top of the autonomy gates, so the confirm prompt was
  registered under the inner tool's name while the frontend matches the prompt
  to the running tool segment by exact raw name (`use_tool`) — the documented
  `run_runnable` contract in `_register_confirm`, re-broken for the new
  wrapper. The gates now follow the `run_runnable` pattern precisely: the
  inner tool is resolved up front (invalid names still fail fast), the
  **decision** reflects the inner tool (`_effective_tool_autonomy` recurses,
  so an autonomous inner tool still runs promptless), the **prompt** keeps the
  raw `use_tool` name/input so the buttons render, the **card** displays the
  inner tool + args via `_confirm_card_view` (which also makes the frontend
  render the confirm read-only, as for `run_runnable`; a defensive branch
  applies any edit to the inner input), and **execution** applies the inner
  call after approval so Console tracking and audit record the real tool.
  Regression-tested in `test_builtin_search_tier.py`, including the
  `runspec:tool_confirm` payload contract (raw `tool_name`, inner
  `display_name`/`display_input`).


## [0.190.1] — 2026-07-23

### Fixed
- **"Copy SSH key to all hosts" no longer wipes the password field mid-typing.**
  The bulk dialog reset its state (including the password) on every change to
  its `hosts` prop, and the Settings drawer recomputes that array on every
  render — so a background connection-status refresh, or a host flipping
  connected after a failed first attempt, cleared the field while the operator
  was still typing (most visible when retyping after a first-attempt typo). The
  reset now runs only on the closed→open transition, so parent re-renders leave
  the in-progress password untouched (`CopyKeyAllModal.tsx`).


## [0.190.0] — 2026-07-22

### Added
- **Deferred built-ins in search mode (`[llm] tool_mode = "search"`).** The
  chat turn's built-in tools now split the same way the runnables did when
  search mode was introduced: a **hot kernel** stays inline (`console_status`,
  `search_history`, the memory/runbook tools, and the `workspace_*` tools —
  the ones a turn reaches for constantly, or that spilled tool results
  reference), and the **long tail** (Outlook, repo, transfer/denylist, action,
  exec, and config tools — ~40 schemas) leaves the per-turn tool list. The
  agent discovers them from a one-line-per-tool menu folded into the
  conversation next to the runnable catalog index (same `[llm] tool_index`
  knob), fetches full schemas on demand with the read-only **`describe_tool`**,
  and runs one via **`use_tool`** — a thin wrapper the autonomy gate **unwraps
  to the inner tool before any decision**, so the inner tool's own confirm
  rules, Console card, tracking, and audit record apply unchanged (the same
  inner-name gating `run_runnable` has always used). `_run_tool_sync` fails
  closed on a still-wrapped `use_tool` (refuses rather than executes), and an
  un-unwrapped wrapper classifies as `confirm`. **Headless (trigger/schedule)
  turns are unaffected**: they keep every built-in inline
  (`_runnables_to_tools(inline_builtins=True)`) because `action_allow` can
  only narrow tools it can see, and `_filter_headless_tools` strips the
  deferral meta-tools unconditionally — automation never gets a run-by-name
  side door. Zero-code escape hatch: `[llm] defer_builtins = false` keeps
  built-ins inline in search mode. Dedup/per_host modes are byte-identical to
  before. New `_BUILTIN_META_NAMES`/`_HOT_BUILTIN_NAMES` in `bridge_const`;
  `_deferred_builtin_tools`/`_builtin_meta_tools`/`_run_describe_builtin_tool`/
  `_unwrap_use_tool`/`_build_builtin_index`/`_chat_index_text` in
  `bridge_search`.


## [0.189.0] — 2026-07-22

### Added
- **`search_history` built-in agent tool.** The chat agent (and trigger/schedule
  turns — it is read-only) can now query the run history the History tab shows:
  newest-first audit records with timestamp, host, operator (OS user), run-as
  target, exit code, duration, args, and provenance (`initiatedBy` user/llm plus
  the trigger/schedule or runbook that caused the run). Answers "when was X last
  run, by whom, and did it succeed" without opening the UI. Filters: `runnable`
  (exact match first, substring fallback), `host` (enum of local + fleet; a
  host-scoped query passes the runnable through so only its log files are read,
  falling back to an unfiltered read for the substring case), `failed_only`, and
  `limit` (default 10, max 50; truncation is visible via a `note`). Heavy fields
  (per-run log lines, traceback frames) are stripped by default — exceptions
  slim to type + message — and each record carries its `runId`: pass it back as
  `run_id` to fetch that one run *with* its logged output, or set `include_log`
  to attach log lines to search results (tail-capped at 200 lines per record,
  with tighter record limits, default 3 / max 10). A host-scoped query reads
  that host over its own SSH exec even without a standing connection; only the
  no-host sweep is limited to local + currently connected hosts. Autonomous (no confirm gate): it reads the same records the
  operator already sees. Reuses the reporting mixin's `get_history` /
  `get_history_all`, so only local + currently connected hosts are searched.
  New `_HISTORY_TOOL_NAMES` in `bridge_const`; schema/handler in
  `bridge_agent_tools` (`_history_tools`/`_run_history_tool`/
  `_slim_history_record`).


## [0.188.2] — 2026-07-22

### Fixed
- **Outlook reminders (`create_reminder`) fired off by the machine's UTC offset —
  the same bug as `outlook_create_event` (0.188.1), in the reminder path.** All
  three reminder builders assigned a Python `datetime` to a COM date property
  (`_build_appointment_reminder` → `appt.Start`; `_build_task_reminder` →
  `task.StartDate`/`DueDate`/`ReminderTime`; `_build_flagged_mail_reminder` →
  `mail.TaskStartDate`/`TaskDueDate`/`ReminderTime`), which pywin32 UTC-shifts, so
  a reminder popped an hour early on a UTC+1 box (most visibly the Rota "Take a
  break" auto-resume "break over" nudge). Each now assigns the wall-clock as an
  unambiguous year-first ISO **string** via `_com_local_time`, so Outlook keeps
  the intended local time. Unit-tested against the fake COM items; the offset
  itself is a Windows/Outlook COM effect to confirm on the operator's machine.


## [0.188.1] — 2026-07-22

### Fixed
- **`outlook_create_event` scheduled meetings off by the machine's UTC offset.**
  The tool told the agent to pass local time (correct), and the tool layer handed
  Outlook a naive local `datetime` — but pywin32 marshals a Python `datetime` to
  the COM `VT_DATE` through a UTC conversion, while Outlook's `Start`/`End` are
  *local wall-clock*, so the appointment landed shifted by the local UTC offset (a
  17:00 local meeting saved as 16:00 on a UTC+1 box). `_build_event` now assigns
  the wall-clock as an unambiguous year-first ISO **string** (`_com_local_time`),
  which Outlook coerces to a local `DATE` with no shift — the clock time is
  preserved exactly. Pure/unit-tested (the string form is asserted against the
  fake COM appointment); the offset behaviour itself is a Windows/Outlook COM
  effect that must be confirmed on the operator's machine.


## [0.188.0] — 2026-07-22

### Added
- **`[llm] effort` — reasoning effort (adaptive extended thinking).** A
  named-level dial (`off` default / `low` / `medium` / `high` / `xhigh` / `max`)
  that sets how hard the model thinks before it answers. Uses Anthropic's
  **adaptive** thinking shape (Claude 4.6+): the console sends
  `thinking = {"type": "adaptive"}` plus `output_config = {"effort": <level>}`, so
  the model self-manages its own thinking depth and it counts toward `max_tokens`
  (no separate budget to reserve — `max_tokens` is left as the operator set it).
  Wired into the `anthropic` and `bedrock` built-ins — both talk the Anthropic
  SDK, so the request shape is identical — and forwarded to any plugin adapter
  declaring an `effort` parameter. Thinking blocks survive tool-call turns
  automatically (`make_tool_turn` reuses the raw response content), so effort
  composes with tool use. Shared pure helpers `adapters.base.effort_level` /
  `apply_thinking` (unit-tested). This deliberately avoids the deprecated
  `thinking = {"type": "enabled", "budget_tokens": N}` shape, which is rejected
  with a 400 on Opus 4.8/4.7, Sonnet 5, and Fable 5.
- **`effort` on the `langserve` adapter / gateway proxies.** The LangServe
  built-in now also honours `[llm] effort` — since it can't assume your gateway's
  request shape, it sends the level as a plain pass-through string under an
  OpenAI-style `reasoning_effort` field, with `[llm] effort_key` / `effort_in`
  renaming the field / picking the `/invoke` slot (mirroring `max_tokens_key` /
  `max_tokens_in`). A gateway wanting the native Anthropic adaptive shape
  overrides `_build_payload` and calls `apply_thinking` (documented, with a
  worked snippet). A plugin adapter gets `effort` auto-forwarded the moment its
  `__init__` declares the parameter. Docs: `docs/console-adapters.md`.

### Fixed
- **The agent scheduled meetings in UTC.** Every agent turn's date block
  (`_now_context()`) carried the operator's local *date* but no *timezone*, so the
  model had no local anchor and defaulted to UTC for clock times (e.g. a "2pm"
  meeting landed at 2pm UTC). The block now also states the operator's **timezone**
  (name + UTC offset, e.g. `CDT (UTC-05:00)`) and instructs the agent to produce
  and interpret clock times in **local** time, never UTC. Still date + timezone
  only (both stable across a day) so the model-side prompt cache keeps hitting —
  the exact current *time* stays on the on-demand `console_status` tool.
  `outlook_create_event`'s `start`/`end` descriptions now say "local time, don't
  convert to UTC" and point at `console_status` for relative times.

### Changed
- **The default agent prompt now prefers a clarifying question over guessing.**
  `_base_system`'s default (used when `[llm] system` is unset) tells the agent to
  ask one brief clarifying question — rather than guess — when a required argument
  is missing or ambiguous and getting it wrong would matter (scheduling, sending
  mail, host targeting, anything destructive), while still proceeding on low-stakes
  or clearly-implied values. A configured `[llm] system` replaces the default
  wholesale, so operators using a custom prompt should add this themselves.


## [0.187.0] — 2026-07-21

### Added
- **`outlook_create_event` gained `open_in_outlook`, the reliable path to a Teams
  meeting on Outlook Classic.** Real-world testing confirmed the Teams Meeting
  add-in only hooks the Outlook window and does **not** run for COM-created
  meetings, so the `online_meeting` registry toggle never attaches a link to them.
  With `open_in_outlook=true`, `create_event` Saves the event unsent and
  `Display()`s it — it pops open in Outlook where the add-in is live, so the
  operator clicks the "Teams Meeting" button (one click) and Sends (`sent` is
  `False`; the event still gets an `entry_id`).
- **`outlook_event_join_url` tool** (autonomous read; `read_event_join_url`) reads
  the Teams join URL back off an event by `entry_id` once the operator has added
  the meeting — closing the loop so the link can be reused elsewhere (e.g.
  `open_url`, another channel) without the Graph `OnlineMeetings` scope. Returns
  `""` until the meeting is added.
- `online_meeting` is downgraded to a documented best-effort bonus (it toggles the
  machine-wide setting but doesn't apply to COM-created meetings); the tool now
  points the agent at `open_in_outlook`. Docs: `docs/design/outlook-agent-tools.md`.


## [0.186.0] — 2026-07-21

### Added
- **Saved runs — deterministic presets.** A saved run is a runnable + target
  host(s) + fixed args, with optional **typed open params** filled at run time —
  the "Run now" idea freed from the Schedules tab and made a first-class, saved,
  shareable item. No model involved: it fires exactly as stored. Stored in
  **`runspec_saved_runs.toml`** (`[[saved_run]]`, **rides Config Sync**, merged
  by `id`); targeting mirrors a runnable-action schedule exactly (one of
  `host` / `group` fan-out / `hosts` first-connected failover /
  `selected_hosts` fan-out); `params` is a runspec args table (same grammar as
  `[runbook.args]`, parsed/validated via `runbookargs`), a param may not shadow
  a fixed arg, and secrets ride `credential_ids` (keychain) — never fixed args.
  Surfaces:
  - **Forms tab** — a "Saved runs" lane; clicking opens a params-only run
    dialog (`SavedRunRunModal`) with a read-only summary of the baked-in
    target/args/credentials; zero-param presets run immediately; multi-host
    presets render one Console run block per host.
  - **Agent** — a `run_saved_run` action tool whose autonomy gate resolves the
    **underlying runnable** (grouped editable confirm across a fan-out,
    most-restrictive wins, `manual` blocks — the same machinery as a group
    `run_runnable`), a chat-only `manage_saved_runs` config tool
    (list/get autonomous, save/delete confirm), and a "Saved runs"
    console-context block so the agent knows what exists.
  - **Schedules** — new `action = "saved_run"` (+ `values` for the open
    params) in the Schedules modal, `create_schedule` agent tool, and
    `run_schedule_now`; the preset's own targeting decides the hosts.
  - **Settings → Saved runs** — the collection editor (validated on save).
  New module `savedruns.py`, bridge mixin `bridge_saved_runs.py`
  (`get_saved_runs`/`save_saved_runs`/`run_saved_run`), docs at
  `docs/console-saved-runs.md`.

### Changed
- **`_invoke_scheduled` generalised to `_invoke_fanout`** (bridge_schedules) —
  the shared deterministic targeting+invoke engine behind runnable-action
  schedules and saved runs. In passing this adds **subcommand (`command_path`)
  threading** to scheduled/deterministic runs, which the schedule path
  previously dropped entirely.
- The agent exec path (`_run_tool_sync`) now accepts a `credential_ids` list
  and a list-valued `command` riding a call's args (popped before dispatch, as
  the singular forms already were) so a saved run's stored credentials and
  subcommand path reach a gated agent-side run.


## [0.185.0] — 2026-07-21

### Added
- **Transfer dialog defaults a local destination to the agent's workspace.** When
  the destination is *This computer*, the folder now pre-fills with the current
  chat session's agent workspace (`config.workspace_dir` for the active
  conversation) — so a downloaded or relayed file lands where the agent can then
  act on it (grep it, run a `run_python` snippet over it, etc.), tightening the
  human→agent hand-off. It only pre-fills while the field is empty (never
  clobbers a folder the operator typed), falls back to blank when there's no
  active chat, and stays fully editable. New best-effort bridge method
  `current_workspace_path()` (creates the workspace dir so it's immediately a
  valid destination; returns `""` on no-session/error) —
  `bridge_transfer.current_workspace_path`, wired in `TransferModal`.


## [0.184.0] — 2026-07-21

### Added
- **`open_file` + `reveal_in_folder` agent action tools, and `workspace_info`.**
  The agent can now surface a workspace file to the operator on the console
  machine: `open_file` opens it in its OS default app, `reveal_in_folder` shows it
  (highlighted) in the file manager. Both are confirm-gated action tools (in
  `_ACTION_TOOL_NAMES`, allowlistable per trigger, in `list_action_tools`) that
  take a path **relative** to a `location` root — `workspace` (the session
  sandbox, default) or `documents` (where `create_document` writes) — resolved
  through the same `workspaces.jail` as the read tools (`_resolve_jailed_path`), so
  neither can escape those two console-owned roots. **Security split:** `open_file`
  is restricted to a passive-viewer extension allowlist (`_OPENABLE_EXTS`:
  pdf/txt/md/csv/log/json + common images) so "open" can't become code execution;
  anything else goes through `reveal_in_folder` (any type — it only reveals, never
  executes) and the human decides. Rationale: the agent acts on untrusted external
  input, and "open by default app" is extension-driven shell execution, so the
  allowlist + jail are the boundary, not the confirm gate.
- **Workspace self-awareness.** New autonomous `workspace_info` tool reports the
  session id + the **absolute** workspace root and documents dir, and
  `workspace_ls` now includes the absolute `root` in its response — so the agent
  can tell the operator where a file is and can address `open_file` /
  `reveal_in_folder` without being handed the path (previously it had to `ls` and
  be told the directory). Docs: `docs/design/console-session-workspace.md`.


## [0.183.0] — 2026-07-21

### Added
- **`open_url` built-in agent action tool** — opens an http(s) URL in the
  operator's default browser on the console machine (e.g. the Teams join link
  from `outlook_create_event`, a PR, a doc). On Windows a Teams `meetup-join`
  link hands off to the Teams desktop app via its protocol handler. It is a
  confirm-gated action tool (in `_ACTION_TOOL_NAMES`, so part of the trigger
  `action_allow` vocabulary and listed by `list_action_tools`), and is
  **restricted to http(s)** (`_is_web_url`) so the agent can't be steered into
  opening a local file or a custom-scheme URI. Backed by `webbrowser.open`;
  handler `_action_open_url`. Docs: `docs/design/console-triggers.md`,
  `docs/design/trigger-actions-ux.md`.


## [0.182.0] — 2026-07-21

### Added
- **`outlook_create_event` now returns the Teams join URL** when `online_meeting`
  mints one, so the link can be reused in other channels (e.g. where the Graph
  `OnlineMeetings` scope isn't granted). Best-effort: Outlook's Teams add-in
  injects the link into the saved appointment asynchronously, so `create_event`
  re-reads the item a few times (`_read_join_url`, bounded to ~1.5s) and parses
  the URL from the body (`_extract_join_url`, matching both `.../l/meetup-join/...`
  and short `.../meet/...` forms). The result carries `join_url` (`""` when no
  link appears — add-in absent or an Outlook restart pending), and the tool output
  shows the link or says it isn't available yet rather than dropping a bare
  success line. New pure/unit-tested helpers `_extract_join_url` / `_read_join_url`
  (injectable sleep) plus a first-chance read of `join_url` in `_build_event`.
  Docs: `docs/design/outlook-agent-tools.md`.


## [0.181.0] — 2026-07-21

### Added
- **`outlook_create_event` can now attach a Teams online-meeting link** via a new
  `online_meeting` flag (mirrored on `sources.outlook.create_event` /
  `_build_event`). Outlook exposes **no** per-appointment COM API to mint a Teams
  join link — the desktop Teams Meeting add-in isn't scriptable and only the Teams
  service can create the meeting — so `online_meeting=true` instead enables
  Outlook's per-user **"add online meeting to all meetings"** setting
  (`AddOnlineMeetingToAllMeeting` under the Office version's Calendar options key
  in HKCU) before creating the event, and Outlook itself adds the link. New
  helpers `set_online_meeting_default` (public) plus the pure, unit-tested
  `_office_version_root` / `_calendar_options_key` and the Windows-only
  `_read_online_meeting_default` / `_write_online_meeting_default`. Caveats stated
  in the tool description and docstrings: it needs the Teams Meeting add-in
  installed + Teams signed in, it is a **machine-wide toggle for the signed-in
  user** (affects subsequent new meetings too, not just this one), a running
  Outlook may need a restart to pick it up, and enabling is best-effort (a
  registry failure is logged and the event is still created). Docs:
  `docs/design/outlook-agent-tools.md`.


## [0.180.0] — 2026-07-20

### Changed
- **A config seed can declare its own `[config_sync].preserve`, honoured from the
  first re-assert.** `seed_apply` now folds the preserve list the seed's own
  `config.toml` declares into the active preserve set (alongside
  `DEFAULT_PRESERVE`, the `[config_sync].last_*` pins, and the local config's
  list). Previously preserve was read only from the *local* config, so a
  seed-shipped preserve was one upgrade behind: the upgrade that introduced it
  ran against the old local config, cleared the operator's value (e.g. a
  credential `username`), then wrote the preserve — indistinguishable from
  preserve being ignored. A white-label author can now declare
  `preserve = ["runspec_credentials.toml:*.username", …]` once in the seed and it
  protects each operator's local value from the very re-assert that ships it — no
  per-machine setup. Read from the fetched seed bytes before apply
  (`bridge_fleet._seed_declared_preserve`); best-effort (a malformed seed yields
  no extra preserve and is caught again by the validate-before-write gate). Note
  a preserve still only keeps a value already present in the local file — it
  can't resurrect one an earlier upgrade already cleared. Docs:
  `docs/white-label.md` §3, `docs/console-team.md` (Config Sync).


## [0.179.0] — 2026-07-20

### Added
- **Config seed surfaces which local values it would overwrite.** The seed is the
  lowest config layer, but on a re-assert (a package upgrade, or **Re-apply
  seed**) it overlays its values on top of local for every key it declares — so an
  operator who edited a seed-shipped key (an `api_key_command`, a credential
  `username`, a host field) saw it silently revert unless it was `preserve`d.
  `seed_status()` now also returns **`overrides`** — the seeded leaf keys whose
  current *local* value differs from the seed's, each flagged `preserved` when a
  preserve rule already pins it local. Settings → Config Sync → **Config seed**
  renders them ahead of the next upgrade: a warning listing the unprotected local
  edits a re-assert would overwrite (with `local → seed` values and the hint to
  add the key to *Preserve*), plus a separate, non-alarming list of the ones a
  preserve rule already keeps. The diff is the pure, unit-tested
  `configmerge.seed_overrides` computed against each bundled file folded to its
  effective table (the custom-merged `runspec_triggers.toml` is skipped);
  read-only and best-effort — it never blocks the status. See
  `docs/design/config-seed.md` §8.


## [0.178.0] — 2026-07-20

### Changed
- **`api_key_command` now runs venv-relative, so a seeded key helper resolves
  wherever the venv lives.** A config-seed (or Config Sync) commonly ships the
  API-key helper into the same venv as the console rather than seeding a static
  `[llm] api_key` (a secret, never seeded), but the venv can be created anywhere —
  a seeded command hardcoding a path into `Scripts/`/`bin/` broke on a machine
  whose venv lived elsewhere. `api_key_command` is now executed with the current
  venv made discoverable: the venv's script dir is **prepended to `PATH`** (ship
  the helper as a console-script and seed the bare name, e.g.
  `api_key_command = "get-key"`), and **`RUNSPEC_PYTHON`** (`sys.executable`) +
  **`RUNSPEC_VENV`** (`sys.prefix`) are exported for a module helper
  (`api_key_command = '"$RUNSPEC_PYTHON" -m yourpkg.getkey'`). Because a
  venv-relative command is correct on every machine, re-assert-on-upgrade is no
  longer a hazard for this key. The anthropic and langserve adapters now share one
  `adapters/base.run_api_key_command` / `api_key_command_env` helper instead of
  each running the command with the bare inherited environment. See
  `docs/design/config-seed.md` §11a.


## [0.177.1] — 2026-07-20

### Fixed
- **Jumpless ad-hoc hosts timed out on proxied networks.** The jumpless
  connection path (0.176.0) used a raw paramiko connect, which bypassed the
  `[ssh] proxy` (HTTP CONNECT) and `~/.ssh/config` (incl. `ProxyCommand`) routing
  that every configured host — and the jump connection itself — goes through. On a
  network where the console reaches hosts via a proxy / ProxyCommand, a blank-jump
  ad-hoc browse/transfer therefore had no route and timed out; adding a jump only
  masked it. Jumpless now connects through the same `_make_ssh_client` builder the
  rest of the console uses (as the unsaved-host browser already did), so it
  inherits that routing and host-key handling. Affects relay, transfer, and the
  ad-hoc directory browser uniformly (`bridge_transfer._open_source_conn`).


## [0.177.0] — 2026-07-20

### Changed
- **Long built-in tool calls no longer look frozen.** When the chat agent runs a
  built-in tool (`repo_*`, `outlook_*`, `manage_denylist`, action/memory tools),
  a slow one — e.g. reading a large synced git repo — could leave the Console
  chat card static and the log file silent for minutes, making the app look
  broken even though good work was happening. `_run_builtin_tool_tracked` now:
  - **logs the tool lifecycle** — an INFO `started` line, an INFO
    `finished outcome=… in Nms` line on every exit path (ok / cancelled /
    error), and mirrors the tool's own progress messages to the log at **DEBUG**
    (so a chatty `repo_grep` stays out of the default log but is available under
    `--debug`); previously the `on_progress` callback only drove the UI and was
    never logged, and there were no start/finish/elapsed lines at all.
  - **emits a ~12s "still running, Ns elapsed" heartbeat** (new
    `_TOOL_HEARTBEAT_SECS`) to both the log (INFO — the default-visible
    "work is happening" signal) and the chat card (via the existing
    `runspec:tool_progress` path, so the card's status line keeps ticking with
    no frontend change). Fast tools finish before the first tick, so the common
    case adds no noise; a headless trigger/runbook turn (empty `chat_id`) still
    gets the log heartbeat even though there is no card to animate.
- **Composer gives feedback while the agent is busy.** Pressing Enter (or Send)
  during an in-flight chat turn previously did nothing silently — reinforcing
  the "app is frozen" impression. It now shows a transient
  *"Agent is working — press Stop to interrupt"* hint and leaves the typed text
  intact so it can be resent after Stop.


## [0.176.0] — 2026-07-20

### Changed
- **Transfer and Relay are now one dialog, with ad-hoc hosts on both sides.**
  The toolbar's separate Transfer and Relay buttons collapse into a single
  **Transfer files** dialog: pick a **Source** and a **Destination**, each of
  which can be **This computer**, a **configured host**, or an **ad-hoc host**
  (an SSH host not in the fleet, reached through a configured jump — the shape
  the old Relay dialog offered only as a *source*). The right engine is chosen
  on submit — one local side routes to `start_transfer` (or `start_fanout` for a
  local→many upload); two remote sides relay through the console via
  `start_relay`. This fills the gap that mattered: **download from / upload to an
  ad-hoc host** (ad-hoc ↔ local), which neither engine could do before.
  `start_transfer` now accepts an ad-hoc descriptor dict on its remote side
  (`_normalise_transfer_side`), reusing the relay source opener
  (`_open_source_conn`); the ad-hoc secret is resolved from the keyring at
  connect time and stripped before `_in_flight`/logs, exactly as relay does. An
  ad-hoc *destination* is only offered from **This computer** (relay always
  writes to a configured host); a local→local pick is disabled.
- **Agent parity — `download_file` gains an ad-hoc source.** To keep the agent
  and human capabilities in sync, the built-in `download_file` tool now takes the
  same optional `source_via` / `source_target` / `source_credential` fields
  `relay_file` already had, so the chat agent can pull a file from a non-fleet
  host via a jump straight to local. Purely additive: the required inputs stay
  `path` + `dest_dir`, and `host` still names a configured fleet host when set.
  New frontend: shared `AdHocHostFields` component (used by the merged dialog),
  `AdHocEndpoint`/`TransferEndpoint` bridge types. The Relay-specific
  `RelayModal` is removed (folded into `TransferModal`).
- **Ad-hoc hosts no longer require a jump.** An ad-hoc endpoint now needs only a
  target hostname; the **jump host is optional** — leave it blank and the console
  connects to the target directly (add a jump only when the target isn't directly
  reachable). The single connection seam (`_open_source_conn`) connects with
  `sock=None` when no jump is given, so relay, transfer, directory browsing, and
  the agent tools all get it; `download_file` / `relay_file` `source_via` is now
  optional. More intuitive for a flat network — type a host and go.

### Added
- **Rename a file on the target.** The Transfer dialog now offers an optional
  **"Rename on target"** field (shown when a single file is selected) so the
  landed file can have a different name than the source — download/upload/relay/
  fan-out all honour it. Threaded as an optional `dest_name` through
  `start_transfer` / `start_fanout` / `start_relay` and the pure planner
  (`fs_transfer._dest_items` / `_clean_dest_name`), which validates it is a bare
  filename (a path separator or `.`/`..` is rejected so the file can't escape the
  destination folder) and refuses a multi-file rename. The agent tools
  `download_file` / `relay_file` gain a matching optional `dest_name`.


## [0.175.0] — 2026-07-19

### Added
- **The chat agent can now configure the console — hosts, groups, triggers,
  and the rota.** Four new built-in tools close the "ask the agent to set it
  up" gap for config that was Settings-UI-only: `manage_hosts` (add/update/
  remove fleet entries — core keys only; room/helpdesk settings stay in the
  UI and survive updates untouched), `manage_groups` (registry add/update/
  remove/rename, delegating to the orphan-safe bridge paths), `manage_triggers`
  (save/delete/enable/disable, with the save validated by
  `triggers.Trigger.from_config` *before* writing so bad agent input is
  rejected with the human-first message instead of landing in
  `runspec_triggers.toml`), and `manage_rota` (partial-merge `set` validated
  by a new strict `triggers.validate_rota`). Read actions (`list`/`get`) run
  autonomously; every mutation confirm-gates with editable args, and the
  handlers delegate to the same public bridge methods the Settings UI calls,
  so persistence, engine restarts, and UI refresh stay single-pathed. A new
  `hosts.validate_editor_hosts` pre-flight rejects bad host input
  (`save_jump_hosts` alone coerces rather than refuses). The Triggers and
  Rota tabs refresh live on a new `runspec:triggers_updated` signal.
  Credentials and `[llm]`/theme config remain deliberately UI-only.
- **Config tools are chat-only, enforced twice.** A trigger- or
  schedule-originated agent turn can never use them — automation must not be
  able to rewrite itself (a trigger turn that could edit triggers could widen
  its own `action_allow` or mint new triggers). Layer 1: the headless turn's
  toolset strips `_CONFIG_TOOL_NAMES` unconditionally (new
  `_filter_headless_tools`, extracted from `_run_isolated_agent_turn`) —
  no `action_allow` can re-admit them, and they are absent from the
  `list_action_tools` vocabulary. Layer 2: `_run_config_tool` refuses any
  turn whose registered run origin names a trigger, covering a hand-written
  rule trigger that names a config tool as its `run` directly.

## [0.174.0] — 2026-07-18

### Changed
- **The footer room indicator is now selection-aware.** With the single-room
  selector, the footer's old "N/M rooms · K down" count was misleading: a
  deselected standby showed as **down** (with a "server unreachable" alarm), and
  because redundant hosts share the room namespace, the one live connection
  flipped **every** same-named host green ("2/2 connected" when only one was).
  The footer now reflects the **active** room's health — green when it's up, red
  (with the reachability hint scoped to that host) when it isn't, and a calm grey
  **"no room"** when nothing is selected. Non-selected hosts are labelled
  **standby**, not down. `get_room_state` now reports `connected` per **host**
  (only the active host with a live source) plus an `active` flag, so a standby
  never inherits the active host's status (`bridge_triggers.get_room_state`,
  `App.tsx` footer).

### Fixed
- **The Self-Service room selector now refreshes when host config changes.** A
  host whose room config was just removed (or added) in the Hosts editor kept
  showing in the "Connected room" dropdown — and was still selectable/connectable
  — until the app restarted, because the tab only fetched the room list on mount.
  `save_jump_hosts` now emits the `runspec:hosts_updated` signal (like the other
  host-mutating paths), and the Self-Service tab reloads its room list on both
  `runspec:hosts_updated` and `runspec:room_connection`, so the dropdown tracks
  the live host config without a restart. (The backend already read hosts fresh
  from disk each call — this was purely a stale-frontend refresh gap.)

---


## [0.173.0] — 2026-07-16

### Added
- **Single-room selector (Settings → Self-Service) for redundant helpdesk
  hosts.** When several fleet hosts each run the *same* helpdesk room (for
  redundancy), joining more than one collides on the namespace `helpdesk/<queue>`
  and only one wins non-deterministically (the old silent "duplicate room —
  skipping"). The Self-Service tab now has a **Connected room** dropdown listing
  every room-configured host; this console connects to exactly **one at a time**,
  and switching live re-joins (`restart_chat`). The choice is **machine-local**
  and **never synced** (a new `runspec_active_room.toml`, mirroring the active-
  profile pointer), so two consoles can't disagree over a shared flag and there's
  no way to have two active. Defaults are back-compatible: a lone room host
  auto-connects; 2+ hosts with no pick connect to none (logged) until one is
  chosen; an explicit "None" detaches. New pure `active_room` module
  (`read/write_active_room`, `select_room_specs`), bridge `get_room_hosts` /
  `get_active_room` / `set_active_room`, and `_active_room_specs` gating
  `start_chat`. This is the redundancy/failover model — pair a switch here with
  repointing the visitor front door at the selected host.

---


## [0.172.0] — 2026-07-16

### Added
- **`workspace_grep` gained the same search options as `repo_grep` (0.171.0) —
  case-insensitive, literal, file-glob, and a truncation signal.** The workspace
  search the agent uses to sift big spilled tool output (logs/JSON in
  `tool-results/`) was regex-only and case-sensitive with no way to scope the
  scan, so an `error`/`ERROR` sweep needed a case fold it couldn't ask for, a
  literal token containing regex metacharacters (`price[0]`, a bracketed log
  prefix) matched the wrong thing or raised `re.error`, and a broad pattern
  silently hit the result cap with no sign more existed. `workspaces.grep` now
  takes `ignore_case`, `fixed` (match literally — metacharacters escaped, immune
  to `re.error`), and `glob` (restrict the scan to files matching a shell-style
  pattern, comma-separated list allowed, e.g. `tool-results/*.json` or
  `*.log,*.txt`); the `workspace_grep` tool schema/description surface all three,
  and the payload is flagged `truncated` with a hint to narrow when the row count
  reaches the effective cap. This brings the two agent grep tools to parity so
  the model doesn't have to remember which one supports what (`workspaces.grep`,
  `bridge_agent_tools` `workspace_grep`). The `context` option (0-5 lines each
  side) that `workspace_grep` already had is unchanged.
- **Spilled JSON is now legible at a glance.** Large tool output is very often
  *minified* JSON (a runnable with `output = "json"`, an Outlook COM dump, an MCP
  result), for which the spill envelope's raw 4 KB byte prefix was a mangled,
  useless fragment of one giant line. When the spilled output parses as JSON the
  envelope now carries a compact **shape summary** instead — top-level type, an
  object's `{key: type, …}`, an array's `array[len] of {field: type, …}` (the
  union of the sampled records' fields) — so the agent knows exactly which fields
  it can grep/query without a wasted read. For a top-level JSON **array**, a
  line-per-record `.jsonl` companion is written alongside the raw file so the
  *autonomous* line tools (`workspace_grep`/`read`/`head`/`tail`) can filter or
  page records without a confirm-gated `run_python`. The raw file stays the
  verbatim record; the `.jsonl` is a best-effort derived view (`workspaces`
  `_json_shape`/`json_preview`/`_parse_json_bounded`, `spill_output`). See
  `docs/design/console-session-workspace.md` §3.1.

### Fixed
- **`workspace_read` can now page the whole of a large spilled file.** It read
  only the first ~512 KB and treated `offset` as a line index into *that* window,
  so lines past ~512 KB were unreachable and `total_lines` was wrong for bigger
  files — exactly the multi-MB spills (and new `.jsonl` companions) the agent
  needs to page. It now streams the file line by line to the requested window
  (bounded by a 64 MB per-call scan cap), reports an accurate `total_lines` plus a
  `total_lines_exact` flag, caps returned `content` at 512 KB, and normalises CRLF
  (`workspaces.read_file`).
- **`workspace_write` can no longer be tricked into overwriting a system-owned
  `tool-results/` provenance file.** The reserved-dir guard compared the raw
  first path segment, so `notes/../tool-results/x` (a `..` re-entry) and, on a
  case-insensitive filesystem, `Tool-Results/x` both slipped past. It now checks
  the *resolved* target's first segment case-insensitively (`workspaces.write_file`).
- **`workspace_tail` no longer returns empty on a huge single-line file.** For a
  file over the read cap it seeks near EOF and drops the (likely partial) first
  line — but for a minified single-line JSON dump that lone line *was* the tail,
  so the result came back empty. It now only drops the first line when more than
  one remains (`workspaces.tail`).
- **A failed spill of a large `run_python`/`run_sql` result no longer fails the
  tool call.** The exec-result spill ran outside the handler's try/except, so a
  disk-full/unwritable-dir `OSError` escaped instead of degrading; it now falls
  back to returning the payload inline, matching the runnable-output path and the
  design's "a hiccup can never fail a tool call" promise (`bridge_agent_tools`
  `_run_exec_tool`).
- **Spilled files are written atomically** (temp file + `os.replace`), so an
  interrupted/disk-full write can't leave a truncated file masquerading as a
  complete `tool-results/` record (`workspaces._atomic_write_bytes`).
- **`workspace_grep`/`read`/`tail` line numbers no longer skew on exotic control
  chars.** They used `str.splitlines()`, which also breaks on `\f`, `\x85`,
  ` `, … — a form feed inside a spilled log/JSON value invented a line and
  threw off grep line numbers relative to newline-based tools. They now split on
  `\n` only (normalising CRLF), via a shared `workspaces._split_lines`.
- **Backslash paths work in every workspace tool, not just `workspace_write`.**
  `jail` now normalises `\` → `/`, so a Windows-minded `notes\summary.md` means
  the same nested path in `read`/`grep`/`ls` as in `write` (a leading separator /
  drive letter is still refused as absolute).

### Added
- **`workspace_ls` gained `sort="modified"`** — list a directory newest-first so
  the agent can grab the most recent spill (`ls tool-results/ sort=modified`)
  without reading mtimes (`workspaces.list_dir`, `workspace_ls` schema).
- **Schedule-sourced agent turns now name their workspace `schedule-<id>`**
  (was always `trigger-<id>`), so the orphan sweep can tell a schedule run from a
  reactive trigger run — the `schedule-` prefix the design always described is now
  actually produced (`_trigger_run_agent_turn` `workspace_prefix`, passed by
  `bridge_schedules`).


## [0.171.0] — 2026-07-16

### Added
- **`repo_grep` gained case-insensitive, literal, and file-glob search plus a
  truncation signal.** The agent's repo search was regex-only and
  case-sensitive with no way to scope the scan, so a `resumeTask` lookup that
  needed a case fold or a `price[0]`-style literal (regex metacharacters) came
  back empty, and a broad pattern silently hit the result cap with no indication
  more matches existed. `grep_repo` now takes `ignore_case` (fold case), `fixed`
  (match the pattern literally — metacharacters escaped, also immune to
  `re.error`), and `glob` (restrict the scan to files matching a shell-style
  pattern, comma-separated list allowed, e.g. `*.py` or `*.ts,*.tsx` — narrower
  and faster on large trees). The `repo_grep` tool schema/description surface all
  three, and when the row count reaches the effective cap the payload is flagged
  `truncated` with a hint to narrow — so the agent knows to refine rather than
  assume it saw everything. Glob filtering is applied before the file is
  stat-ed/read, so a scoped search skips the bulk of a large tree
  (`repos.grep_repo`, `bridge_agent_tools` `repo_grep`).

### Fixed
- **A `repo_grep` pattern containing regex metacharacters no longer silently
  misfires.** It was always compiled as a regex, so a search meant literally
  (e.g. `price[0]`, `foo(bar)`) matched the wrong thing or raised `re.error`;
  pass `fixed=true` for a literal match. (A match already succeeds anywhere on a
  line, so leading whitespace/tabs were never the cause — that part of the
  earlier diagnosis was a red herring.)


## [0.170.0] — 2026-07-16

### Added
- **Every email-verify send is now audited in the console log.** The console
  mails the room's login link *as the queue mailbox* (Outlook send-as), but only
  a *failure* was logged — a send that succeeds at the COM level yet is bounced
  downstream (missing Send-As rights / NDR / junk) was silent, making "the login
  email never arrived" undiagnosable. `_handle_send_verify` now logs the outcome
  of every attempt at INFO on success / ERROR on failure, with the recipient and
  the mailbox identity it sent as, so the operator can tell whether the console
  even attempted the send and as whom (`sources/chat.py`).

### Fixed
- **Removing a room (or clearing profiles) in the host editor now sticks.** The
  editor merges the cleared form over the loaded host (`{ ...h, ...v }`), but
  cleared fields were removed with `delete` — a deleted key is *absent* from the
  patch, so the spread fell back to the host's previously-saved value and the
  room config "came back" after save/restart (only a manual `runspec_hosts.toml`
  edit removed it). Cleared fields are now set to `undefined` (which overwrites
  and is dropped on serialize) via a new pure, unit-tested `normalizeHostForm`
  (`SettingsDrawer.tsx` + `SettingsDrawer.hostform.test.ts`). Same root cause as
  the profile-tag wipe below — both are the `delete`-vs-`undefined` merge trap.
- **Saving a host in the editor no longer wipes every host's profile tags.**
  `get_jump_hosts` (the Settings → Hosts load) omitted the per-host `profiles`
  array, while `save_jump_hosts` rebuilds every *managed* key on write — so a
  round-trip through the editor dropped all profile assignments and the operator
  had to re-tag them (most visible right after startup, on the first host add).
  `get_jump_hosts` now returns `profiles` (like `group`/`room`), so the save
  round-trip preserves them. Covered by `tests/test_jump_hosts_profiles.py`.

### Changed
- **The host editor's "Queue name" field now validates the queue token.** A
  helpdesk queue name becomes the room namespace `helpdesk/<name>`, so the
  server's `valid_queue` rejects whitespace and `/`. Typing a space previously
  saved silently and then built no helpdesk spec — the room just never
  connected. The field now rejects spaces / `/` inline with a clear message
  ("use a dash, e.g. `it-support`") and the help text spells out the rule.

---


## [0.169.0] — 2026-07-16

### Changed
- **A runnable's JSON result posted into a chat room is now pretty-printed.**
  When an operator enacts a runnable for a room (`_run_assignment`), its raw
  stdout was posted verbatim — often dense one-line JSON, the "raw json" seen in
  the room. The room web app renders message bodies as plain (`pre-wrap`) text
  and has no rich renderer like the desktop console's `OutputPanel`, so the tool
  runs console-side and is now formatted console-side to match: a new pure
  `bridge_utils.format_result_for_room` pretty-prints a JSON object/array with a
  2-space indent (the same shape as the console's `<pre>` fallback) before it is
  sent. Plain text, gate-failure prefixes (`✗` / `[exit …]`), bare scalars, and
  truncated/unparseable output pass through unchanged.
- **The helpdesk queue name is now editable in the host editor (Settings →
  Hosts).** A fleet host with a `room_port` staffs a helpdesk queue whose name
  defaults to `"helpdesk"`, and the room namespace is `helpdesk/<queue>`. With
  two such hosts and no `room_queue` set, both resolved to `helpdesk/helpdesk`,
  so only the first connected and the second was silently skipped (`chat:
  duplicate room … — skipping`). The `room_queue` field was present in the host
  form but `hidden`, leaving no UI to give each room a distinct name — it is now
  a visible **Queue name** input (`SettingsDrawer.tsx`). The skip warning is also
  actionable now, naming the fix (`set a distinct room_queue on this host`).

---


## [0.168.0] — 2026-07-12

### Changed
- **A large completed run renders behind a brief "Rendering result…" placeholder
  instead of freezing the card.** After 0.167.0 bounded the streaming and table
  costs, a data-heavy result (e.g. a ~520 KB JSON array) still ran its parse +
  initial table build **synchronously in the same commit that marks the block
  done** — a couple of seconds of frozen, unlabelled UI, made more noticeable by
  the in-flight strip (which tracks the *backend process*, already finished)
  having disappeared. A completed run whose output exceeds `HEAVY_RENDER_CHARS`
  (100 KB) now paints a placeholder with a spinner + size first, yields two
  frames so it actually shows, then commits the heavy build — keeping the UI
  responsive (Stop stays clickable) and reading as "rendering…" rather than a
  hang or a flash of raw lines being replaced by the table. Small results render
  inline as before, with no placeholder flash. (`OutputPanel.tsx`
  `RenderingPlaceholder` + the deferred-ready gate on `parseOutput`.)

## [0.167.0] — 2026-07-12

### Fixed
- **A direct Console run of a runnable that returns a huge blob no longer hangs
  the card.** A runnable emitting e.g. 10k lines of pretty-printed JSON (~520 KB)
  used to freeze the Console tab for minutes with no way to stop it. Three
  compounding causes are fixed:
  - **Per-line UI churn (frontend).** Each `runspec:output` line did its own
    `setBlocks(prev => … [...b.lines, line])` — an O(n) array copy per line, so
    streaming was O(n²) plus a React render per line. Output events are now
    **coalesced and flushed once per animation frame** in `useInvocationBlocks`,
    and the run's `parseOutput` is **memoized** so a 520 KB `JSON.parse` runs once
    instead of on every re-render.
  - **Unbounded growing `<pre>` while streaming.** The raw view rendered every
    line as it arrived (the 20-line truncation only applied once the run was
    *done*). While streaming it now renders a **bounded live tail**
    (`STREAM_MAX_LINES`, 500) with a "showing the last N of M lines" note.
  - **No backstop at the source (backend).** `invoke_runnable` dispatched one
    `runspec:output` per line with no cap. It now **stops dispatching past a
    line/byte cap** and emits a single sentinel line — configurable via
    `[console] output_line_cap` (default 5,000) / `output_byte_cap` (default
    2 MB); `0` disables either limit. The runnable's own audit log is unaffected.
    (The agent tool-result path keeps its separate `[llm] tool_output_cap`.)
  - **Unbounded structured table (completion phase).** When a large result
    *parses* into a JSON table (e.g. a 10k-row array-of-objects returned as one
    compact line — so the streaming caps above don't apply), `JsonTable` mounted
    a cell per column per row all at once. It now renders a **bounded initial
    slice** (`TABLE_ROW_CAP`, 200) and reveals the rest in `TABLE_ROW_STEP`
    (2,000) chunks via a "showing N of M rows — show more" affordance (rows are
    appended, not re-rendered). Scalar cells were also made **hook-free** —
    `JsonCell` routes a primitive/scalar-array cell to plain text before any
    hook runs, delegating only nested object/array cells to the stateful
    disclosure — so a wide table of scalars no longer pays a `useState` + theme
    subscription per cell. Applies everywhere the table renders (run cards,
    `StructuredOutput` in chat/trigger transcripts, nested disclosures).
- **Run cards gained a Stop button.** A still-running `/runnable` card now shows a
  Stop button (mirroring chat turns; `bridge.cancel_invocation`) so a run can be
  cancelled from the card, not only the in-flight strip.

## [0.166.1] — 2026-07-11

### Fixed
- **Footer update check no longer stalls to "no indicator" on a slow machine or
  internal mirror.** The `pip index versions` query was capped at a hardcoded 8s;
  when it timed out the check returned "unknown" and the footer showed just the
  version, never resolving to "up to date". The timeout is now **20s** by default
  and configurable via `[console] update_check_timeout` (seconds, clamped to
  1–120). The version still renders instantly — this only bounds the background
  indicator — so a longer wait is safe. `config.update_check_timeout()`, threaded
  through `check_for_update` → `version.latest_version(timeout=…)`.
- **`run_python` gives a directive error when a model omits the code.** Some
  models (especially through a schema-loose gateway) occasionally emit a
  `run_python` call with no `code` argument. The tool now returns a
  self-describing error that names the keys it did receive and tells the agent to
  retry with `code` (or `file`) set — so it recovers in one step instead of
  repeating the empty call — and the tool description states exactly one of
  `code`/`file` is required.


## [0.166.0] — 2026-07-11

### Fixed
- **Long agent replies no longer truncate silently.** Every model call hardcoded
  a 4096 output-token ceiling, and the agentic loop treated the resulting
  `stop_reason == "max_tokens"` as a normal end-of-turn — so a reply past ~4096
  tokens was cut off mid-sentence with no marker and no continuation. The
  ceiling is now configurable via `[llm] max_tokens` (default raised to **8192**;
  Anthropic + Bedrock adapters), and when the model does hit it the loop
  **auto-continues** (`[llm] max_continuations`, default **3**) — re-prompting it
  to pick up where it left off so the answer streams out whole. If the
  continuation budget is spent, the turn ends with a visible
  `_(response was cut off at the output limit)_` marker instead of stopping
  silently. Tool-output truncation is unchanged (it was always deliberate and
  flagged).
- **The LangServe adapter now exposes the output ceiling and surfaces the
  gateway's stop reason.** For a LangServe-style corporate proxy in front of
  Bedrock/Anthropic: `[llm] max_tokens` is forwarded to the gateway (off by
  default — the published chain usually pins its own — with `max_tokens_in`
  = `kwargs`/`config`/`input` and `max_tokens_key` selecting where it lands),
  and the adapter now reads the reply's real stop reason from the message
  metadata (`stop_reason`/`stopReason`/`finish_reason`, `length`→`max_tokens`)
  instead of hardcoding `end_turn` — so the auto-continue above actually fires
  through the proxy. A custom plugin adapter that declares `max_tokens` on its
  constructor already receives it (signature-forwarded), so no change is needed
  there.
- **The adapter plugin contract now covers auto-continue.** The agentic loop
  detects an output-ceiling cutoff via `adapters.base.is_output_truncation()` —
  which accepts any provider spelling (`max_tokens`/`length`/`max_output_tokens`
  /…) — rather than one literal string, and runs tool calls whenever they're
  present (not only on an exact `stop_reason == "tool_use"`). So a custom
  provider adapter participates in continuation and tool execution regardless of
  its provider's exact stop-reason spelling. The conformance harness
  (`adapters/testing.py` `VALID_STOP_REASONS`) and the `ChatResponse.stop_reason`
  contract now document the truncation stop reasons alongside `tool_use` /
  `end_turn`.
- **Workspace tools were missing on remote fleets.** The `workspace_*` file tools
  were only offered to the agent on a local-only fleet — the remote-host branch
  of the tool list omitted them, so on a fleet with SSH hosts a spilled output
  pointed the agent at a `tool-results/…` file it had no tool to read. They are
  now offered on both paths.

### Added
- **`run_python` / `run_sql` agent tools, rooted in the session workspace.** The
  agent can now process what tools/prompts hand it — read a spilled output,
  reshape or join data, query a SQLite file — instead of doing heavy
  data-wrangling token-by-token. `run_python` runs a snippet (or a workspace
  `.py` file) with the console's own interpreter and the workspace as its cwd;
  `run_sql` runs SQL against a SQLite file in the workspace. **Trust posture:**
  `run_python` is **always confirm-gated** — it runs arbitrary code as the
  operator, so the confirm gate (not the path-jail) is the security control;
  `run_sql` runs read-only queries autonomously (opened `mode=ro`) and
  confirm-gates writes/DDL. Both apply a subprocess timeout
  (`[workspace] exec_timeout`, default 30s) and spill large output to the
  workspace like any big tool result. A locked-down deployment can drop them
  entirely with `[workspace] code_exec = false`. New module `codeexec.py`.

### Changed
- **History tab is bounded and reliable — no more spinner-then-trickle.** The
  History read used to re-glob and fully re-parse *every* audit log file on
  disk (decompressing every `.gz` archive, JSON-parsing every line) on an 8s
  interval, then keep only the newest 200 rows — O(all logs) per poll. On a
  `store="per-run"` fleet (one file per invocation) that is thousands of files
  every 8 seconds, and on remote hosts the `cat`-everything SSH scan was
  truncated by the 20s timeout, so rows arrived in dribs. Now the local read
  is **bounded newest-first with a correct early stop**: files are read newest
  by mtime, and once 200 rows are held any file whose mtime predates the
  200th-newest row's timestamp is skipped (the run_summary — the row anchor —
  is a file's last line, so `summary.ts <= mtime`; fresh per-run/single files
  are read before archives so a recently-*touched* archive with old *contents*
  can't crowd out real recent runs). A per-instance **parse cache** keyed on
  `(mtime, size)` means the periodic refresh re-parses only files that actually
  changed — immutable archives and completed per-run files are parsed once.
  Remote reads stream only the newest ~250 files (`ls -1t | head`), so the SSH
  payload is bounded and can't be truncated. Files: `bridge_reporting.py`
  (`get_history`, `_history_files`/`_read_newest`/`_parse_log_cached`/
  `_prune_history_cache`, `_get_remote_history`), `bridge_utils.py`
  (`_remote_log_script(limit=…)`, `_is_archive`, `_mtime_iso`), `bridge.py`
  (`_history_parse_cache`).
- **History refreshes on run-completion, not on a busy poll.** The tab now
  listens for `runspec:run_end` and does a single debounced silent refetch when
  a run finishes; the fallback poll dropped from 8s to 25s (the read is cheap
  now). Derived table columns, filters, and facet sets are memoised, so typing
  in the search box no longer rebuilds the whole column config. The spinner
  shows only on the first load / host switch. Files:
  `packages/console-ui/src/views/HistoryView.tsx`.

### Added
- **History → "View full log".** An expanded run row gains an opt-in *View full
  log* action that lazily opens the complete merged stream for that one run
  (via `logs_view --run <id>` — the same read the Logs tab's stream drawer
  uses, and reaching lines beyond History's inline preview / older than the row
  cap). It's fetched only on click, so History's cheap inline preview stays the
  default. Records now carry the raw `run_id` and their venv `group` to resolve
  the drill (`bridge_utils.py` parsers; `HistoryRecord` in
  `bridge/index.ts`). The stream-line renderer is shared between the Logs tab
  and this drill (`packages/console-ui/src/components/LogStream.tsx`). Files:
  `packages/console-ui/src/views/HistoryView.tsx`,
  `packages/console-ui/src/views/LogsView.tsx`.


## [0.164.1] — 2026-07-09

### Fixed
- **Relay modal — Jump host is no longer silently auto-picked.** The "Relay
  between hosts" ad-hoc mode used to default Jump host to the first fleet
  entry, so a Browse against a target whose default jump had
  `AllowTcpForwarding no` failed with a paramiko
  `ChannelException(1, 'Administratively prohibited')` before the target
  credentials were consulted — reading as a credential problem when it was
  really the wrong forwarder. Jump host now starts blank and the Browse
  button stays disabled until it's chosen. Destination host and the
  configured-mode Source host still auto-pick (those paths use the pool, not
  a direct-tcpip forward, so the bug doesn't apply). Files:
  `packages/console-ui/src/views/RelayModal.tsx`,
  `packages/console-ui/src/views/RelayModal.test.tsx`.

---

## [0.164.0] — 2026-07-09

### Added

- **Schedules — attach console credentials to a runnable-action schedule.** The
  "New schedule" modal now has a **Credentials** multi-select (runnable action),
  mirroring the Forms run dialog: a selected credential is applied when the
  schedule fires — as name-derived env vars (a credential `Jira` sets
  `JIRA_USERNAME`/`JIRA_PASSWORD`, incl. the `_USER`/`_PAT` aliases) and into any
  declared argument it binds (`username_arg` → argv, `secret_arg` → the env
  channel). **Only the credential ids are stored** in `runspec_schedules.toml` —
  the secret never leaves the OS keychain and is injected at fire time, so a
  scheduled `runspec-jsm`/`runspec-snow`/etc. run gets its credentials without
  the secret ever touching disk. This stays entirely local (the console's own
  process, its own SSH, its own keychain) — the same trust model as Forms and
  reactive Triggers, unrelated to the 0.163.0 shared-daemon delegation that was
  removed. New `credential_ids` field on `ScheduleJob` + `[[schedule]]`, threaded
  through every fire path (`ScheduleSource._fire` → `_run_scheduled` →
  `_invoke_scheduled` incl. the group / any-of-hosts / selected-hosts fan-out, and
  `run_schedule_now`) to `invoke_runnable(credential_ids=…)`. A schedule with **no**
  credentials still passes `credential_ids=None`, preserving the prior scope-based
  auto-apply behaviour. **Secrets are never persisted:** a `password`-typed arg is
  no longer written into the schedule's stored `args` (a plaintext secret on disk)
  and isn't required at save — supply it via a credential (or the runnable's own
  env resolution) at fire time. Files: `runspec_console/sources/schedule.py`,
  `runspec_console/bridge_schedules.py`, `packages/console-ui/src/views/SchedulesView.tsx`,
  `packages/console-ui/src/bridge/index.ts` (`Schedule.credential_ids`).

### Changed

- **Schedules — "New schedule" modal args now render with the rich Forms controls.**
  Previously every runnable arg / runbook parameter fell back to a plain text
  input (so a Boolean `flag` arg was typed as `"true"`/`"false"`, a `choice` with
  no options rendered as free text, and there was no picker for `int`/`float`,
  `path`, `date`/`datetime`/`time`/`month`/`color`, or a masked `password`). The
  modal now reuses the Forms tab's `ArgInput` verbatim so the same rich controls
  appear in both places: `flag` → Switch, `choice` → Select, `int`/`float` →
  InputNumber (with range/slider when declared), `path` → browsable PathInput,
  `date`/`datetime`/`time`/`month`/`color` → the matching picker, `password` →
  masked field; runbook values reuse `RunbookField` (which adds the `ui = "host"`
  fleet dropdown). Layout matches Forms too — args with a default fold into a
  collapsed "Default arguments" section near the top; required/optional-no-default
  args lead the form. Values are now stored as native types (a flag is written as
  `flag = true`, not `flag = "true"`), which the executor already handles. Edit
  prefill goes through the same `initialFormValues` helper Forms uses. Front-line
  validation (required + numeric range) mirrors the Forms run modal. Files:
  `packages/console-ui/src/views/SchedulesView.tsx`,
  `packages/console-ui/src/views/RunbookRunModal.tsx` (exports `RunbookField`).

## [0.163.0] — 2026-07-08

### Removed

- **Schedules — host-delegation (the "Run from → Host's scheduler" feature added
  in 0.162.0) is removed.** On reflection it didn't fit the console's single-user
  model, and — the deciding factor — routing an *operator's* credentials onto a
  shared host for a shared always-on daemon is a trust risk we don't want in the
  console: one mistake or one bad actor could taint corporate IT's trust in the
  console for everyone. Credentials now stay sealed in the operator's own keychain
  and are never shipped anywhere by the console. Reverts the `runner`/`status`/
  `credential_ids` schedule fields, the remote-schedule read/CRUD bridge helpers +
  `_remote_sched_cache` refresh hook, and the modal's "Run from" toggle / credential
  picker / Runner column — the Schedules tab is back to purely local (console-fired)
  schedules. The **`runspec-scheduler`** package remains on PyPI as the basis for a
  future *standalone, shared* scheduler with its own governed credential store (see
  `docs/design/venv-scheduler.md`).

## [0.162.0] — 2026-07-08

### Added

- **Schedules — delegate a schedule to a host's always-on scheduler.** The
  new-schedule modal (*Run a runnable* → **One host**) gains a **"Run from"**
  toggle: **This console** (the operator's own, user-specific schedule, fired by
  the console while it's open — unchanged) or **the host's scheduler** (a
  **shared, always-on** schedule delegated to the target host's
  [`runspec-scheduler`](../runspec-scheduler) daemon, which runs whether or not
  any console is connected). The host option is enabled only when the selected
  host has a `runspec-scheduler` in the **same venv** as the target runnable.
  For these **remote schedules the host is the source of truth**: the console
  reads them live via `runspec-scheduler list` each refresh cycle and CRUDs
  against the host by invoking the scheduler's own `add`/`remove`/`run-now`
  verbs over the existing SSH executor (so subcommands + credential injection
  come for free); they are never written to this operator's user-specific
  `runspec_schedules.toml`. The Schedules table gains a **Runner** column
  badging each row **Console** vs **Remote** (with a live ok/unreachable status
  dot). For a `run_as` runnable, a credential multi-select (reusing the Forms
  env-var derivation) attaches service-account credentials, written to a
  protected `RUNSPEC_ENV_FILE` on the host owned by the `run_as` account
  (only the derived variable **names** travel as `--cred-vars`; the values ride
  the executor's existing secret channel). New private bridge helpers
  (`_remote_schedules`/`_remote_create`/`_remote_delete`/`_scheduler_targets`/…)
  and a `_remote_sched_cache`; the frozen JS API surface is unchanged. See
  `docs/design/venv-scheduler.md`.

## [0.161.0] — 2026-07-07

### Added

- **Schedules — new "Selected hosts" run target.** The Schedules-tab "New
  schedule" modal's *Run a runnable* target picker gains a fourth option:
  **Selected hosts**. The operator ticks a set of hosts from a multi-select and
  the runnable runs on **every** ticked host (fan-out, one run card per host —
  same lifecycle as a host-group fire), skipping any ticked host that doesn't
  carry the runnable. It complements the existing three modes and closes the
  gap for an ad-hoc multi-host run that doesn't warrant registering a group in
  `runspec_groups.toml`: **One host** (single host), **Any of hosts** (candidate
  list, first connected — failover), **Selected hosts** (explicit list, all of
  them — fan-out), **Host group** (registered group members). Wired end-to-end:
  a new `selected_hosts: list[str]` field on `ScheduleJob` and the
  `runspec_schedules.toml` schedule entry (parsed by `parse_schedule_entry`
  with the mutex extended to XOR across `host`/`group`/`hosts`/`selected_hosts`);
  a new branch in `_invoke_scheduled` that mirrors the group fan-out
  (per-host `_host_has_runnable` filter → recursive per-member invoke); the
  `create_schedule` autonomous agent-tool schema gains a `selected_hosts` array
  property (host-name enum when available) so the chat agent can create the
  same schedules from natural language; the Schedules event-log target label
  reads `selected:h1,h2,…`. Fully additive — existing `host`/`group`/`hosts`
  schedules fire unchanged.


## [0.160.1] — 2026-07-07

### Changed

- **Richer debug logging around chat-room connect / subscribe / disconnect /
  reconnect.** The room connection lifecycle was nearly silent — only the catalog
  publish logged (`"chat: published N tool(s)"`), so a dropped-and-reconnected
  room showed nothing but a repeated catalog line while the server logged only
  `user subscribed` / `user unsubscribed`. Now: a successful connect logs at
  **INFO** (`"chat: connected to <room> on host <host> as <user>"`), a genuine
  drop after a healthy connection logs at **INFO**
  (`"chat: <room> dropped after Ns up — reconnecting in Ns"`), and the
  supervisor start/stop log a one-line summary. Under `--debug`: the hello (which
  *is* the subscribe), presence, advertise, and catalog **sends** each log; the
  tunnel/exec channel open logs; and — the key diagnostic — the transport now
  distinguishes a **clean EOF** (peer/room server closed the channel) from a
  **channel error** (SSH transport died mid-stream) at the byte level, so "why
  did it drop" is answerable from the log. No behavior change beyond logging;
  the flapping-detection dedup is preserved.


## [0.160.0] — 2026-07-07

### Added

- **Per-session workspace sandbox for large tool outputs** — every chat
  conversation (and every trigger/schedule agent turn) now owns a private folder
  under `{app_dir}/workspaces/{session_id}/`. When a runnable's stdout exceeds
  the spill threshold (`[workspace] spill_threshold`, default 32 KB) the raw
  bytes are written to `tool-results/{tool}-{inv_id}.{json|txt}` in that
  workspace instead of inlined; the model receives a short envelope with a head
  preview and the file's workspace-relative path. Six new autonomous agent
  tools (`workspace_ls` / `workspace_read` / `workspace_grep` / `workspace_head`
  / `workspace_tail` / `workspace_write`) let the agent interrogate a spilled
  output at its own pace — read a page, grep for the interesting rows, tail the
  end, save a distilled note under `notes/` — without ever pulling the raw
  bytes into model context. The whole point: **stop needing to hand-shrink
  every runnable's output** just to fit the tool-result budget, and unblock
  third-party tools (vendor CLIs, MCP tools, Outlook dumps) whose output shape
  we don't control. Machine-local (never synced, same class as
  `conversations/`); chat workspaces are pruned alongside their conversation
  via `[chat_history]` retention, headless (trigger/schedule) workspaces via
  the new `[workspace] headless_retention_days` (default 7). Startup runs a
  cheap orphan sweep. All workspace tools are strictly path-jailed to the
  current session's directory. See `docs/design/console-session-workspace.md`.


## [0.159.1] — 2026-07-02
### Fixed

- **Outlook watch folders no longer replay the whole inbox as `new_mail` on every
  startup.** The poll fallback (which catches bulk moves `ItemAdd` misses) filtered
  with `Items.Restrict("[ReceivedTime] > '<stamp>'")`, formatting `<stamp>` as a
  hardcoded US `MM/DD/YYYY`. Outlook re-parses that string under the **OS regional
  settings**, so on a non-US-date locale (e.g. en-GB, `DD/MM/YYYY`) `07/02/2026`
  was read as **7 Feb**, not 2 Jul — pushing the high-water mark months into the
  past and re-emitting every message in the folder as `new_mail` on each launch,
  worst on a large personal "also watch" Inbox. The poll now sorts newest-first and
  compares each item's own `ReceivedTime` in Python (breaking at the first item at
  or before the mark), so it's locale-independent and touches only the recent tail.
- **Settings → Mailbox: "Suppress replies by default (once per thread)" now
  persists.** The `Form.Item` wrapped two children (the `<Switch>` and a
  description `<span>`), so Ant Design never bound the switch to the field — the
  value was saved as `false` every time and never restored on reopen. The switch is
  now the Form.Item's single child, with the description moved to the item label.
- **`[[watch]]` is written as a proper table-array block instead of an inline array
  floated to the top of `config.toml`.** `tomli_w` renders an array-of-tables inline
  (`watch = [ { … } ]`); being a bare top-level key, TOML forces it above every
  `[section]`, so it sat at the very top (empty entries as a bare `{}`) looking out
  of place. `write_config` now emits top-level arrays-of-tables as idiomatic
  `[[watch]]` blocks after the sections. Purely cosmetic — the on-disk value
  re-parses identically.


## [0.159.0] — 2026-07-02

### Added

- **Forms tab: time, month, colour, and slider controls.** More `ui` hints render
  matching rich controls (extending the `date`/`datetime` pickers):
  - `ui = "time"` on a `str` arg → an Ant `TimePicker` (hour/minute/second);
    value stored as an `HH:MM:SS` string.
  - `ui = "month"` on a `str` arg → an Ant month `DatePicker`; value stored as a
    `YYYY-MM` string.
  - `ui = "color"` on a `str` arg → an Ant `ColorPicker` with a hex readout; value
    stored as a `#rrggbb` string.
  - `ui = "slider"` on an `int`/`float` arg with a `range` → an Ant `Slider` paired
    with a number box (this finally wires the long-listed `slider` enum value);
    falls back to the number box when no range is declared.
  The arg's type badge reads `time`/`color` where set. (`FormsView.tsx` `ArgInput`.)

## [0.158.1] — 2026-07-02

### Fixed

- **Mailbox settings no longer freeze the app on save (Windows).** Saving in
  Settings → Mailbox calls `restart_triggers` (`stop_triggers` then an immediate
  `start_triggers`), and because a `[mailbox]` is configured the trigger engine
  force-starts the Outlook COM source. `OutlookSource.stop()` previously only
  *signalled* its pump/dispatch threads and orphaned the references without
  waiting for them to exit, so the restart spun up a **second** STA COM pump that
  bound `WithEvents` sinks to the singleton `Outlook.Application` while the old
  pump was still bound. Overlapping STA pumps on one Outlook instance deadlock on
  Windows, hanging the console ("not responding") until it was killed — and the
  single **Save** button made it worse by restarting twice (`save_mailbox` then
  `save_watches`). `stop()` now **joins** both threads (bounded by
  `_STOP_JOIN_TIMEOUT`, logging if a pump is wedged in a COM call) so the old
  apartment fully unwinds — including `CoUninitialize` — before a new pump starts,
  serialising teardown → rebuild.


## [0.158.0] — 2026-07-02

### Changed

- **Settings → LLM: the Model field is now a dropdown.** The previously
  freeform *Model* text input is a strict selector chosen from a per-console
  list of **Available models** — a new tag-style editor in the same tab where
  you maintain the model ids you use. The list persists as a new `[llm] models`
  array in `config.toml` and rides Config Sync like any other config key; the
  active `[llm] model` must be one of them. An existing hand-edited `model`
  that isn't in the list is kept selectable (prepended on load), and switching
  provider seeds the list with that provider's default. Frontend-only — the
  `models` key is inert to the model adapters (never forwarded to a client).


## [0.157.0] — 2026-06-30

### Added

- **Native Windows title-bar tint — `[console.theme] title_bar_color`.** A new
  white-label theme key tints the Windows 11 caption (via DWM `CAPTION_COLOR` /
  `TEXT_COLOR`). It is an **independent** hex, deliberately *not* derived from
  `accent_color`, so a brand can pair e.g. a blue accent UI with a red title bar.
  Unset → the existing default runspec-blue caption (no change to default
  installs). The (caption, text) COLORREFs are computed by the new pure
  `config.title_bar_colors` (the caption-text colour is picked black or white for
  contrast by luminance), consumed by `app.py` `_apply_dwm_title_bar`. Windows 11
  build 22000+ only; a no-op on other platforms/builds. See
  `docs/design/console-theme.md`.

## [0.156.0] — 2026-06-30

### Added

- **Bundle the white-label app icon inside the package — `[console.theme] icon =
  "pkg:<module>:<relpath>"`.** The `icon` override now accepts a package
  reference (e.g. `pkg:mycorp_console:app.ico` or
  `pkg:mycorp_console:assets/app.ico`) in addition to a filesystem path: the icon
  shipped *inside* a package is resolved via `importlib.resources` and copied
  into app-data (keyed by content hash) so pywebview always gets a stable real
  path — even from a zip-imported wheel. So a white-label distribution can ship
  its own icon with **no machine-specific absolute path** to hard-code (mirroring
  how the config seed already rides in the package). A plain path still works;
  a bad path / module / resource falls back to the generated accent square.
  (`app.py` `_resolve_icon_override`.) See `docs/design/console-theme.md`.

## [0.155.1] — 2026-06-30

### Fixed

- **Generated app icon rendered with a dark-grey artifact.** The recoloured
  fallback icon (a solid square in `accent_color`) embedded a **24-bit RGB** PNG
  while the ICO directory entry declared **32bpp** — so Windows expected an alpha
  channel, found none, and invented one, painting the square with a dark-grey
  cast ("red and dark grey"). It now emits a true **opaque 32-bit RGBA** PNG
  (colour type 6), so the square renders clean. The cache filename gained a
  `-rgba` suffix to force a regenerate over any stale pre-fix icon. (`app.py`
  `_build_icon`.) Note the generated icon is, by design, a solid square in your
  brand colour — for a **real logo** set `[console.theme] icon` to an `.ico`/`.png`
  path, which overrides the generated square entirely.

## [0.155.0] — 2026-06-30

### Added

- **Watermark styles (`[console.theme] watermark_style`).** The white-label
  content watermark is no longer only the tiled/slanted Ant mark. A new
  `watermark_style` key chooses how it's drawn: **`tiled`** (the current repeated
  mark, default — existing configs unchanged), **`banner`** (a single large word
  centered over the content area, upper third — a clean "brand banner" look), or
  **`center`** (a single large word dead-center). All three honour a
  `watermark_logo` image instead of text, are faint + theme-aware + non-interactive,
  and remain a passthrough when no watermark is set. Validated server-side
  (`config.valid_watermark_style`, surfaced via `brand_theme`); rendered by a new
  `App.tsx` `BrandWatermark` component. See `docs/design/console-theme.md`.

## [0.154.1] — 2026-06-30

### Fixed

- **Version footer went blank on a white-label install.** `app_version()`
  returned `""` when the **branded** package's version couldn't be read via
  `importlib.metadata` (a renamed/editable install, or a provider that can't be
  traced to a distribution) — which, combined with an update check that can't
  reach a private index, left the bottom-left footer completely empty. It now
  falls back to runspec-console's own version (and survives a branding-resolution
  error), so the footer **never goes blank**. The branded version still shows
  whenever it resolves. (`bridge_config_ssh.py` `app_version`.)
- **Startup race with pywebview 6.x dropped bridge calls (incl. the white-label
  theme).** pywebview 6.x injects `window.pywebview.api` and then attaches its
  methods **asynchronously**, so a mount-time bridge call (`brand_theme`,
  `list_action_tools`, …) could land **before that method was attached yet** and
  fail with an opaque `(intermediate value)[t] is not a function`. The failures
  clustered at startup and varied in count run-to-run — a race, not a missing
  method (the methods exist in Python). For `brand_theme` the failure was
  swallowed, so the theme silently never applied. The bridge proxy now reads the
  **live** api and **briefly retries** while a method is still being attached, so
  mount-time calls succeed once pywebview finishes wiring up. A method still
  absent after the api settles raises a **named** error (a genuine version
  mismatch) instead of the minified mystery, and every uncaught bridge error is
  now **forwarded into the Python log file** via a new best-effort
  `log_client_error` method — so `runspec-console.log` captures frontend failures
  instead of leaving them trapped in the web view. (`bridge/index.ts`
  `resolveMethod` + `bridge_infra.py` `log_client_error`.)

## [0.154.0] — 2026-06-30

### Added

- **Context-window indicator** (`docs/design/console-context-indicator.md`, Phase
  1). A footer gauge shows the active chat's latest-turn context fill —
  `48.2k / 200k · 24%` with a green/amber/red dot at 60/85 % of the model's
  window — and a hover popover breaks the prompt down by source (System base /
  Console context / Memory / Tool definitions / Catalog / History, largest first)
  with cache-hit %. The headline total is exact (from the model's reported usage);
  the per-section split is a char-proportioned estimate that always sums to it. The
  row is hidden until a chat reports usage and resets when the chat is cleared.
  New backend event `runspec:context_estimate`; pure helpers
  `estimate_context_buckets` / `model_context_window` in `bridge_utils.py`.
- **Tool-result truncation is now visible.** When a runnable's output is cut at
  the per-call cap before the model sees it, the tool card shows a red
  **⚠ output cut** badge (with the original → cap sizes) instead of the cut being
  silent — previously the marker lived past the 2 KB the UI received. A
  non-truncated but large result (> 50 KB) gets a dim size chip, since it stays in
  the conversation context for the rest of the chat. New `output_chars` /
  `truncated` fields on `runspec:tool_end`.
- **`[llm] tool_output_cap`** makes the per-tool-output truncation cap
  configurable (default unchanged at 1 MB — kept so data-heavy runnables emitting
  one big JSON line aren't sliced mid-structure; lower it per-deployment to force a
  chatty runnable to fail loudly and early). `bridge_autonomy.py` truncation site
  now uses a parseable marker so the badge can read it.
- **Context-management levers in the gauge hover** (Phase 2): a **search-mode
  nudge** when tool/catalog definitions are ≥ 30 % of the window (opens Settings);
  a **Compact now** action once the live history reaches ~24 messages —
  `compact_chat` condenses the older prefix into a note and keeps the recent tail
  (the same safe transform as resume-time compaction), freeing context without
  losing the thread or clearing the chat; and a **pinned-memory hint** showing the
  pin count and flagging when more facts are pinned than fold into the prompt. New
  `compact_chat` JS API method; `runspec:context_estimate` now also carries
  `tool_mode` / `history_msgs` / `memory_pins`.


## [0.153.1] — 2026-06-29

### Fixed

- **Update check pinned on "checking…" forever** when the package index is
  unreachable (offline / a private index / a blocked network policy). The footer
  only resolved on a successful check; now any outcome clears the spinner — a
  failed/timed-out check (the backend already caps the pip call at 8s) shows just
  the version with no update tag, and "up to date" is shown only when the latest
  version was actually confirmed. (`App.tsx`.)
- **Watermark invisible on the dark UI.** Ant `<Watermark>`'s default font colour
  is `rgba(0,0,0,.15)` — invisible on the console's dark background. The
  white-label content watermark now uses a theme-aware colour so it shows in both
  light and dark. (`App.tsx`.)
- **White-label `accent_color` now drives the app's signature accent**, not just
  Ant's `colorPrimary`. The visible accent — sidebar title, active nav tab,
  section/room highlights — was a hardcoded blue (`titleCol`), so a brand colour
  only tinted Ant controls (buttons/switches) and looked like it "wasn't working".
  `accent_color` now feeds `titleCol` too (falling back to the built-in blue when
  unset), so the brand colour shows where it's actually visible. (`App.tsx`.)
- **Forms "Default arguments" collapsed too much.** `partitionArgs` folded *every*
  non-required arg into the collapsed section, including optional args with **no
  default** — which then hid inputs the operator might need to type under a
  "Default arguments" heading. Only args that actually carry a default (incl. a
  flag's `false` and a `$VAR`-ref default) collapse now; required and
  optional-no-default args stay visible. (`FormsView.tsx` `partitionArgs`.)

## [0.153.0] — 2026-06-29

### Added

- **White-label theme rendering (`[console.theme]`).** The package config seed
  (0.152.0) could *deliver* theme values; now the console *renders* them — four
  surfaces, all **read-only from config** (set by a seed or by hand; no Settings
  editor in v1):
  - **Accent colour** — `accent_color` (a hex colour) drives Ant Design's
    `colorPrimary` (buttons, links, active tabs, switches) in both light/dark,
    **and** recolours the generated app icon, so the white-label "blue square"
    becomes the brand colour with no file to ship.
  - **Content watermark** — `watermark_text` / `watermark_logo` render a faint
    tiled brand mark behind the content area (Ant `<Watermark>`); unset → nothing.
  - **Sidebar brand name** — `brand_name` replaces the sidebar title; unset keeps
    the literal `runspec`, so default installs are pixel-identical.
  - **App icon** — an explicit `icon` path (a readable `.ico`/`.png`) overrides
    the generated icon outright; otherwise the square is painted in `accent_color`.
    The icon cache is keyed by colour so a changed accent regenerates it.
  - New read-only bridge method `brand_theme()` (validated — a non-hex colour or a
    non-`http(s)`/`data:image` logo is dropped); pure `config.valid_accent` /
    `valid_image_ref` / `theme_config` shared by the bridge and the icon builder.
    See `docs/design/console-theme.md`.

## [0.152.0] — 2026-06-29

### Added

- **Package config seed — bundle a baseline config inside the deployment package.**
  A white-label / corporate distribution that wraps the console can ship a baseline
  config as package data, applied on launch so a fresh or **air-gapped** install is
  configured with **no git host and no PyPI index reachable at run time** — the
  floor for a disaster-recovery scenario. It's built on the existing Config Sync
  merge engine (the seed is a new *source* feeding the same fold/merge pipeline a
  git pull uses), and is the **lowest** config layer: a git pull above and the
  operator's own edits always win, with `preserve` keys pinned local.
  - **Discovery (`configseed.py`).** A contributor declares an entry point in group
    `runspec_console.config_seed` pointing at a package that holds a `config_seed/`
    data directory of config TOMLs (filenames in Config Sync's `KNOWN_FILES`).
    `discover()` collects them into the `{name: bytes}` + `{dist: version}` shape
    the apply pipeline consumes, folding multiple contributors low→high by sorted
    distribution name. Best-effort — a broken contributor is skipped, never fatal.
  - **Re-assert on upgrade.** `Bridge.seed_apply()` runs on bring-up and compares
    the bundled `{dist: version}` stamp against a **machine-local**
    `runspec_seed_state.toml` (never synced); equal ⇒ a pure no-op, changed ⇒ the
    seed re-folds onto local config so a **package upgrade rolls the baseline
    forward**. A seed may carry `[config_sync]` to bootstrap the live git layer;
    the machine-local `last_*` run-state is preserved.
  - **Secrets are never seeded** — the package delivers metadata and structure
    only (same contract as Config Sync); secret bootstrap stays in the keyring.
  - **Settings → Config Sync → Config seed.** A read-only status line ("Bundled:
    `<dist> v<ver>`", last-applied time, an "update pending" hint) and a **Re-apply
    seed** button (`seed_apply(force=true)`). New bridge methods `seed_status` /
    `seed_apply`.
  - Internals: `_apply_config_layers` was extracted from `sync_pull` as the shared,
    source-agnostic apply half (validate → merge → hot-reload). See
    `docs/design/config-seed.md`.

## [0.151.0] — 2026-06-29

### Changed

- **Forms tab: optional args collapse, date pickers, auto-growing strings.** Three
  refinements so a run form leads with what the operator must fill:
  - **"Default arguments" collapse.** Args with a default (i.e. non-required /
    optional) now fold into a section that's **collapsed by default**, near the
    top of each group; the required args stay always-visible below it. For a
    runnable with subcommands the collapse applies *within* each section — the
    **Inherited** section gets its own "Default arguments" sub-collapse and the
    **This command** section gets its own — so inherited globals and the command's
    own optional args each tuck away independently.
  - **Date pickers.** A `str` arg that declares `ui = "date"` or `ui = "datetime"`
    (the new core format hint) renders an Ant `DatePicker` (with time for
    `datetime`); the value is written back as an ISO string, so the run contract is
    unchanged. The arg's type badge reads `date`/`datetime`.
  - **Auto-growing string fields.** Plain string inputs are now auto-sizing
    textareas — **Shift+Enter inserts a newline** (and the field grows), while a
    bare Enter is suppressed (no stray newline). Password fields stay single-line
    and masked. (Runbook forms inherit all three via the shared `ArgInput`.)
  - `console-ui/src/views/FormsView.tsx` (new `partitionArgs` / `suppressPlainEnter`
    helpers, `DefaultArgsCollapse`); `dayjs` added as a direct dependency.
- **Catalog passes the `ui` hint to the room.** `arg_to_property` now emits the
  form-control hint as `x-ui` on each generated JSON-Schema property, so the
  runspec-room web form can render the matching date picker. (`catalog.py`.)

## [0.150.1] — 2026-06-29

### Fixed
- **History "Edit & rerun" is now always offered, not gated on recorded args.**
  Password-typed args are deliberately never written to the run-summary audit
  record (core `parser.py` excludes `type == "password"`), so a runnable whose
  required arg is a secret has an **empty `args`** in its History record. The
  `hasArgs` gate then hid the **Edit & rerun** button, leaving only **Rerun** —
  which re-invoked without the secret and failed with *"missing required
  argument"*, with no way to supply it. The edit button is now always shown so
  the operator can open the rich run form and type the secret (and any other
  args). The Console card was unaffected — its rerun data is captured in the
  browser at invoke time, so it still carries the value.

## [0.150.0] — 2026-06-29

### Added
- **Host profiles — a switchable, scoped working-set of hosts.** A profile is a
  named set of remote hosts; you load one at a time and only its hosts are probed
  and hold standing SSH connections — like rebooting the console into a different
  host-scope. Built for operators who wear several hats (app-support *and* DevOps
  *and* another region) and don't want connections to the whole fleet, all the
  time. A new **profile switcher** in the left sidebar (shown once any profile
  exists) flips between **All hosts** (the default = today's behavior) and each
  profile; switching tears down the leaving hosts' pooled transports and re-probes
  the new scope. Settings → Jump Hosts gains a per-host **Profiles** multi-select
  and a **Manage profiles…** dialog (mirrors Manage groups).
  - **Scope is interactive-only** (deliberate): profiles scope the sidebar,
    discovery, and standing connections, but schedules/triggers/explicit runs
    still reach any configured host on demand — automation spans profiles, so a
    DevOps schedule still fires while you're in the App-Support profile.
  - **Git-syncable:** profile *definitions* sync — membership is a `profiles`
    array on each host in `runspec_hosts.toml`, and a new `runspec_profiles.toml`
    name registry rides Config Sync (`KNOWN_FILES` / `COLLECTION_FILES`, merged by
    `name`) like the group registry. The *active* profile is **machine-local**
    (`runspec_active_profile.toml`, never synced) — which one you've loaded is
    personal, moment-to-moment state.
  - Bridge: `get_profiles` / `save_profiles` (self-healing + delete reconciliation)
    / `rename_profile` (re-points membership) / `get_active_profile` /
    `set_active_profile` (the switch). Profiles vs groups: a group is a *targeting
    unit* (one per host, fan-out); a profile is a *working-set* (many per host,
    switchable). See `docs/design/host-profiles.md`.

### Profiles are orthogonal to groups
- Groups still organize the sidebar and target fan-out; profiles scope which
  hosts are live. A host can belong to several profiles but one group.


## [0.149.0] — 2026-06-29

### Added
- **Copy SSH public key to all hosts at once.** A new **Copy key to all hosts…**
  button in Settings → SSH (the SSH key section, beside Generate / Rotate) opens
  a dialog that authorises this machine on every selected remote host over a
  one-off password connection — the bulk equivalent of the per-host *Copy SSH
  public key* action (left sidebar right-click, Jump Hosts row icon). Built for
  initial fleet bootstrap: when hosts arrive from a Config-Sync pull none of them
  trust our key yet, so they all show disconnected and each would otherwise need
  the single-host dialog one at a time. The disconnected hosts are pre-selected
  (the ones still missing the key); one password is tried against every target
  (the common shared-bootstrap-credential case) and per-host results report which
  succeeded so the operator can retry the rest individually. The password is used
  only for these connections — never stored, cached, or logged. New bridge method
  `copy_ssh_id_all(password, targets=None)` (omit `targets` ⇒ every remote host
  not already connected), reusing the same idempotent `grep -qF || append` push
  and `[ssh]` proxy / host-key handling as `copy_ssh_id`; the single-host push is
  refactored into a shared `_push_key_over_password` helper. UI: `CopyKeyAllModal`.


## [0.148.0] — 2026-06-28

### Changed
- **Edit-and-rerun now opens the Forms run dialog, pre-filled.** The pencil
  ("Edit args and rerun") on a Console transcript card and the **Edit & rerun**
  button on a History row previously dropped an *inline plain-text editor* that
  only listed the args that happened to be recorded — no selectors, switches,
  range hints, or path pickers, and no way to set an arg the original run left at
  its default. Both now route to the **Forms** tab and open that runnable's run
  dialog with its full, rich, typed inputs (`choice` → Select, `flag` → Switch,
  `path` → browsable picker, etc.) **pre-filled with the previous run's values**
  — every available arg is shown for editing, not just the ones that were set.
  The plain "Rerun with same args" action is unchanged. Wiring: cards/rows
  dispatch a `runspec:edit_in_form` event; `App` resolves the discovered runnable
  (warning if its host is no longer connected) and opens the form via a new
  `prefill` field on `PendingForm`. The defaults+prefill merge is the exported,
  unit-tested `initialFormValues` helper in `FormsView.tsx` — declared-arg
  filtered, and a `password` arg is never pre-filled (secrets aren't recorded).

## [0.147.0] — 2026-06-28

### Added
- **`console_status` now reports the current wall-clock time.** The per-turn
  system prompt carries only the **date** (kept date-only so the model's prompt
  cache stays warm — a live clock would miss the cache every turn), so the agent
  had no way to know the exact time, only whether it was inside/outside the rota
  window. The on-demand, autonomous `console_status` tool now leads with the
  **current local date and time** (`Weekday, YYYY-MM-DD HH:MM TZ`), and its
  description tells the agent to call it when it needs the precise time — e.g.
  before scheduling or running something time-sensitive. Zero prompt-cache cost
  (it's a tool result, not part of the cached prefix). `bridge_agent_tools.py`
  `_run_status_tool`/`_status_tools`.

## [0.146.1] — 2026-06-28

### Fixed
- **HTTPS git auth (Config Sync *and* Repos sync) with a token-only credential
  sent no `Authorization` header at all — the server replied 401.** dulwich
  attaches HTTP basic auth only when a username is present (it keys on
  `username is not None`), but a **"Token / API key"** credential — the natural
  choice for a GitLab/GitHub **personal access token** — carries no username, so
  `_transport_kwargs` passed `username=None` and the token was never sent. When a
  secret is present but the username is blank, the transport now defaults the
  username to `oauth2` (accepted by GitLab and GitHub PAT auth) so the token rides
  as the password. A `userpass` credential keeps its real username untouched; a
  public/anonymous repo (no secret) still sends nothing. This unblocks using a
  user-level PAT — which covers **every repo you can clone**, unlike a per-project
  deploy token. For a provider that needs a specific basic-auth username (e.g.
  Bitbucket app passwords), set the username on the credential as before.


## [0.146.0] — 2026-06-28

### Added
- **Chat history — reopen and continue past conversations.** The agent chat was
  in-memory only, so closing or "New chat" lost everything. Conversations now
  **auto-save** to a machine-local store and the Console gains a **History**
  button: browse past chats, reopen one (its full transcript renders), and just
  keep typing to **continue** it (the model history is rebuilt from the
  transcript). Rename or delete from the list.
  - **Storage** is machine-local JSON, one `{id}.json` per conversation under
    `conversations_dir()` (`app_dir()/conversations`) — **never synced**
    (transcripts can be sensitive). A turn is stored as a provider-agnostic
    transcript (user text + assistant prose + tool name/input/output + usage),
    not the adapter-native history (which keeps raw SDK objects that don't
    serialise) — so it renders and resumes across providers. New module
    `conversations.py`; bridge `list_conversations`/`load_conversation`/
    `resume_conversation`/`delete_conversation`/`rename_conversation`; each
    completed chat turn is appended in `_agentic_chat_turn`; `clear_chat` starts
    a fresh conversation.
  - **On by default, with retention.** A `[chat_history]` config block
    (Settings → Chat History): `enabled` (default true), `max_conversations`
    (200) and `max_age_days` (90), pruned on each save. Disabling stops new
    writes.
  - **Resume fidelity:** the rebuilt history is plain user/assistant text (tool
    calls folded into the assistant text as short notes), robust across
    providers — exact provider-native tool_use/tool_result pairs are not
    reconstructed (a possible future enhancement; the API reports `exact:false`).
  - **Resume-time compaction:** reopening a long conversation keeps the opening
    `resume_keep_first` turns (default 1) + the recent `resume_keep_recent` turns
    (default 20) verbatim and
    condenses the middle into one note, so a long thread doesn't blow the context
    window. By default the note is a no-model summary (lists what the omitted
    turns covered); `[chat_history] compact_on_resume` (off by default) swaps it
    for an LLM summary, falling back to the note if that fails. **The saved
    transcript is never modified** — compaction only shapes the working history
    sent to the model (`conversations.split_for_compaction`, bridge
    `_flatten_turns`/`_mechanical_resume_note`/`_summarize_middle_turns`; the note
    is folded into the first recent message to keep strict user/assistant
    alternation). `resume_conversation` now reports `{kept, compacted, summarized}`.
  - Front end: a Conversations drawer + `conversationToBlocks` hydration (one
    chat block per turn) + `useInvocationBlocks.hydrateBlocks`.

## [0.145.0] — 2026-06-28

### Fixed
- **The agent now knows today's date.** Nothing in the agent's prompt told it
  the current date, so it resolved relative dates ("last Friday", "the 26th")
  from its training cutoff and got them wrong. Every agent turn — chat, trigger,
  and schedule — now folds in a `_now_context()` block carrying the operator's
  **local machine date** (weekday + ISO date) so relative dates resolve against
  the real clock. Injected on the per-turn path (`_spawn_chat_turn`'s `extra`
  for chat; the composed `system` in `_trigger_run_agent_turn` for triggers and
  schedules) rather than the memoized adapter, so the date is always fresh. It's
  **date-only** on purpose — the string is byte-identical across a day's turns,
  so the model-side prompt cache keeps hitting (a per-minute clock would churn
  it every turn).

## [0.144.0] — 2026-06-28

### Added
- **Credential env-var aliases — a credential labelled to match a tool just
  works.** Each kind now emits the common spellings, not just one: `userpass`
  sets both `*_USERNAME` and `*_USER`; `token` sets both `*_TOKEN` and `*_PAT`
  (canonical first; the aliases are additive and backward-compatible). So a
  credential labelled `JSM` feeds `runspec-jsm` (`JSM_USER`/`JSM_PAT`) and one
  labelled `SNOW` feeds `runspec-snow` (`SNOW_USER`/`SNOW_TOKEN`) with no
  per-tool config. (`credentials._KIND_ENV_FIELDS`; UI preview mirrored in
  `FormsView.credEnvNames`.)

### Changed
- **Forms run dialog now honours credential arg-bindings — credentials work the
  same for humans as for the agent.** Previously a credential's
  `username_arg`/`secret_arg` binding only took effect on the agent path, so a
  runnable whose interface is *arguments* (`--user`/`--password`, not bare env
  vars) couldn't be filled from a credential on the Forms run dialog. The run
  dialog now applies the operator's **explicitly selected** credentials'
  bindings (`bridge._apply_selected_credentials`, called from `invoke_runnable`):
  the username **pre-fills** the arg field (visible + editable), the secret rides
  the `RUNSPEC_*_ARG_*` env channel (and is popped from argv so the CLI can't
  override it with a blank), and the required-arg check is relaxed for a bound
  field. Each bound field shows a "from `<credential>`" badge so the fill is
  never silent. This deliberately keeps the Forms/agent split's *intent*: it is
  explicit-selection-driven (no scope auto-apply on the Forms path) and
  bindings-only (no sole-password fallback), so every credential-supplied field
  is one the dialog can label. The agent path keeps `_apply_credentials`
  (scope auto-apply + sole-password fallback) unchanged. Per-arg badge logic is
  `FormsView.bindingsForArgs`.

## [0.143.0] — 2026-06-28

### Added
- **Built-in agent tools are now visible and stoppable.** When the chat agent
  ran a built-in tool — the `repo_*` repo tools, `outlook_*`, `manage_denylist`,
  the action tools (`create_document`/`run_runbook`/`create_schedule`/
  `emit_event`) and the memory tools — the work was invisible: no Console
  in-flight card, no progress, and the Stop button had no effect until the tool
  finished (a long `repo_sync` clone or `repo_grep` read as a hung console).
  Each built-in tool now, once past the autonomy gate:
  - **Shows a live Console in-flight card** (label like `repo_sync: runspec`,
    elapsed time, Cancel button) in the same strip runnable runs use — pushed
    immediately via `runspec:in_flight_updated`, not on the 5s poll.
  - **Streams progress** into the chat tool segment via a new
    `runspec:tool_progress` event: `repo_sync` surfaces dulwich's clone/fetch
    progress, `repo_grep` a running "scanned N files" count, and
    `manage_denylist` a "host i of n" line.
  - **Is cooperatively cancellable.** The card's Cancel (`cancel_invocation`)
    stops just that tool — the agent gets a "⏹ stopped" result and the turn
    continues; the chat **Stop** (`cancel_chat`) now also aborts a built-in tool
    mid-run and halts the turn. `repo_sync` aborts the transfer at a progress
    boundary and removes a half-made checkout; `repo_grep`/denylist abort between
    files/hosts. New `ToolCancelled` sentinel (`bridge_const`).
  - **Leaves a History/Analytics audit record** — one `run_summary`-shaped line
    per call to `~/logs/runspec-console-tools.log`, read by the existing history
    scanner with no parser change (attributed to the tool label; outcome and
    cancellation captured via `exit_code` 0/1/130).

  Implemented as one uniform wrap in `bridge_autonomy._run_builtin_tool_tracked`
  (with `_builtin_tool_label`/`_host`/`_args` helpers) around the existing
  `_run_tool_sync` path; `repos.sync_repo`/`grep_repo`,
  `bridge_agent_tools._run_repo_tool`/`_run_manage_denylist` gained
  `cancel_event`/`on_progress`; audit via `bridge_reporting._audit_builtin_tool`.
  Front end: `runspec:tool_progress` rendering in `OutputPanel` `ToolCallBlock`
  and a `builtin_tool`-aware label in `ConsoleView`'s `InFlightStrip`. Runnable
  executions (`run_runnable`/search meta-tools) are unaffected — they keep their
  own `invoke_runnable` card.

## [0.142.0] — 2026-06-27

### Changed
- **Self-service designation moved from `runspec.toml` into a console config
  file.** Which discovered runnables are offered to *visitors* in the helpdesk
  room is now configured here, not baked into each package's `runspec.toml` (the
  `discoverable = ["self-service"]` marking was removed from the format in
  `runspec` 0.47.0). A new **Settings → Self-Service** tab (`SelfServiceTab` in
  `SettingsDrawer.tsx`) toggles runnables on/off; the set is stored in
  `runspec_self_service.toml` — a `[[self_service]]` collection that rides Config
  Sync (merged by `name`, like `runspec_groups.toml`). The catalog publisher
  (`catalog.build_full_catalog` / `build_catalog`) now takes the configured
  `self_service_names` set instead of reading each runnable's `discoverable`
  audience; every discovered runnable is at least operator-visible, and the
  per-queue `room_catalog` allowlist still narrows the self-service set per queue.
  New module `self_service.py`, bridge `get_self_service`/`save_self_service`,
  config `self_service_path()`. See `docs/design/operator-self-service.md`.

## [0.141.0] — 2026-06-27

### Added
- **Room connection status in the console footer.** The backend already emitted
  per-room connect/drop events (`runspec:room_connection`) and exposed
  `get_room_state()`, but nothing in the UI rendered them — a room server going
  down (e.g. a console-room reaped on logout because `loginctl enable-linger`
  wasn't set) was only visible by reading Dev-tab SSH logs. The footer now shows
  a live **`N/M rooms`** indicator (green all-up / amber some-down / red all-down)
  with a hover breakdown listing each configured room + host and its
  connected/down state, plus a nudge to check the room process when one is down.
  Hidden entirely when no rooms are configured (`App.tsx`).

### Changed
- **Quieted two sources of room-connection log noise.** When a host's
  `console-room` server isn't reachable on its port, the room tunnel's
  direct-tcpip channel-open is refused and paramiko logs
  `Secsh channel N open FAILED: Connection refused: Connect failed` at ERROR on
  **every** reconnect backoff (with an incrementing channel id), so the Dev tab
  filled with the same error 4+ times. The Dev-tab handler (`_ParamikoDevHandler`)
  now **coalesces identical SSH records within a 60s window** — normalising the
  channel id out of the dedup key — so a reconnect storm shows one row, not N; a
  record that persists past the window resurfaces, and any record carrying a
  traceback is never suppressed. (The console already logs its own friendly,
  deduped, host-aware summary of this exact "is the room process running on its
  port?" condition to the app log — that is unchanged.)
- **Catalog re-publish no longer spams the app log on a flapping room.** A room
  that opens, publishes, and drops repeatedly used to log
  `published N tool(s) to 'helpdesk/helpdesk' catalog` at INFO on every
  reconnect, because the forced subscribe re-publish always logged and the
  disconnect path cleared the per-namespace publish cache. `_publish_catalog`
  now logs INFO **only on a genuine content change**; a forced re-send of an
  unchanged catalog (the reconnect path) still sends — a fresh hub needs it —
  but logs at DEBUG. The cache is retained across the drop so an unchanged
  reconnect is recognised as such.


## [0.140.0] — 2026-06-27

### Added
- **TCP socket keepalive on pooled SSH transports + host-attributed SSH events.**
  Two changes aimed at the idle-disconnect → reconnect → catalog-republish churn
  seen in the Dev tab. (1) `executor._make_ssh_client` now enables
  `SO_KEEPALIVE` on the transport's underlying socket and sets the probe schedule
  (`TCP_KEEPIDLE`/`TCP_KEEPALIVE` 60s, `TCP_KEEPINTVL` 15s, `TCP_KEEPCNT` 3) where
  the OS exposes it (Linux/macOS `setsockopt`; Windows `SIO_KEEPALIVE_VALS`
  ioctl), so the kernel reaps a silently-dead peer and middleboxes don't evict an
  idle NAT mapping. This is a **backstop** to paramiko's existing application-layer
  keepalive (`[ssh] keepalive`, default 30s, which usually fires first); the room
  tunnel rides the pooled transport, so it inherits the protection. Tunables:
  `[ssh] tcp_keepalive` (off switch), `tcp_keepidle` / `tcp_keepintvl` /
  `tcp_keepcnt`. Best-effort — an unsupported option is skipped, never raised.
  These tunables are also editable from **Settings → SSH → Advanced** (a new
  "TCP keepalive" subsection: a default-on socket-keepalive switch plus the three
  probe-schedule numbers), persisted to `[ssh]` like the existing connect/banner/
  auth timeouts — `SettingsDrawer.tsx`.
- **SSH Dev-tab events name their host.** Each transport's paramiko log channel is
  tagged `paramiko.transport.<host>`, and `_ParamikoDevHandler` parses the host
  back out, so a steady-state SSH event (idle-disconnect, EOF, keepalive failure)
  in the Dev tab now carries a `host` field and an `[<host>]` message prefix
  instead of an anonymous `paramiko.transport` row. (Connect-time banner errors,
  which predate the transport, were already logged with the host by the pool's
  WARNING.)


## [0.139.0] — 2026-06-26

### Added
- **Plugin adapters can require their own `[llm]` config keys.** When building an
  LLM adapter, `Bridge._get_adapter` still threads the built-in keys (`api_key`,
  `model`, `system`, `base_url`, `api_key_command`, `api_key_ttl_ms`, the
  LangServe knobs) from `[llm]`, but now **also inspects the adapter factory's
  constructor signature** and forwards any further `[llm]` key whose name matches
  a declared parameter. So a custom provider whose `__init__` takes `tenant_id`
  receives `[llm] tenant_id` with no allowlist change in runspec-console. Built-in
  / `**kwargs`-only factories declare no named parameters, so they are unaffected;
  `provider` and `system` are reserved and never forwarded raw. New
  `runspec_console.adapters.base.adapter_config_keys(provider)` (best-effort
  signature inspection, empty on unresolvable/uninspectable factories); factory
  resolution split into a shared `_resolve_factory` helper.

---


## [0.138.0] — 2026-06-26

### Added
- **Settings → Repos: choose a custom checkout location per repo.** A repo
  normally checks out under the app-data `repos/{name}` dir — fine for read-only
  repos you'll never edit. For a repo you actively develop, the repo form now has
  a **"Checkout location"** field (with a native **Browse…** folder picker) to
  point its checkout at your own dev folder instead, so the agent reads/edits the
  same working copy you do. That path is **machine-local**: it's stored in its own
  `runspec_repo_paths.toml` and is **never** synced (it's specific to this
  machine's filesystem), while the repo registry that rides Config Sync stays
  path-free. The repo row shows a **`custom`** tag when an override is set.
  (`config.repo_paths_path`; `repos.read_repo_paths`/`custom_path`/`set_repo_path`/
  `checkout_dir` honouring the override; bridge `browse_folder`.)

### Changed
- **Deleting a repo now also removes its app-data checkout from disk** (previously
  the on-disk clone was left behind). A **custom dev-folder checkout is never
  touched** — those are your own working copies, possibly with uncommitted work —
  only the override mapping is dropped. New bridge `delete_repo` (the registry
  remove + AppData-only cleanup); the Settings delete confirmation now says which
  applies.

### API
- New pywebview bridge methods: `delete_repo(name)`, `browse_folder()`. `get_repos`
  rows gain `path` (the machine-local override, "" = default) and `checkout_dir`
  (the effective absolute path); `save_repos` splits a repo's `path` out to the
  machine-local file and strips it from the synced registry.


## [0.137.1] — 2026-06-26

### Fixed
- **Settings → Repos: a failed clone left an orphan `.git` that showed a green
  "synced" chip with no files.** Three linked issues: (1) a new credential didn't
  appear in the repo dropdown until a restart — `ReposTab` now refreshes on the
  `runspec:credentials-changed` event the Credentials tab already fires; (2) the
  "synced" status keyed only on a `.git` dir existing, so a failed checkout read as
  synced — `is_checked_out` now requires a real working tree; (3) `sync_repo` ran
  `Repo.init` (writing `.git`) before the fetch, so a failed **fresh** clone (e.g.
  a credential/auth failure) left that orphan `.git` behind — it now removes the
  half-made checkout on failure (an existing checkout is preserved), and fails
  loudly if a non-empty commit ever materialises zero files. Net: a bad-auth sync
  now surfaces a clear error and an honest "not synced", instead of a misleading
  green chip over an empty folder.

### Changed
- **Replaced the jargon "LLM" in user-facing wording (strings/labels only — no
  code, config keys, or identifiers changed).** Context-aware, not a blanket
  swap:
  - The **acting entity** in chat/cards now reads **"assistant"** — "ask the
    assistant", "Ask the assistant", "Triggered by the assistant", "the assistant
    can invoke this", trigger help text. ("Assistant" avoids colliding with the
    human *helpdesk agent / operator* meaning the product also uses.)
  - Where the word names the autonomous **mode**, it stays **"agent"** — the
    redundant "(LLM)" qualifier is dropped ("Agent (LLM turn)" → "Agent"; the
    analytics "User vs agent (LLM)" → "User vs agent"; the analytics initiator
    filter "LLM" → "Agent").
  - The **provider/model configuration** reads **"Model"** — the Settings tab
    "LLM / API" → "Model / API", and the misconfig messages ("No LLM provider
    configured" → "No model provider configured", "Unknown LLM provider" →
    "Unknown model provider", etc.).

  Config keys (`[llm]`, `_llm` templates, `tool_mode`), the provider-plugin API,
  and all identifiers are unchanged.

## [0.137.0] — 2026-06-26

### Changed
- **Renamed "skills" → "runbooks" everywhere.** The TOML-based, parameterized
  procedures the console calls *skills* are now consistently called **runbooks**,
  to stop the agent confusing them with Claude Code *skills* (markdown `SKILL.md`
  files) when told to "use a skill". This touches the whole surface:
  - **Agent tools** are now `save_runbook` / `load_runbook` / `run_runbook` (were
    `save_skill` / `load_skill` / `run_skill`), with descriptions to match. The
    schedule `action = "runbook"` replaces `action = "skill"`.
  - **UI**: the `$` menu, the Forms-tab swimlane, the Settings tab, and the
    schedule modal all say "Runbooks" / "Run a runbook".
  - **Storage**: the collection file is `runspec_runbooks.toml` with `[[runbook]]`
    / `[runbook.args]` (was `runspec_skills.toml` / `[[skill]]`); bridge API is
    `get_runbooks` / `save_runbooks` / `run_runbook`; the run-provenance env var is
    `RUNSPEC_RUNBOOK`; the UI refresh event is `runspec:runbooks_changed`.
  - **Back-compat** (so the rename is seamless on an existing install): a legacy
    `runspec_skills.toml` is migrated to the new name on first read; a schedule
    with `action = "skill"` (or a `skill` field) is still accepted and treated as a
    runbook. **Config Sync note:** a synced repo that still ships
    `runspec_skills.toml` should rename it to `runspec_runbooks.toml` — the pull
    now looks for the new name.
- **`save_runbook` is also the edit path.** Its description now states that saving
  with an existing id overwrites that runbook — so to edit one the agent does
  `load_runbook` → change fields → `save_runbook` with the same id. No separate
  edit tool (the upsert already covers it).

## [0.136.1] — 2026-06-26

### Fixed
- **A runbook the chat agent saved didn't appear until a console restart.** The
  agent's `save_skill` tool persisted the skill correctly, but — unlike the
  Settings → Skills editor — it dispatched no UI event, so the `$` skills menu,
  the Forms tab, and the Skills editor kept showing the old list until the next
  start (when `get_skills` re-reads the file). `_run_memory_tool` now fires
  `runspec:skills_changed` after a successful `save_skill`, so an agent-saved
  runbook shows up live everywhere, no restart needed.

## [0.136.0] — 2026-06-26

### Added
- **Settings → Repos tab** — manage the git-repo registry (`runspec_repos.toml`)
  from the UI instead of hand-editing TOML. The tab is the agent's **read/edit
  allowlist**: add/edit/delete repos (name, URL, branch/tag, a credential picked
  from the Credentials tab, description), with a per-row **Sync now** button
  (operator-initiated clone/fetch — the click is the authorization, so no autonomy
  gate) and a live checked-out / synced-commit status. New bridge methods
  `get_repos` / `save_repos` / `sync_repo` (`bridge_fleet.py`); the registry still
  rides Config Sync. Deleting an entry revokes the agent's access but leaves the
  local checkout on disk.

## [0.135.0] — 2026-06-26

### Added
- **Eight new Outlook Classic agent tools** (desktop COM) — read/triage and create
  calendar events without any new Graph permission, riding the signed-in desktop
  session. Reads (autonomous): `outlook_get_message` (full body + headers +
  attachment list — closes the gap where list/search rows only carried a 280-char
  preview), `outlook_resolve_recipient` (GAL/contacts → SMTP). Writes (confirm):
  `outlook_save_attachment` (to disk, e.g. to feed a log into `runspec-logops`),
  `outlook_create_event` (meeting invite with attendees, else a personal block),
  `outlook_respond_invite` (accept/tentative/decline), plus the helpdesk-triage
  trio `outlook_mark_read` / `outlook_categorize` / `outlook_move`. `outlook_draft`
  also gained a `reply_all` mode.
- **Git-repo agent tools (local checkouts)** — ground the agent in real source code
  and make small, reviewed *local* edits, reusing the Config Sync git machinery
  (dulwich + paramiko SSH + HTTPS CA/proxy), so auth/CA/proxy behave exactly as
  Config Sync does. New **`runspec_repos.toml`** registry (the controlled list of
  repos the agent may touch; rides Config Sync, merged by `name`). Seven tools:
  `repo_status` / `repo_list` / `repo_grep` / `repo_read` / `repo_diff` (read-only,
  autonomous) and `repo_sync` (clone/fetch + checkout) / `repo_write` (local file
  edit) (confirm-gated). Checkouts live at `{app_dir}/repos/{name}` (never synced);
  every path is fenced by `_safe_path`; reads are byte/size/result-bounded; binaries
  are skipped.
- All new tools are selectable in the per-trigger action allowlist and named in the
  per-turn `_console_context`.

### Notes
- `outlook_create_event` does **not** add a Teams join link by itself — the desktop
  Teams add-in is not scriptable over COM; a link appears only if the org enables
  online-meeting-by-default (or via a separate Graph `OnlineMeetings` scope).
- Repo edits are **local only — no commit/push by design**; the operator reviews with
  `repo_diff` and commits with their own tooling (a re-`repo_sync` restores files
  from the remote). Registry is file/Config-Sync managed for now (no Settings UI / no
  new public bridge API surface). See `docs/design/repo-tools.md`.


## [0.134.0] — 2026-06-26

### Fixed
- **The agent is no longer "blind" on the first turn after launch (search mode).**
  Fleet discovery filled the runnables cache in one atomic assignment at the *end*
  of the refresh cycle — after the connectivity probes and discovery across **every**
  host finished — so a chat turn fired before that completed saw an empty cache. In
  `[llm] tool_mode = "search"` the searchable menu *is* that cache, so the agent had
  nothing to search and silently gave a degraded answer (and a single slow/hanging
  bastion delayed even local runnables). Discovery now **merges each host into the
  cache as it completes** (`bridge_internals.py` `_refresh_cycle`), so local (no SSH —
  near-instant) and fast hosts are usable immediately; the canonical end-of-cycle
  assign still prunes hosts that left the fleet. The search-mode catalog index also
  carries a *"discovery in progress — N of M hosts loaded"* note while the first cycle
  runs, so the model offers to retry instead of concluding a runnable doesn't exist.

### Added
- **"Discovering fleet… N/M hosts" indicator** above the chat composer while the first
  discovery cycle runs, so a first turn that races discovery is visibly explained.
  Backed by a new `runspec:discovery_state` event (dispatched per host as discovery
  completes) and a `discovery_state()` bridge method (read on mount in case the events
  predate the listener).
- **Catalogue size in the sidebar footer.** A live `N tools · N venvs` readout (with a
  hover popover breaking the catalogue down per host → per venv) sits above the version
  line. The count **grows as discovery streams hosts in** on startup — so you can watch
  it climb and learn to expect a steady-state number — and shows `done/total hosts`
  while the first cycle runs. A spinner stands in for the icon until discovery settles.
  The count **flashes green briefly when it changes**, so the eye catches it climbing.
- **Per-host runnable counts in the sidebar.** Each host row shows its own tool count
  next to the name, with a tooltip breaking that host down per venv — so a host
  advertises how much it contributes without opening the footer popover.
- **Friendlier version/update footer.** The version is now click-to-open-docs, "up to
  date" gains a check icon, and a "checking…" state shows while the update probe is in
  flight (previously blank).


## [0.133.0] — 2026-06-25

### Added
- **Operator room: see and pick which host a runnable runs on.** A fleet often
  exposes the same runnable on several hosts (e.g. `check-disk` on every Linux box),
  but the catalogue deduped by name and silently ran it on the first discovered host
  — so an operator couldn't target *that* server. `build_full_catalog` now collapses
  same-named runnables into one entry whose **`x-hosts`** lists every host it was
  discovered on, and keeps a per-host enactment index (`name → {host: runnable}`). In
  the room form, an operator session gets a **Host** picker when a runnable has more
  than one host (and a "Runs on `<host>`" caption when it has one); the chosen host
  rides the request (`kind="runnable"` requests gain a `host`) to the enacting
  console, which runs it there (`_run_assignment(…, host=…)`), failing clearly if that
  console can't reach the named host. Visitors and runbooks are unchanged (a runbook
  runs on the console and targets hosts from inside its body). Requires runspec-room
  ≥ 0.30.0.

  *(0.132.0 is reserved for the visitor-facing-runbooks change on a separate branch;
  this lands as 0.133.0 to avoid a version collision.)*
## [0.132.0] — 2026-06-25

### Added
- **Visitor-facing self-service runbooks.** A `[[skill]]` runbook can now be opened
  to ordinary email-verified visitors, not just operators, by tagging it
  `discoverable = ["self-service"]`. Published skills are now stamped with the
  **broadest** audience they qualify for (self-service when self-service-marked, else
  operator), mirroring how runnables are tagged — so a self-service runbook appears
  as a form in a visitor's Runbooks section and a `kind="skill"` request routes to an
  operator console, which renders the body and runs it as the same autonomy-gated
  agent turn (every runnable it reaches still parks at the per-runnable confirm gate).
  Operator-only runbooks (`discoverable = ["operator"]`) are unchanged. The room and
  frontend were already audience-generic; this is a one-field change in
  `catalog.build_skill_catalog` / `skill_to_catalog_entry`. Requires runspec-room
  ≥ 0.29.0.


## [0.131.1] — 2026-06-25

### Fixed
- **Triggers editor: the rule-mode runnable picker stayed empty.** The editor
  fetched hosts + runnables once on mount, but runnable discovery finishes
  asynchronously (after the SSH connect that the host list doesn't wait on), so an
  editor mounted before discovery completed showed an empty runnable dropdown even
  though the host/group selector worked. `TriggersEditor` now refreshes on the same
  `runspec:hosts_updated` / `runspec:runnables_updated` events App listens for, so
  the runnable list fills in as soon as discovery lands.

## [0.131.0] — 2026-06-25

### Added
- **Operator self-service + runbooks in the console-room.** The console now
  publishes one **audience-tagged** catalogue to each queue's room instead of just
  the self-service subset: every entry carries `x-audience`
  (`self-service` | `operator`) and `x-kind` (`runnable` | `skill`). A new
  `build_full_catalog` emits each runnable once, tagged with the broadest audience
  it qualifies for — self-service when it's self-service-marked *and* passes the
  `room_catalog` visitor allowlist (visible to everyone), otherwise operator
  (visible only to operator sessions; the operator set is deliberately *not*
  narrowed by the visitor allowlist). The room filters per session, so an operator
  who logs into a queue's room sees and can run the **full operator catalogue** as
  forms. (`catalog.py`, `bridge_triggers.py` `_publish_catalog`.)
- **Self-service runbook skills (operator-only).** A `[[skill]]` runbook can now opt
  into a queue's catalogue via a `discoverable` audience field (default empty —
  runbooks aren't published unless marked). Published runbooks appear as
  `skill:<id>` catalogue entries whose form `inputSchema` is built from the skill's
  `[skill.args]` (the same machinery the Forms tab uses). Submitting one routes a
  `kind="skill"` portal request to an operator console, which renders the body with
  the submitted values and runs it as an **autonomy-gated agent turn** (reusing the
  trigger/schedule isolated-turn path) — every runnable the runbook reaches still
  parks at the per-runnable confirm gate, and the request reports `done` only when
  the whole turn finishes. (`catalog.skill_to_catalog_entry`/`build_skill_catalog`,
  `bridge_triggers._run_skill_assignment`, `memory.save_skill` `discoverable`.)


## [0.130.0] — 2026-06-25

### Changed
- **Triggers editor: rule "Run" is now a host/group + runnable picker with an
  arg list (was free text).** A rule-mode trigger's target — both the single
  "Run" field and every row of the "Multiple runnables" action list — is now a
  **Host/Group toggle + target selector + runnable selector** (mirroring the
  Schedules modal) instead of a hand-typed `host__runnable` string. Picking a
  runnable **auto-lists its declared args** as labelled rows (required ones
  flagged, type/options shown as a placeholder hint). Crucially the values stay
  **template-capable** — each is free text, so a literal *or* a `{{ payload field }}`
  / `{{ _llm.x }}` template both work (templating is the whole point of trigger
  args, so they are not typed inputs like the schedule's). Anything not in the
  spec is kept as an editable "custom arg" row, with an *Add custom arg* button
  for extras. The current selection always renders even if the host is offline or
  the runnable undiscovered, so editing an existing trigger never loses its target.
  Pure helpers `parseRun`/`composeRun`/`argsForRun` are unit-tested.

## [0.129.0] — 2026-06-25

### Added
- **Schedule a prompt or a skill runbook (Schedules tab).** A schedule's `action`
  now has two new options beyond `runnable` / `event`:
  - **`action = "prompt"`** runs a headless **agent turn** on a fixed instruction
    when it fires — so a human (or the agent itself) can schedule something to
    *check or do later* in natural language, e.g. *"every morning, check disk usage
    on the web hosts and flag anything over 80%."*
  - **`action = "skill"`** runs a saved **skill runbook** (by id) as an agent turn:
    the runbook body is rendered with any `values` filling its `{{placeholders}}`,
    then handed to the agent as its instructions — re-run a runbook on a cadence
    without re-typing it.

  Both run the same isolated, autonomy-gated loop a reactive agent-mode trigger
  uses (`_trigger_run_agent_turn`), so a destructive tool the agent reaches for
  still parks at the confirm gate; the turn runs on its own thread (non-blocking)
  and surfaces a run card + event-log entry in the Schedules tab like any other
  fire. New `ScheduleJob` fields `prompt` / `skill` / `values`; `parse_schedule_entry`
  validates them; `ScheduleSource` gets a `run_agent` callback wired to the bridge's
  `_run_scheduled_agent` / `_invoke_scheduled_agent`.
- **The agent can create these schedules and is aware of them.** The built-in
  `create_schedule` action tool's `action` enum now offers `prompt` and `skill`
  (with the saved-runbook ids as a `skill` enum), and the per-turn console-context
  block renders prompt/skill schedules and tells the agent it can schedule a
  prompt or runbook to run later. The Schedules-tab modal gains "Run a prompt"
  and "Run a skill" actions.
- **Run-on-any-one-host schedules + selectors in `create_schedule`.** A
  runnable-action schedule can now target a **candidate host list** (`hosts`) as a
  third option beside a single `host` and a `group`: at fire time it runs on the
  **first connected** host in the list (failover), one run. The Schedules-tab modal
  gains an "Any of hosts" target (multi-select); the agent's `create_schedule` tool
  now exposes its targets as **selectors** — `runnable` is an enum of the fleet's
  distinct runnable names, `host` / `group` are enums, and `hosts` is an array of
  the host enum — so the agent picks valid values instead of guessing a string.
  New `ScheduleJob.hosts`; bridge `_pick_connected_host`.

### Changed
- **Trigger/schedule agent turns reach discovery parity with chat (search mode).**
  In `[llm] tool_mode = "search"`, an unrestricted trigger or schedule agent turn
  now gets the same always-on **catalog index** (the full `id — description` menu)
  that the chat turn already folds in, so it discovers what runnables exist
  identically. Allow-scoped triggers are unaffected (they fall back to the dedup
  layout, which carries full schemas inline). `_run_isolated_agent_turn` folds
  `_build_catalog_index` into a per-call copy of its history (the stored history
  stays clean), matching `_agentic_chat_turn`.

## [0.128.1] — 2026-06-25

### Fixed
- **Wheel build failed (`A second file is being added to the wheel archive at the
  same path: runspec_console/sounds/__init__.py`).** The bundled sounds package was
  added to `[tool.hatch.build.targets.wheel.force-include]` even though it lives
  inside the `runspec_console` package and is already VCS-tracked, so hatchling
  included every `sounds/` file twice. Removed the redundant force-include — the
  `.wav` cues and module ship via the normal package selection. force-include is
  now only the gitignored Vite `dist` and `runspec.toml`.


## [0.128.0] — 2026-06-25

### Added
- **Audio cues + a notification fix, so you can run the console in the background
  and still know what's happening.** A new backend `sounds` package plays short
  cues via stdlib `winsound` — which works even when the window is **minimized**
  (frontend HTML5 audio gets throttled). Distinct synthesized cues for: a
  confirmation waiting (the urgent one), a trigger firing, a run succeeding /
  failing / being stopped, an **agent reply ready** while the console isn't
  focused, and a chat/helpdesk message. Each is configurable (synthesized set /
  Windows system sound / off). Generated by `sounds/_generate.py` (original tones,
  no licensing). On by default; high-frequency events (tokens, output, transfer
  progress) stay silent.
- **Settings → Sounds & Notifications** tab: master toggle, per-event sound choice,
  the previously-hidden `confirm_notifications`/`room_notifications` toggles, an
  availability indicator, and a **Test** button (`test_notification`) that fires a
  toast + flash + sound and reports which channels worked (incl. `window_found`,
  surfacing the flash title-match prerequisite).

### Fixed
- **Taskbar flash now fires even without the `[notifications]` extra.** The flash
  (pywin32, a core dep) was scheduled *after* an early `return` that triggered when
  the WinRT toast extra was missing — so a confirmation/message produced no flash
  *and* no toast. The toast availability check now gates only the toast; the flash
  and sound always fire (`bridge_autonomy.py` `_notify_confirm`/`_notify_room_message`,
  `bridge_triggers.py` `_notify_page`). Added a debug log when the window title
  doesn't match (flash/focus can't find the window).


## [0.127.0] — 2026-06-24

### Changed
- **Output cards now follow the theme too — completing the light-theme pass.** The
  card shell, header, JSON tables / structured views, tool-call blocks (incl. the
  amber approval gate), raw-log view, and the streaming transcript were hard-coded
  dark; they're now theme-aware via a shared `useCardTheme()` palette in
  `OutputPanel.tsx`. This fixes the inconsistency where console cards stayed dark
  but trigger-panel cards went light — now everything is theme-aware, nothing is
  pinned to a single mode.


## [0.126.0] — 2026-06-24

### Changed
- **Light-theme pass on the chrome that ignored the theme.** The composer (prompt
  box + slash/skills menus), the top nav-tab labels, and the sidebar host-group
  headers were hard-coded dark / washed-out grey; they now follow the light/dark
  toggle. The prompt box was a deliberate dark element originally, but it now reads
  correctly in light mode like the rest of the chrome. (Output cards were completed
  in 0.127.0.)


## [0.125.0] — 2026-06-24

### Fixed
- **Card bodies now keep the agent's line breaks.** The markdown renderer followed
  CommonMark, where a single newline collapses to a space — so multi-line agent
  prose ran together. Added `remark-breaks` so a single newline renders as a line
  break. Applies everywhere markdown is shown (console + trigger panel).

### Changed
- **Wider Triggers detail drawer** — `min(1040px, 94vw)` (was `820px`), so the
  richer transcript (tool blocks + tables) has more room.


## [0.124.0] — 2026-06-24

### Changed
- **Trigger-panel cards now render the same rich agent interaction as the console.**
  Previously the Triggers panel showed an agent turn as flat markdown — no tool
  calls, no structured output — so it felt "watered down." The chat-transcript and
  structured-output renderers are now extracted from the console card into shared
  components (`ChatTranscript`, `StructuredOutput` in `OutputPanel.tsx`), and the
  Triggers panel (`RunInbox` drawer) uses them: a live agent turn shows its
  **interleaved tool-call blocks** (inputs/outputs/durations) and a JSON result
  renders as a **table**, exactly like the console.
- `TriggersView` now builds a per-card transcript from the `tool_start`/`tool_end`/
  `token` events the trigger agent already emits (with the card id) — so the tool
  blocks appear live as the agent works. The console card is unchanged (now sourced
  from the shared `ChatTranscript`). Schedules are unaffected (no transcript;
  finalised JSON output now renders as a table there too).
- **Known limitation:** the shared renderer uses the console's dark palette, so the
  panel transcript looks best in dark mode; a light-theme pass is a follow-up.


## [0.123.0] — 2026-06-24

### Added
- **Composer grows upward as you type.** The prompt textarea auto-expands to fit
  multi-line input, growing up over the transcript (it's bottom-anchored), capped
  at ~45% of the window height before it scrolls — up from the old fixed 240px, and
  recomputed on window resize.
- **Draft persistence.** A half-typed prompt now survives tab switches and app
  restarts (localStorage), so an in-progress message is never lost.
- **Edit & resend on chat turns.** Each finished chat card gets two actions: *edit
  & resend* (loads the original prompt back into the composer to tweak) and *resend*
  (fires it again as-is).
- **Live elapsed clock on running turns.** A chat turn's card header now shows a
  ticking elapsed timer while the agent works (chat turns don't appear in the
  in-flight strip, so there was no running clock before).
- **Tool-call count on chat cards.** A small badge shows how many tool calls a turn
  made.
- **Stopped vs error status.** A user-halted turn now shows a neutral *stopped*
  status (grey pause icon + tag) instead of a misleading green "ok" / red "error" —
  the backend now tags `run_end` with `stopped`.
- **Copy the prompt.** A copy action on each chat card copies the original prompt
  text.
- **Per-tool durations (tool timeline).** Each tool call now shows how long it took,
  so a slow turn's time is attributable.
- **Estimated cost.** A `~$x` tag (and the token tooltip) shows an approximate USD
  cost from the turn's tokens + model (Anthropic list prices; omitted for unknown
  models — a labelled estimate, `chat_usage` now carries the model id).
- **Collapse all / Expand all.** Transcript-header controls fold or unfold every
  card at once.

(Smart auto-scroll with a "jump to latest" pill already existed and is unchanged.
Anchor/permalink-to-a-card was dropped — URLs don't fit a desktop window; the
collapse/expand + existing jump pill cover navigation.)


## [0.122.0] — 2026-06-24

### Removed
- **The composer's Speak / Listening… voice button is removed.** Driving Windows
  Voice access from the app proved unreliable — the Alt+Shift+B wake/sleep chord
  works from a physical keyboard but the *injected* keystroke doesn't wake Voice
  access, even after making the injection mimic real hardware (per-key events +
  holds + `MapVirtualKey` scan codes). The likely cause is Voice access ignoring
  synthetic input (injected-input flag / UIPI), which `SendInput` can't defeat.

### Kept (dormant, no UI caller)
- `winkeys.py` (now with the scan-code-correct injection) and the
  `start_voice_dictation`/`stop_voice_dictation` bridge methods (in
  `bridge_triggers.py`, still in the frozen JS API surface) are retained so a
  future re-enable — or, more likely, a real local STT engine
  (faster-whisper/Vosk) — can reuse the plumbing. **Note for that work:** outbound
  HuggingFace is blocked on the target fleet, so the model must be **vendored**
  (bundled / internal mirror), not downloaded at first run. See
  `docs/design/console-voice-input.md`.


## [0.121.0] — 2026-06-24

### Changed
- **Refresh cycle uses a bounded worker pool (SSH connection pool phase 2).**
  `_refresh_cycle` previously spawned one OS thread per host per phase (probe +
  discovery) — ~2N short-lived threads every `[refresh] interval`, which became
  the bottleneck at many hundreds / low thousands of hosts once the connection
  pool removed the handshake storm. It now fans both phases out over a bounded
  `ThreadPoolExecutor`, so the thread count is a small constant regardless of
  fleet size. New `[refresh] workers` knob (default `max(16, 2 * [ssh]
  max_concurrent)`); an explicit value overrides. Behaviour is identical for
  small fleets (with `workers >= 2N` every host still runs concurrently) and
  byte-for-byte equivalent in output (connected cache, deduped runnables, the
  `hosts_updated`/`runnables_updated` events, catalog republish, reap). Probe and
  discovery moved to `_probe_one`/`_discover_one` worker methods; per-host jitter
  and per-op SSH timeouts (which bound a hung host to its own worker slot) are
  unchanged. See `docs/design/refresh-cycle-thread-pool.md`.


## [0.120.0] — 2026-06-24

### Added
- **Host groups are now a first-class targeting concept.** A host's `group`
  (in `runspec_hosts.toml`) was previously only a sidebar label; a group name can
  now run a runnable on **every host in the group at once**:
  - **Agent**: `run_runnable`'s `host` may be a group name — it fans out to every
    member that carries the runnable behind **one grouped confirm** (most-restrictive
    autonomy wins; a `manual` member refuses the whole group; args editable before
    approve). A literal host name wins on a host/group name collision. The chat
    agent is also made **group-aware** via a bounded "Host groups" block in the
    per-turn console context (group → connected/total + member names).
  - **Triggers**: a rule `run` or an `actions[].run` may be written
    `group:<group>__<runnable>` to fan out (single grouped confirm; the
    post-expansion action total is bounded).
  - **Schedules**: a runnable-action schedule takes a `group` field as an
    alternative to `host` (exactly one of the two), fanning out autonomously
    (pre-authorised, one run card per host).
  - **Skills**: inherit group support — a runbook step can name a group; the agent
    passes it as the `host`.
  - Resolver is the single source of truth `hosts.group_members` / `groups_map`
    (bridge wrappers `_group_members` / `_groups_map`), reused everywhere.
- **Canonical host-group registry (`runspec_groups.toml`).** Group names are no
  longer free-form: a new synced registry file (`groups.py`; added to Config Sync
  `KNOWN_FILES` + `COLLECTION_FILES`, merged by `name`) is the controlled
  vocabulary the host editor's **required group dropdown** is populated from, so a
  group referenced by a shared runbook resolves to the same group on every
  console. Membership still lives on the host's `group` field (also synced);
  "Ungrouped" is an explicit choice. The registry is self-healing — `get_groups`
  absorbs any in-use host group not yet registered (migrates pre-registry
  free-form groups). A Settings → Jump Hosts → **Manage groups** dialog
  adds/renames/deletes groups (`save_groups` resets orphaned hosts to Ungrouped;
  `rename_group` re-points member hosts).
- **Sidebar host groups are now collapsible.** Each group header toggles
  (chevron, state persisted in `localStorage`); collapsed it shows a green
  connected count and a grey disconnected count. The ungrouped "Hosts" section
  collapses the same way.


## [0.119.0] — 2026-06-23

### Changed
- **The Speak / Listening… affordance now sits *inside* the prompt box**, just
  above the `❯` input row, rather than as a separate strip above the bordered
  composer. Purely a layout move — the bordered box now wraps the voice strip and
  the input row as one unit. No behaviour change.


## [0.118.0] — 2026-06-23

### Changed
- **Speak button now toggles Windows Voice access listening with the correct
  chord (Alt+Shift+B), wake-on-click / sleep-on-send.** The previous chord
  (Ctrl+Win+S) actually opened legacy Windows Speech Recognition; the documented
  Voice access wake/sleep shortcut is **Alt+Shift+B**. Clicking **Speak** now
  wakes Voice access and focuses the prompt box (the button shows *Listening…* in
  red); sending — or a second click — puts it back to sleep. The same chord
  toggles both ways, so the UI tracks the state and presses it to flip
  (`winkeys.VOICE_ACCESS_TOGGLE`, still a one-line change if a build differs;
  Alt+Shift+C is mic on/off). New `stop_voice_dictation()` bridge method
  alongside `start_voice_dictation()`; both inject the toggle via `SendInput`.
  Still no new dependency, still fail-soft (no-op off Windows / on failure).


## [0.117.0] — 2026-06-23

### Changed
- **The composer's Speak button now *starts* Voice access, not just focuses the
  box.** Clicking it focuses the prompt `<textarea>` and asks the host to inject
  Voice access's start chord (**Ctrl+Win+S**) via `SendInput`, so one click both
  begins listening and aims dictation at the prompt box. JavaScript in the
  WebView2 sandbox can't synthesize a Windows-key hotkey, so this goes through a
  new `start_voice_dictation()` bridge method backed by `winkeys.py` (ctypes
  `SendInput`, no new dependency). Fail-soft: a no-op off Windows / on failure —
  the operator can still start Voice access from its own bar, and the box is
  focused either way. The start chord is a single named constant
  (`winkeys.VOICE_ACCESS_HOTKEY`) so it's a one-line change if a build documents a
  different combo. Nothing is sent until you press Enter (unchanged).


## [0.116.0] — 2026-06-23

### Added
- **Voice input affordance in the chat composer.** A small **Speak** mic button
  now sits just above the `❯` prompt row. Clicking it focuses the composer so
  **Windows 11 Voice access** dictates straight into the prompt box, with a
  one-time inline hint for discoverability and an explanatory tooltip. Voice
  input is therefore a pure input convenience — nothing is sent until you press
  Enter, so it stays below the existing autonomy/confirm gates.
- This is deliberately **engine-free**: the console adds **no** speech
  dependency and drives no recognizer. It leans on Voice access — the on-device,
  policy-permitted Windows dictation tool (where Win+H/online is often blocked by
  org policy) — which types into whatever editable control has focus. Because the
  console renders in WebView2 (Edge/Chromium) and the composer is a standard
  `<textarea>`, dictation lands in it directly. See
  `docs/design/console-voice-input.md`.


## [0.115.0] — 2026-06-23

### Added
- **Watch more than one mailbox: a primary `[mailbox]` plus watch-only
  `[[watch]]` extras.** The Outlook source can now monitor additional
  mailboxes/folders for `new_mail` alongside the primary one — e.g. your own
  Inbox next to a shared helpdesk mailbox. Each `[[watch]]` entry is
  `{address, folder, label}` with the same normalisation as `[mailbox]`
  (blank `address` = your default account; blank/`"inbox"` folder = the Inbox),
  and is deduped against the primary so a repeat doesn't double-bind.
  - **The extras are deliberately watch-only — they carry no room/helpdesk
    role.** The room `hello` handshake and the email-verify "send-as" identity
    stay tied to the primary `[mailbox]` alone, so the *shared* mailbox stays the
    one used for the runspec-room connection while your personal Inbox is just
    another `new_mail` feed. (Reply suppression is per-trigger on extras — no
    per-watch `once_per_thread`.)
  - `config.read_watches()` / `write_watches()`, bridge `get_watches` /
    `save_watches`, and a **Settings → Mailbox → "Also watch"** editor (add/remove
    rows). The trigger engine starts the Outlook source whenever a primary mailbox
    *or* any extra is configured. The agent's Outlook tools' prompt names the
    extra mailboxes so it can read them by passing `mailbox`/`folder`.


## [0.114.0] — 2026-06-23

### Changed
- **Credentials: forms no longer copy secrets into arguments — they set
  name-derived environment variables instead.** A credential applied to a run
  from the Forms run dialog is now injected as plain env vars whose names derive
  from its label + kind by a strict convention — a credential labelled `Jira`
  (username + password) sets `JIRA_USERNAME`/`JIRA_PASSWORD`, `Jira Dev` sets
  `JIRA_DEV_USERNAME`/`JIRA_DEV_PASSWORD`, a token credential `Deploy` sets
  `DEPLOY_TOKEN`. Name a credential to match what the runnable reads from the
  environment. **Multiple credentials per run** are supported (a multi-select).
  This replaces the old single-credential, arg-binding behaviour on the form
  path (`username_arg`/`secret_arg`/scope auto-fill); those bindings now drive
  **agent** runs only.
- **Forms block required arguments — including `password`-typed ones.** Because
  credentials are no longer copied into args for humans, a required arg (secret
  or not) must be typed in the form. For reusable credentials, read them as
  environment variables (above) instead of declaring a required `password` arg.
- **The agent invoke path also injects the name-derived env vars** (in-scope
  credentials plus an explicit `credential_id`), on top of keeping its existing
  arg-binding fill — so env-only runnables work when the agent runs them.
- **Run form remembers your credential picks per runnable.** The selected
  credentials are stored locally per runnable name (across hosts) — only the
  credential *ids*, never secrets — and prefilled next time (stale ids are
  dropped). The Settings → Credentials editor previews the env var names a
  credential will set as you type its label.

### Notes
- Credentials are carried through `sudo`/`su` (`env(1)`) for `run_as` runnables,
  the same as the secret channel. Most sudo runnables take no credential at all
  (passwordless sudo), so they are unaffected.
- **Known gap:** under the strict naming convention a credential named `Jsm`
  emits `JSM_USERNAME`/`JSM_PASSWORD`/`JSM_TOKEN`, but the shipped `runspec-jsm`
  reads `JSM_USER`/`JSM_PAT` and `runspec-snow` reads `SNOW_USER`/`SNOW_TOKEN`;
  those packs need their env reads aligned to be wired by a conventionally-named
  credential (tracked as a follow-up).


## [0.113.0] — 2026-06-23

### Fixed
- **Forms: subcommands chip no longer squeezes the card title.** On a runnable
  card with subcommands, the `N subcommands · required` chip sat in the header
  next to the title and pushed the runnable name into a narrow left column. The
  chip now lives in the bottom tag row alongside the host and arg-count chips,
  and the card grid's minimum column width was bumped `210px → 250px` so cards
  have a little more room.


## [0.112.0] — 2026-06-22

### Fixed
- **Path browser: large directories are now browsable.** The Forms path-picker
  *Browse* modal (`PathInput`) gained a **filter-by-name box** and caps the rows
  it actually renders (first 300 of the filtered set, with a "refine the filter"
  hint). A directory like `/usr/bin` (~1k+ entries) previously rendered as one
  unfiltered, un-virtualised list and was effectively unusable — you couldn't
  type to find the binary you wanted (e.g. a `python3` interpreter for
  `create-venv`), so it appeared you "couldn't browse" it. The backend listing
  cap (`fs_browse.MAX_ENTRIES`) was also raised from 1000 to 5000 so common
  system dirs list in full rather than silently truncating.


## [0.111.0] — 2026-06-22

### Added
- **Richer trigger actions (Outlook / documents / schedules / skills) + bounded
  multi-runnable rules.** A trigger can now do far more than run one runnable or a
  free-form agent turn:
  - **New built-in agent action tools**, available to any agent-mode trigger (and
    chat): `create_document` (write a markdown doc locally and/or upload it to a
    fleet host), `run_skill` (follow a saved runbook *inline* — rendered, not a
    spawned sub-agent), `create_schedule` (add a cron/interval schedule), and
    `emit_event` (fire a matchable event into the engine for fan-out). All four are
    **confirm-gated**. The six Outlook Classic COM tools (read inbox/search/
    calendar/reminders + draft/send) were already reachable in a trigger turn and
    are now surfaced per-trigger in the editor.
  - **Per-trigger action allowlist** (`action_allow`): an agent trigger can be
    scoped to just the built-in action/Outlook tools it's meant to use (empty =
    all available), alongside the existing runnable allowlist.
  - **Declarative multi-runnable rule actions** (`actions` + `action_mode`):
    a `mode = "rule"` trigger can run **several** runnables `sequential` or
    `parallel`, instead of a single `run`. A parallel batch is approved by **one
    grouped confirm** (most-restrictive autonomy across the steps; any `manual`
    step refuses the group). Bounded by design — a flat list (cap 10), no
    branching, no step-result piping — so it stays under the "no workflow engine"
    line in `docs/design/console-triggers.md`.
  - **Optional LLM pre-turn** (`pre_turn`) ahead of a rule action: one headless
    extract step (no runnable tools, can't loop or branch) whose output is exposed
    to the action arg maps under `{{ _llm.<name> }}`.
- **Editable confirmations (global).** At the autonomy gate the operator can now
  **edit a tool call's arguments before approving** — the edited values run
  instead of the agent's proposal. Applies to every gated tool call (chat, agent,
  trigger) and to a draft/propose reply's body; the search-mode `run_runnable`
  wrapper stays read-only (its display args would re-wrap lossily).
  `resolve_tool_confirmation` gained an optional `edited_input` argument.


## [0.110.0] — 2026-06-22

### Added
- **Lock auto-suspend (presence governance control).** Reactive triggers no
  longer fire while the operator's workstation is **locked** — so a forgotten,
  unattended console can't take autonomous action. A Rota-tab toggle ("Pause
  reactive triggers while my screen is locked") with a **live presence indicator**
  (unlocked / locked / unknown), plus the chat agent's context notes it.
  - **Default ON** (absent `lock_suspend` ⇒ enabled) — safe-by-default, matching
    the library's autonomy stance.
  - **Fail-closed when on:** an *uncertain* probe (`unknown`) counts as locked, so
    an unreadable lock state can't let automation run unattended. Non-Windows
    reads `unlocked`, so the gate is a no-op there.
  - **Absolute:** unlike the working-hours rota, `ignore_rota` does **not** bypass
    it — there is no per-trigger override.
  - **Scheduled jobs are exempt:** schedule-sourced events (e.g. a 2am batch job)
    still run while locked — they're pre-authorised, not reactions-while-away.
  - A suppressed fire is logged (`trigger '…' skipped: session locked`) and lands
    in the per-event audit record — the evidence the control fired.
  - New tri-state `winlock.screen_lock_state()` (builds on `is_screen_locked()`);
    bridge `_build_lock_ok()` + `screen_state()`; engine gains a `lock_ok` gate.

### Changed
- **`create_reminder` is now profile-robust.** It tries a calendar **appointment**,
  then a **task**, then a **flagged to-do mail** — so the "Take a break" reminder
  works on **IMAP/POP** Outlook profiles whose default store has no Calendar/Tasks
  folder (where `CreateItem(1)`/`(3)` fail but mail works). Returns a `kind`; the
  successful method (after any fallback) is logged. Still best-effort.


## [0.109.0] — 2026-06-21

### Added
- **Rota "Take a break" button.** On the Rota tab, pick minutes (presets 15/30/60
  + custom, default 60; today's shift end is shown — you may break past it) and
  the rota suspends, then **auto-resumes** at the return time so automation can't
  be left off by accident. A best-effort **Outlook reminder** is set for the
  return time as a nudge. Disabled when the rota is off; while on a break the tab
  shows "On break until HH:MM" with an **End break now** button.
  - **Honours the lock screen:** if the screen is still locked when the break
    elapses, automation stays paused until you unlock (you're clearly still away).
    Backed by a new minimal `winlock.is_screen_locked()` (Windows `OpenInputDesktop`,
    best-effort; `False`/plain auto-resume elsewhere).
  - Implemented in-memory on the bridge (`_break_until` + `start_break`/`cancel_break`);
    the rota gate (`_build_clock_ok`) honours it **live** and self-clears — no timer,
    no persisted state, no engine restart. The chat agent's per-turn context
    (`_console_context`) notes an active break.
  - New Outlook COM helper `create_reminder` (an appointment with a reminder) —
    the file's first *create* calendar item; reading reminders + draft/send already
    existed.

The broader **lock auto-suspend** feature (an always-on opt-in "pause while my
screen is locked" with an event-driven listener) remains a planned follow-up; this
change ships only the minimal lock check the break needs.


## [0.108.0] — 2026-06-21

### Fixed
- **The Forms path picker can now select a *folder*, not just a file.** In the
  browse modal's single-select mode the "Use this folder" action is always
  available (previously only when the arg was `ui = "directory"`), so a generic
  `path` arg that accepts a file *or* a folder (e.g. `install-into-venv --project`)
  can finally pick a directory — in file mode you could otherwise only click a
  file or descend into folders. A `ui = "directory"` arg now also titles the modal
  "Select a folder · <host>" and the form field tags it **folder** (not `path`),
  so it's clear the arg wants a directory (e.g. `install-into-venv --venv`, which
  points at the venv folder, not the `bin/python` inside it). Backend folder
  listing (`dirs_only`) was already supported; this is the missing UI affordance.


## [0.107.0] — 2026-06-21

### Added
- **The chat agent can use Outlook Classic (desktop COM).** Six built-in agent
  tools, available only when Outlook COM is reachable (Windows + pywin32), riding
  the desktop's signed-in session so they work where the Microsoft Graph
  device-code flow is blocked:
  - **Read-only (autonomous, no prompt):** `outlook_inbox` (recent/unread mail in
    the monitored or default folder), `outlook_search` (subject/body text),
    `outlook_calendar` (upcoming events over N days), `outlook_reminders`.
  - **Write (confirm-gated):** `outlook_draft` (save a new mail or a reply — with
    the original quoted — to Drafts) and `outlook_send` (send a saved draft or
    compose+send). `from_mailbox` sends as a shared mailbox. **Send is
    confirm-gated**, so the operator approves each one.

  Built on the console's existing Outlook COM (`sources/outlook.py`) — new
  helpers `list_messages` / `search_messages` / `upcoming_events` /
  `list_reminders` / `save_new_draft` / `send_new_mail` plus the bridge wiring
  (`_outlook_tools` / `_run_outlook_tool`, `_OUTLOOK_READ_TOOLS` autonomous /
  `_OUTLOOK_WRITE_TOOLS` confirm).
- **The agent is now aware of the monitored mailbox.** `_console_context()` (the
  per-turn awareness block) names the monitored mailbox/folder and the available
  `outlook_*` tools when Outlook is reachable — so the agent knows it can read,
  search, draft and send, and `console_status` reports it too. Emits nothing when
  no mailbox is configured (keeps the prompt-cached fast path).

This shifts Outlook usage for the agent fully onto Outlook Classic. The
`runspec-windows[graph]` Outlook/Calendar runnables are now redundant; retiring
them (and dropping the `[graph]` extra, which still backs the Teams/OneDrive
Graph runnables) is a planned follow-up.


## [0.106.1] — 2026-06-21

### Changed
- **Docs link now points at `https://runspec.app/`.** `DEFAULT_DOCS_URL` (the
  in-app "Documentation" target, still overridable via `[console] docs_url`)
  follows the docs site's move to the owned domain. README updated to match.


## [0.106.0] — 2026-06-21

### Added
- **Denylist fleet kill switch — Settings tab + agent tool.** A new
  **Settings → Denylist** tab (host picker, *Disable all* / *Re-enable all*
  danger buttons, an add-rule form with runnable/user/host globs + reason, and an
  enforce/warn/off mode toggle), plus a built-in agent tool
  (confirm-gated, like `download_file`/`relay_file`) that disables or
  re-enables runspec runnables across fleet hosts by running `runspec deny`
  (`list`/`add`/`all`/`clear`/`mode`) over each host's existing SSH connection,
  once per venv (`runspec_paths`). `targets` selects hosts (omit ⇒ all); `add`
  takes `runnable`/`user`/`host` globs + `reason`. It disables the *runnables* on
  those hosts, not the console, and because it shells out to the core `runspec`
  binary (exempt from its own denylist) `clear` always re-enables — the operator
  can never be locked out. Pairs with the core denylist (runspec 0.43.0); remote
  hosts need `runspec >= 0.43.0` for the tool to take effect. See
  `runspec_console/bridge.py` (`_denylist_tool`, `_run_manage_denylist`).
  - **Live status table.** The tab shows current state per host/venv (mode,
    what's disabled, rule detail) via a new `denylist_status(targets)` bridge
    method that parses `runspec deny list --json`; it refreshes after every
    action.
  - **Agent stays aware.** The agent tool's read-only `action='list'` is now
    **autonomous** (no confirm prompt) so the agent can consult what's disabled
    at any time — e.g. when a run is unexpectedly refused — while the mutating
    actions remain confirm-gated. The read-only `console_status` tool (0.105.0)
    also carries a zero-cost pointer to `manage_denylist` (no fleet sweep) so the
    kill switch is discoverable from the one place the agent checks console state.


## [0.105.0] — 2026-06-21

### Added
- **The chat agent is now aware of the working-hours rota and the configured
  triggers/schedules.** Each interactive turn folds a compact "console context"
  block into the system prompt (via the same per-turn `system_override` path as
  pinned memory, so the prompt-cached tool prefix doesn't churn): whether the rota
  is on and whether it's **currently inside or outside** working hours, plus the
  list of configured triggers (`source:event`, mode) and schedules (when +
  target). So the agent can reason about them and, when asked to run/schedule/
  trigger something **outside** working hours, flag that and confirm before acting
  rather than running blindly. An unconfigured console (rota off, no triggers/
  schedules) emits no block and keeps the fully prompt-cached fast path.
- **`console_status` agent tool.** A read-only, autonomous (no confirm gate) tool
  the agent can call on demand to re-check the rota (and whether it's currently
  inside/outside working hours) plus the configured triggers/schedules — useful in
  a long session where the clock may have crossed a working-hours boundary since
  the turn began. Returns the same snapshot as the per-turn context block.


## [0.104.0] — 2026-06-21

### Changed
- **The working-hours rota moved to its own `runspec_rota.toml`.** It now gates
  **both** triggers and schedules, so it no longer belongs inside
  `runspec_triggers.toml`. The move is transparent: `read_rota` falls back to the
  legacy `[rota]` in `runspec_triggers.toml`, and the next save of either the rota
  or a trigger migrates it across (and strips the stale `[rota]`). Like the mailbox
  folder, the rota is machine/person-local and is **not** synced by Config Sync.

### Fixed
- **Rota tab showed defaults on restart even when working hours were saved.** The
  rota *was* persisted and read back fine — but the Rota tab's panel is open by
  default, so the working-hours editor mounted with an empty rota and froze its
  initial field values to the defaults before `list_triggers()` returned. The
  editor now waits for the first load and re-seeds from disk on every (re)load, so
  saved hours appear after a restart.

## [0.103.0] — 2026-06-21

The console is a **user tool, not an always-on automation system** — so both
triggers and schedules are now governed by one shared working-hours rota, and the
Schedules tab gains the Triggers tab's full run surface.

### Changed
- **Working hours moved to its own Rota tab.** The working-hours editor that lived
  inside the Triggers tab is now a first-class **Rota** tab — the single shared
  gate for *both* triggers and schedules. Same editor and behaviour (console grid
  or Outlook calendar source). (Shipped first as the `0.103.0.dev1` pre-release.)
- **Trigger editing moved onto the Triggers tab.** The create/edit/delete editor
  that lived under *Settings → Triggers* is now the Triggers tab's collapsible
  "Triggers" bar; the Settings → Triggers sub-tab was removed (Mailbox stays).
- **The run view is a "run inbox", not a "kanban board".** Docs/comments reworded
  to match what the UI has always rendered (a list, not columns). No behaviour or
  wire-format change (`column`/`runspec:trigger_card`/`get_trigger_board` unchanged).

### Added
- **Schedules now mirror the Triggers tab.** The schedules table sits in a
  collapsible bar, and below it a **run inbox** + detail drawer shows every
  runnable-action schedule run (live status, output, timeline, Stop), plus an
  **Event log** of every scheduled fire and its disposition — reusing the same
  components as the Triggers tab. A nav badge shows while a schedule run is in
  flight.
- **Schedules honour the working-hours rota.** A scheduled fire outside working
  hours is skipped (dropped, not queued) and recorded as "skipped (working hours)"
  in the Schedules event log — exactly like a trigger. A per-schedule
  `ignore_rota` opts a schedule out; *Run now* always fires (explicit
  authorization). Backed by `ScheduleSource` gaining a `clock_ok` gate and an
  `on_event` audit hook, and the bridge's `get_schedule_board` /
  `get_schedule_events` feeds.
- **"Trigger now" on each trigger row.** Fire a trigger immediately with a
  synthetic event — a pasted message (under the `body` field) plus key=value
  fields — to test it without waiting for a real event. Bypasses match/rota/dedup
  for the chosen trigger (`TriggerEngine.fire_test` / bridge `test_trigger`); the
  run appears in the Triggers run inbox.

## [0.102.2] — 2026-06-20

### Fixed
- **An in-scope credential could bleed a flag into an argument-less runnable.**
  0.102.1 changed the credential auto-apply guard to treat an *empty* `valid_args`
  set as "no restriction" — but a runnable that genuinely declares **no args**
  (e.g. `upgrade-packages`) has an empty set, so an in-scope credential's
  `username_arg`/`secret_arg` would inject e.g. `--user <name>` into a runnable
  that doesn't accept it, failing the run. Reverted: an empty declared-args set
  again blocks auto-injection (there are no args to fill). An explicitly named
  `credential_id` still applies regardless, and the lone-`password` fallback (the
  actual 0.102.1 fix) is unaffected.

## [0.102.1] — 2026-06-20

### Fixed
- **A credential's secret now reaches a runnable's lone `password` arg without a
  `secret_arg` binding** — fixes the agent (and form) still failing with `Missing
  required argument: --password` even with a credential applied. Previously
  `_apply_credentials` injected a secret only when the credential's `secret_arg`
  named the arg, so a credential set up for a relay (secret referenced by id, no
  `secret_arg`) had nothing to bind. Now a credential applied to a run (an
  explicit `credential_id`, or the single in-scope one) fills the runnable's sole
  still-unset `password`-typed arg from its stored secret — matching how the relay
  picker already works. The `secret_arg` binding still takes precedence; an
  ambiguous case (two in-scope credentials, none explicit) is skipped; a typed
  value is never overwritten.
- **Auto-applied credentials no longer silently skipped when the arg cache is
  empty.** The auto-apply guard treated an *empty* `valid_args` set (a missing
  discovery cache) as "no declared args", blocking all injection — this could hit
  the agent path while the form path (full cache) worked. An empty/None set now
  imposes no restriction.

## [0.102.0] — 2026-06-20

### Added
- **Schedules tab — edit, richer rows, and Run now.** A scheduled entry is no
  longer create-or-delete only:
  - **Edit** — the row's pencil opens the same dialog pre-filled with the
    schedule (action, cron preset/custom or interval, timezone, host/runnable +
    args, or event + fields); saving replaces it in place via a new
    `update_schedule(id, data)` bridge method that keeps the id stable.
  - **More detail in the row** — an inline enable/disable **Switch** (persists
    via `update_schedule`, dims the row when paused), an inline summary of the
    first couple of args/fields (`--key=value`, with a `+N` overflow; the full
    set stays in the expandable row), and a real **Next run** — `get_schedules`
    now attaches a computed `nextRun` (next fire time from the `when`, via
    APScheduler) shown as a relative countdown with the absolute time on hover.
    `nextRun` is display-only and never written back to `runspec_schedules.toml`.
  - **Run now** — the play button fires a schedule immediately regardless of its
    enabled flag via `run_schedule_now(id)`: a runnable-action schedule runs
    directly (same audited path, "Scheduled Task" operator) and an event-action
    schedule emits its matchable event into the trigger engine, fanning out to
    `source="schedule"` triggers.

## [0.101.1] — 2026-06-20

### Fixed
- **Run form still blocked on a required `password` arg.** The 0.101.0 fix only
  cleared the required-field block when the form could *prove* a credential
  covered the arg (in-scope, `secret_arg` set, secret stored). If any of that
  wasn't exactly so — or a value arrived via an `env`/`$VAR` alias the form can't
  see — it still reported "Required". A `password` arg is now **never** blocked by
  the form: its value always arrives out-of-band (a bound credential's secret, an
  env alias, or the masked field), and the form can't validate a secret anyway.
  The field shows a neutral "secret · optional here" tag instead of "required";
  if no secret is supplied anywhere the runnable fails at run time with its own
  error, exactly as a wrong secret would. (Non-secret required args are unchanged,
  and the credential-coverage "from credential" tag still applies.)

## [0.101.0] — 2026-06-20

### Fixed
- **Saved credentials couldn't fill a *required* secret arg.** A runnable with a
  required `password`-typed arg (e.g. `enable-passwordless-sudo`) couldn't be run
  with a bound credential: the run form blocked on the empty field, and the agent
  either had to invent a value (→ auth failure) or was blocked by the required
  schema. The secret was actually injected from the keyring at run time — only the
  pre-flight checks were credential-blind.
  - **Agent:** the tool schema now omits `password`-typed args entirely (and never
    marks them required) — same rule as the visitor catalog and the core's
    `--format mcp` emit. The agent never sees, types, or guesses a secret; an
    in-scope credential auto-applies and `credential_id` picks a specific one.
  - **Run form:** `credentials_for_runnable` now returns each in-scope
    credential's bound arg names (`secret_arg`/`username_arg`, plus non-secret
    `username`), so the form treats a required arg a credential will fill as
    satisfied — mirroring the backend's auto-apply + ambiguity rules. The covered
    field shows a "from credential" tag and can be left blank.

## [0.100.0] — 2026-06-20

### Changed
- **Config Sync backups are now bounded.** A pull used to copy every targeted
  config file to `<name>.bak-<timestamp>` unconditionally and never clean up, so
  repeated pulls left a growing pile of (often identical) backups in the config
  folder. Now: (1) a file the pull doesn't actually change gets **no backup**, so a
  re-pull at the same ref leaves none; and (2) only the most recent
  `[config_sync] backups_keep` (default **5**; `0` = keep all) backups per file are
  retained — the oldest are pruned after each pull. The Config Sync tab gains a
  "Backups to keep per file" field, and the Pull-now result reports how many old
  backups were pruned. `sync_pull` now also returns a `pruned` list.

## [0.99.2] — 2026-06-20

### Fixed
- **Settings → Skills now refreshes after a Config Sync pull.** The Skills panel
  loaded once when the drawer opened and never re-read, so skills merged in by a
  pull didn't appear until the drawer was reopened or the console restarted — even
  though the pull dispatches `runspec:skills_changed` (the `$`-menu already
  listened). The panel now subscribes to that event and reloads. This also closes
  a data-loss path: because the panel held a stale in-memory list, adding a skill
  from the drawer (a whole-collection save) could overwrite skills pulled in since
  the drawer opened; keeping the list current prevents the clobber.

## [0.99.1] — 2026-06-20

### Added
- **Runnable catalogue in the runbook docs.** The generated wiki now publishes the
  console's cached discovery catalogue: `runnables.md` (per-host index) and
  `runnables/<host>.md` (every runnable's description, autonomy, run-as, arguments,
  and subcommands), drawn from `get_runnables("all")` so it publishes offline from
  the last sync. `README.md` gains a Runnables section and `hosts.md` a
  runnable-count column linking each host's page — so the docs are a real reference
  even with no skills defined.
- **Default "regenerate docs" skill embedded in the docs.** `maintaining-docs.md`
  carries a paste-able `[[skill]]` block for a "Regenerate the runbook docs"
  runbook, so whoever uploads the docs can drop it into the repo's
  `runspec_skills.toml` and give the whole team a one-click refresh after the next
  Config Sync pull.
- **Browse buttons** on the SSH-key credential's private/public key path fields
  (Settings → Credentials), via a native file picker (`browse_key_file`, mirroring
  `browse_ssh_binary`).

### Changed
- **Config Sync folder selection is now one field.** The separate "Subdirectory"
  and "Overlay layers" inputs are merged into a single **"Folder(s) in the repo"**
  field: blank = repo root, one folder = read just that folder, several = a shared
  base + your team's folder stacked in order. Saving writes `layers` and clears the
  legacy `subpath` (one source of truth); an existing single `subpath` shows as the
  one entry on load. No behaviour change — the backend already preferred the layer
  list.


## [0.99.0] — 2026-06-20

### Added
- **Shared base config via ordered Config Sync layers.** `[config_sync]` gains a
  `layers` list of repo subdirectories overlaid low→high (e.g. `["_base",
  "regions/emea", "teams/emea-pay"]`), so teams share a common base and overlay
  their own flavour instead of each folder duplicating it. A pull fetches the ref
  **once** (`gitsync.fetch_layers`) and reads every layer from the same commit
  tree, then folds them per file (`configmerge.fold_layers`: mappings deep-overlay,
  collections merge by identity) into one effective table before the existing
  non-destructive merge onto local — so `preserve` and local-only entries still
  win over the whole stack. Empty `layers` falls back to the single `subpath`
  (unchanged behaviour). New "Overlay layers" field in Settings → Config Sync.
- **Runbook docs — a deterministic markdown wiki** (Settings → Config Sync →
  Runbook docs; `runspec_console/docsgen.py`). Generates a human-followable wiki
  from the local skills + fleet — an index grouped by tag, one page per skill
  (when-to-use, steps, params, and cross-links to the runnables/hosts the steps
  name), and a hosts page — so during an outage a person can SSH in and run the
  same runnables the agent would. Output is **byte-stable** (sorted, no
  timestamps), so re-running only changes what the skills changed and the files
  diff cleanly when committed back to the config repo. New `generate_docs` /
  `get_docs_config` / `save_docs_config` bridge methods and a `[docs] dir` setting
  (defaults to `app_dir()/docs`; point it at a local clone of the config repo to
  commit the docs back for the team).

### Notes
- Config Sync stays **pull-only** with no working tree, so the generated docs are
  committed back by the operator from their own clone — the console never pushes.


## [0.98.0] — 2026-06-20

### Added
- **SSH-key credentials** (Settings → Credentials). A new credential `kind =
  "ssh-key"` references an SSH key pair **by file path** (`private_key` +
  optional `public_key`); the key material stays on disk and only an optional
  key **passphrase** is stored in the OS keychain. This lets multiple SSH
  identities coexist — e.g. a dedicated GitHub **deploy key** per repo — rather
  than the single console-wide `[ssh] identityFile`.
- **Config Sync uses an SSH-key credential's deploy key.** For an SSH repo URL,
  `_sync_auth` now prefers the selected credential's `private_key` (and its
  keyring passphrase) and falls back to `[ssh] identityFile` when no key-bearing
  credential is chosen. `GitAuth` gains a `passphrase` field; the paramiko vendor
  decrypts the key file with it (bound into the vendor factory, since dulwich's
  `SSHVendor` signature has no passphrase parameter). SSH key paths are
  `~`-expanded before they reach paramiko.

### Notes
- SSH-key credential metadata (`private_key` / `public_key` paths) rides Config
  Sync's non-destructive credential merge like any other credential, but the
  paths are machine-local — protect them with a `preserve` entry if a synced
  repo also defines the same credential id.


## [0.97.0] — 2026-06-19

### Added
- **Skills appear as launchable cards in the Forms tab.** A parameterized skill is
  effectively a form, so the Forms view now shows a **Skills** swimlane above the
  runnable cards — click a card to open the same parameter form the `$` chat menu
  uses (and run it through the assistant). Cards show the skill id, title/symptom,
  param count, and tags; they're host-agnostic (only the text search filters them).
  No backend change — reuses `get_skills` and the existing `SkillRunModal`.


## [0.96.0] — 2026-06-19

### Added
- **Skills — user-invocable, parameterized, shareable runbooks.** The existing
  `runspec_skills.toml` runbooks (agent-authored via `save_skill`/`load_skill`) gain
  a human front door. Type **`$`** in the chat prompt to open a skills menu (parallel
  to `/` for runnables); picking one opens a **parameter form** and runs the rendered
  body through the assistant — orchestrating SSH runnables, file relay, and filters
  the agent already drives. A skill's parameters are a real runspec **`[skill.args]`**
  table (same grammar as `[runnable.args]`): parsed by the core `loader`/`inference`
  and validated by the core `validator`/`types` (one type system, no parallel schema),
  so the run form reuses the Forms-tab `ArgInput` controls. A console-only
  `ui = "host"` hint renders a fleet-host dropdown; `path` params are plain text (a
  skill path is usually remote). The body (`steps`) references params as `{{name}}`.
  Authoring is a new **Settings → Skills** tab (popout modal editor mirroring Jump
  Hosts, with a parameter list). Skills already ride Config Sync, so a team shares one
  curated set. New bridge methods `get_skills` / `save_skills` / `run_skill`;
  `memory.save_skill` now round-trips the `args` table and adds `render_skill_body`;
  new `skillargs` module bridges the console to the core arg machinery.
- **Skill runs are attributed by name in History & Analytics.** A run started from
  the `$` menu is tagged with its skill end-to-end: the bridge threads the skill id
  through the run-origin seam, the executor sets `RUNSPEC_SKILL` on both the local and
  remote run paths, and runspec **0.42.0** records it as `run_summary.extra.skill`
  (a provenance axis mirroring `RUNSPEC_TRIGGER`). History gains a **Skill** column and
  Analytics a **skill** filter + dimension, so runs can be grouped and filtered by the
  skill that drove them. Requires `runspec >= 0.42.0`.


## [0.95.0] — 2026-06-19

### Changed
- **Settings → Filters, Credentials, and Triggers now edit in a pop-out modal**,
  matching the Hosts editor. Editing an entry deep in a long list opens a centered
  dialog instead of an inline form, so you never lose your place. No change to the
  fields or what's saved — only the add/edit surface. (Triggers action-field
  wording is being reworked separately; see `docs/design/trigger-actions-ux.md`.)


## [0.94.0] — 2026-06-19

### Added
- **Run-provenance badges in History, Analytics, and the in-flight strip.** Each
  run now carries the event source that *caused* it (its `trigger`) alongside
  whether it was an LLM turn (`initiatedBy`) — two independent axes — surfaced as
  small icons: a clock 🕐 for a scheduled run and the existing robot 🤖 for an
  LLM/agent run (both show together for a scheduled agent turn). Provenance is
  recorded durably in the runnable's own `run_summary` audit (via the new
  `RUNSPEC_TRIGGER` env that runspec 0.39.0 records as `extra.trigger`), so it
  survives restarts and works across hosts. The console sets it on every run
  path: direct `action="runnable"` schedules (`trigger="schedule"`) and
  event-driven trigger runs (`trigger=<event source>`).

### Changed
- **Rule-mode trigger runs now record `agent=False`.** A `mode="rule"` trigger
  runs a fixed runnable with no LLM involved, but such runs previously flowed
  through the shared agent tool path and were recorded as `agent=True` (showing
  as LLM-initiated in History/Analytics). They are now correctly attributed as
  non-LLM. Agent-mode trigger runs and chat tool calls are unchanged
  (`agent=True`). Requires `runspec>=0.39.0`.


## [0.93.0] — 2026-06-19

### Added
- **Schedules (Schedules tab).** Schedules are now first-class and managed in the
  **Schedules tab** (persisted to `runspec_schedules.toml`), separate from
  triggers: a schedule says *when*, a trigger says *what to do*. Each schedule
  has a `when` (a 5-field `cron` expression or an `every` interval —
  `s`/`m`/`h`/`d` — plus an optional `timezone`, default PC-local) and an
  `action`:
  - `action = "event"` emits a rich, matchable event into the trigger engine —
    so a `source = "schedule"` trigger picks it up by `event` type + `match`
    rules, exactly like mail. **One** scheduled emit can fan out to **many**
    triggers (and a scheduled *agent turn* is just a `mode = "agent"` trigger
    listening for the event). The payload carries the schedule's own `fields`
    plus built-ins (`schedule`, `event`, `fired_at`, `date`/`time`/`weekday`/…).
  - `action = "runnable"` runs a specific runnable on a host with fixed args
    **directly and autonomously** — no agent, no confirm gate — audited as the
    `Scheduled Task` operator.

  The engine is APScheduler, shipped as the new optional `schedule` extra
  (`pip install runspec-console[schedule]`); the schedule source reports itself
  unavailable until installed, and adding/removing a schedule applies live.
  Forward-only for now — a fire that would land while the console is closed is
  skipped (no persistence/catch-up across restarts yet).


## [0.92.0] — 2026-06-19

### Added
- **Settings panel layout — less scrolling, more room.** The Settings drawer is
  wider and its tabs now sit on a left/vertical rail, giving each tab more
  vertical space. The **monitored mailbox** settings moved out of the Triggers
  tab into their own **Mailbox** tab, so Triggers has room to be managed
  comfortably. The Jump Hosts **add/edit form now opens in a modal** instead of
  rendering inline at the top of the tab — editing a host deep in a long list no
  longer requires scrolling back to the top to reach its fields.

### Changed
- **Dropped the hand-maintained in-tree `__version__` constant.** `pyproject.toml`
  is now the single source of truth for the version; `installed_version()` reads
  the installed package metadata and returns `""` from a source checkout (where it
  previously borrowed a constant that silently drifted out of sync). No effect on
  installed users — metadata still reports the real version.

### Fixed
- **LLM adapter keys no longer dropped on Settings save.** `api_key_command` and
  `api_key_ttl_ms` live under the LLM tab's "Advanced" panel; antd's Collapse
  left that panel's fields unmounted until expanded, so saving after editing only
  a top-level field (without opening Advanced) silently deleted those keys from
  `config.toml`. The Advanced panel now force-renders its fields so a hand-edited
  value survives a Save unless its field is explicitly emptied.


## [0.91.1] — 2026-06-18

### Fixed
- **Approve/Deny buttons now show for search-mode agent runs.** When the agent
  calls a runnable via the search-mode `run_runnable` wrapper, the autonomy gate's
  confirm event carried the *unwrapped* runnable name (e.g. `drain-node`) as
  `tool_name`, but the running tool-call card is keyed by the *raw* tool name
  (`run_runnable`). They never matched, so the confirm buttons never rendered and
  the gate could only time out as a deny. The confirm event now sends the raw
  `tool_name`/`tool_input` (so the card matches) plus separate
  `display_name`/`display_input` for the human-readable resolved runnable — which
  the approval panel now actually shows (the unwrapped view previously never
  rendered, since the card reads the segment). Introduced with search tool mode
  (#267); unrelated to the 0.91.0 window changes.


## [0.91.0] — 2026-06-18

### Added
- **Window UX polish**, all via `create_window` options (no native object access):
  - **No white startup flash** — the window now paints `#111111` (the dark-theme
    content colour) before the SPA mounts (`background_color`).
  - **Accidental-close guard** — a native "are you sure?" prompt on close
    (`confirm_close`) so an in-progress agent run or chat isn't dropped by a stray
    Ctrl+W / window-X.
  - **Select & copy arbitrary text** — page text is now selectable everywhere, not
    just inputs/outputs (`text_select`).
  - **Zoom** — Ctrl+scroll / pinch zooms the whole UI (`zoomable`) for readability.


## [0.90.3] — 2026-06-18

### Changed
- **Right-click menu + spell-check now use pywebview's own settings instead of
  walking the WebView2 control — fixes the intermittent startup hang.** The
  0.84.0–0.90.2 approach located the live `CoreWebView2` and flipped
  `AreDefaultContextMenusEnabled` from pywebview's background `started` thread.
  But those WebView2/WinForms objects are UI-thread-affine: off-thread access
  *throws* when the control isn't ready (the safe path — it bailed and the window
  loaded) but *blocks* when the control is mid-initialisation, deadlocking the
  window at startup. That race is why it was intermittent, and no amount of
  guarding the walk could fix it. pywebview already sets
  `CoreWebView2.Settings.AreDefaultContextMenusEnabled = debug` itself, on the UI
  thread at init, and opens the devtools *window* only when the separate
  `OPEN_DEVTOOLS_IN_DEBUG` setting is also true. So the console now runs pywebview
  in debug mode (menu + spellcheck on) while pinning `OPEN_DEVTOOLS_IN_DEBUG` to
  the user's `--dev`/`--devtools` choice — the menu comes on without the inspector
  popping open, and there is no background-thread access left that can hang.
  `PYWEBVIEW_LOG` is pinned to `WARNING` so debug mode doesn't make pywebview log
  at DEBUG. Removes `_find_core_webview2` / `_safe_getattr` and the object-graph
  walk entirely.


## [0.90.2] — 2026-06-18

### Fixed
- **Window no longer hangs ("Not Responding") at startup.** A follow-up to the
  0.90.1 fix. Once `_safe_getattr` stopped a throwing .NET property from aborting
  the WebView2 object-graph walk, the walk could run forever: the WinForms graph
  has parent/owner back-references, and pythonnet returns a *fresh proxy wrapper*
  on every attribute access, so the `id()`-keyed `seen` set never deduped real
  .NET nodes. The unbounded BFS spun the startup background thread on cross-thread
  property reads and kept the UI thread from pumping messages — a permanent hang
  with no logged error. `_find_core_webview2` is now hard-bounded by `max_nodes`
  and `max_depth`, so it always terminates regardless of proxy identity (the real
  control sits only a few hops deep, well inside the bounds).


## [0.90.1] — 2026-06-18

### Fixed
- **Native right-click menu + spell-check now actually enable on Windows.** The
  WebView2 object-graph walk (`_find_core_webview2`) probed each node with
  `getattr(obj, name, None)`, which only swallows `AttributeError`. Real
  .NET/pythonnet property getters can *throw* — `CoreWebView2` raises
  `System.InvalidCastException` when probed before the control is initialised
  or off its UI thread — so the search aborted on the first throwing node,
  logged the exception at startup, and left the default context menu (and thus
  Cut/Copy/Paste + spell-check suggestions) disabled. All probes now go through
  a `_safe_getattr` helper that treats any failure as "attribute absent", so a
  throwing node is skipped and the walk still reaches a good `CoreWebView2`.


## [0.90.0] — 2026-06-18

### Added
- **Persistent agent memory + curated runbooks** — the agent now accrues knowledge
  across runs so repeat application-support queries get cheaper, without bloating the
  prompt. Four autonomous tools (local + reversible, so no confirm gate):
  - `remember(fact, app?, tags?, pin?)` / `search_memory(query, app?)` — facts persist
    in a local `runspec_memory.toml`, keyed by an `app` tag. Recall is stdlib
    token-overlap (no embedding dependency). **Never synced** — machine-local.
  - `save_skill` / `load_skill` — reusable runbooks (symptom + ordered steps naming
    which runnables to call) in `runspec_skills.toml`, a `[[skill]]` collection that
    **rides Config Sync** (added to `KNOWN_FILES`, merged by `id` like filters) so a
    team ships vetted procedures from one git tag.
- Only the **pinned** memory subset is folded into the system prompt (capped, and an
  empty string when nothing is pinned), injected per turn via the existing
  `system_override` path so the prompt-cached tool/system prefix never churns.
- Pairs with the new `runspec-logops` runnables (`summarize-log` / `map-trace` /
  `bundle-digest`) for cheap, log-driven application-support triage.


## [0.89.0] — 2026-06-17

### Added
- **More LLM + operational settings in the Settings UI.** Several config keys that
  were previously `config.toml`-only are now editable in **Settings**:
  - Settings → LLM: **Tool list mode** dropdown (`dedup` / `per_host` / `search`)
    — so the search layout can be turned on without hand-editing TOML. Plus an
    **Advanced** section for the rotating-token command (`api_key_command` +
    `api_key_ttl_ms`), the LangServe wiring knobs (`auth_header`, `auth_scheme`,
    `input_messages_key`, `input_tools_key`, `tools_in_config`, shown for that
    provider), and optional explicit Bedrock AWS credentials.
  - Settings → SSH → Advanced: **host-key checking** (accept-new / strict), the
    connect / banner / auth **timeouts**, and the discovery **refresh interval +
    jitter** (`[refresh]`).
  - Each field is set-or-clear: leaving it blank removes the key so the documented
    default applies, and any unmanaged `[llm]` key still survives a Save.

### Fixed
- **README** referenced `runspec_config.toml`; the config file is `config.toml`.


## [0.88.0] — 2026-06-17

### Added
- **Always-on catalog index for search mode.** Search mode (`tool_mode = "search"`)
  hid every runnable behind `search_runnables`, so the agent could only find a
  runnable it already thought to search for — it never saw the menu, and a runnable
  whose distinguishing detail lived in its *description* (e.g. one service-desk among
  several) went unfound until named. The agent now gets a compact **index** folded
  into each chat turn: one line per runnable — `id — first sentence of description`,
  deduped by `(group, name)`, sorted, **no schemas** — so it always knows what
  exists, then uses `search_runnables`/`describe_runnable` for arguments and hosts.
  - Cheap and bounded: ~1 short line per runnable (a fraction of the full-schema
    cost), capped by `[llm] tool_index_limit` (default 300) with an overflow note.
    Disable with `[llm] tool_index = false`.
  - Provider-agnostic and cache-friendly: the index is folded into the first user
    message of a per-call copy of the history (the stored history is never touched),
    leaving the cached tools + system prompt prefix intact. Only the manual chat in
    search mode carries it; dedup/per_host (and allow-scoped triggers) are unchanged.


## [0.87.0] — 2026-06-17

### Fixed
- **Right-click menu / spell-check suggestions now locate the WebView2 control
  reliably.** The 0.84.0 context-menu enable guessed a single pywebview→WebView2
  object path (`window.native` / `native.web_view`); if a pywebview version nests
  the control elsewhere (e.g. `native.browser.web_view`) the hook silently found
  nothing and the right-click menu + spelling suggestions stayed off. It now
  breadth-first searches a curated set of child attributes for `CoreWebView2`, so
  it works regardless of the exact path, and — since this is Windows-GUI internals
  that can't be exercised off-Windows — prints a one-line stderr diagnostic (the
  native object's type + attributes) when the control still can't be found, so the
  real path can be pinned from an actual install.


## [0.86.0] — 2026-06-17

### Added
- **Search tool mode (`[llm] tool_mode = "search"`).** Dedup (0.85.0) collapses the
  *host* axis; search collapses the *runnable-breadth* axis. A fleet that installs
  several packages surfaces 70–100+ distinct deduped tools, each carrying a full
  schema in every turn's tool block. Search mode replaces them with **three fixed
  discovery meta-tools** — `search_runnables` (keyword search returning typed,
  structured argument rows), `run_runnable` (execute by `id` + `host`), and
  `describe_runnable` (full schema on demand) — so the per-turn tool block is a
  constant ~3 tools regardless of fleet size (a 12-host × 72-runnable fleet: 864
  tools → 3).
  - **Argument accuracy is preserved, not traded away.** Because every runnable is
    `runspec.toml`-based, search rows carry the *typed* arg contract (enums, ranges,
    defaults, required) — the schema is deferred, not discarded. `run_runnable`
    runs a **client-side pre-flight** (required / choice / int+float / range /
    unknown-arg / require-command) against the cached spec and returns a precise
    correction *before* the SSH round-trip, so a bad call is fixed in the same turn.
    `password`-typed args are omitted from search rows (they flow via
    credentials/env, never inline).
  - Reuses the dedup routing/schema/dispatch seams: `id` is the dedup tool name, so
    resolution, the autonomy gate (keyed to the *inner* runnable `run_runnable`
    resolves to), credentials, secret-splitting, and positionals all flow unchanged.
    The confirm card unwraps to show the real runnable + args. Read-only meta-tools
    run without a prompt.
  - Applies to the unrestricted (manual chat) tool list only; agent-mode triggers
    (already scoped to a curated allowlist) keep the `dedup` layout.
  - Default stays `"dedup"`; `"search"` is opt-in. See `docs/design/agent-tool-search.md`.


## [0.85.0] — 2026-06-17

### Added
- **Deduplicated agent tool list (`[llm] tool_mode`).** A uniform fleet — N hosts
  each running the same ~M runnables — produced N×M agent tools (`host__runnable`),
  the same M definitions repeated once per host and re-sent on every turn, which
  dominated the agent's context budget. The new default `tool_mode = "dedup"`
  collapses these to **one tool per folder/runnable** (`group__runnable`) with a
  **required `host` enum** naming the hosts that have it; the agent picks the host
  as an argument. An 18-host × 20-runnable fleet drops from 360 tools to 20
  (~84% fewer JSON bytes). Set `tool_mode = "per_host"` to restore the original
  one-tool-per-(host, runnable) layout.
  - Trigger allowlists keep their host-scoping: a bare runnable name admits every
    host, while a `host__name` entry narrows the shared tool's `host` enum to that
    host. The credential override enum is the union across the tool's hosts;
    auto-apply still resolves per the actually-selected host at dispatch.
  - Routing (`_resolve_tool_target`) maps a call back to `(host, runnable)` for
    both layouts and accepts an explicit `host__name` even under dedup, so internal
    callers (e.g. the helpdesk portal enact path) and host-pinned trigger calls are
    unaffected. A runnable that declares its own `host` arg keeps it — the injected
    selector falls back to `_host`.


## [0.84.0] — 2026-06-17

### Added
- **Markdown in the trigger approve/deny preview.** When the gated tool is a
  `draft_reply`, the preview the operator approves is the agent's prose reply, so
  it now renders as markdown (matching the Agent output below it). Runnable arg
  previews stay plain key=value text.
- **Copy button on the trigger detail Timeline.** The timeline (per-step audit
  with timestamps) gets a Copy control like the agent-output and approve-preview
  blocks, since right-click copy is unavailable outside webview debug mode.
- **Agent diagnostic trace.** An opt-in trace of exactly what the chat agent
  sends to the model/proxy, to answer "what bytes does the LLM actually
  receive?" independent of how the cards render. When enabled it writes one JSON
  line per event to a file: `tool_result` (the **full** tool-output string the
  model will receive — the card only shows the first 2000 chars) and
  `model_request` (a JSON-safe rendering of the outbound message list, including
  the tool-result content). Enable with `--debug` (writes to
  `{tempdir}/runspec_agent_trace.jsonl`) or set
  `RUNSPEC_AGENT_TRACE=/path/to/trace.jsonl`. Off by default; the writer is
  best-effort and never raises into a chat turn. Lives in the pure `trace`
  module. (Python-only — no UI rebuild needed to use it.)

### Fixed
- **Right-click in the app now works — copy/paste and spell-check suggestions.**
  pywebview disables the WebView2 default context menu unless debug mode is on,
  which removed right-click Cut/Copy/Paste everywhere and the spell-checker
  suggestions for a misspelled word in the prompt box (the red squiggles were
  WebView2's spellcheck, but the *suggestions* only surface through that native
  menu, and there's no JS API to reach them). The console now re-enables the
  WebView2 default context menu on startup (`AreDefaultContextMenusEnabled`),
  best-effort and Windows-only — a silent no-op on any other backend.

### Changed
- **Larger tool-output budget for the chat agent (hardening).** A runnable's
  stdout handed to the model as a tool result was hard-capped at 16 KB, which
  sliced a data-heavy runnable (e.g. a runspec-linux `list-processes` /
  `list-containers` emitting one big compact `json.dumps` line) **mid-JSON** so
  the model got invalid, partial data. The cap is raised to 1 MB (any normal
  structured result passes through whole), and when output genuinely must be cut
  the marker now says so explicitly so the model knows the JSON is incomplete
  rather than trusting a corrupt tail. (This bounds *large* outputs — it is not
  the cause of a small/tiny result not reaching the model.)


## [0.83.0] — 2026-06-17

### Fixed
- **Wheels can no longer ship a stale UI bundle.** The Vite frontend compiles to
  the gitignored `runspec_console/dist/` which is force-included in the wheel —
  but nothing forced a rebuild during a plain `hatchling build` / `python -m
  build`, so a wheel could silently package an **old** UI (e.g. a markdown/UX
  change present in source but missing from the published bundle). A new
  hatchling **build hook** (`hatch_build.py`) now rebuilds the UI into `dist/`
  when the bundle is missing or older than the UI source, so any built wheel
  carries the current frontend regardless of how it was built. It's a no-op when
  the bundle is already fresh (CI builds the UI first, so the hook skips), and
  degrades gracefully when Node isn't available but a bundle already exists. The
  staleness check is a pure, unit-tested mtime comparison.


## [0.80.0] — 2026-06-17

### Fixed
- **Outlook source now reconnects when Outlook restarts.** The COM pump cached
  the Outlook `Application` / namespace / per-folder `Items` proxies for its
  lifetime; restarting Outlook left them pointing at a dead RPC server ("The RPC
  server is not available"), so folder polling failed on every tick, `ItemAdd`
  stopped firing, and the source only recovered when a Settings save restarted
  it. The pump now probes the connection each poll tick and, on a drop, tears the
  sinks down and re-`Dispatch`es Outlook with backoff — self-healing with no
  settings re-save. (Dedupe-by-EntryID means the reconnect's catch-up poll never
  re-emits mail already seen.)


## [0.79.0] — 2026-06-17

### Added
- **Markdown rendering for assistant messages.** The agent's natural-language
  replies in the Console transcript and the Triggers detail drawer now render as
  GitHub-flavored markdown (headings, lists, **bold**, tables, fenced code) via
  `react-markdown` + `remark-gfm`, instead of raw monospace text. Only the
  model's *prose* is rendered as markdown — runnable stdout / JSON / tool output
  stays in the monospace `<pre>` blocks, and a code fence the model emits still
  renders monospace (so quoted JSON looks right). The live streaming tail stays
  plain while a turn is in flight, then reflows to markdown once committed.
- **Abort button on a triggered run.** The Triggers detail drawer now shows a
  **Stop** control while a card is in flight (triggered / triage / running). It
  calls `cancel_chat(card_id)` — a trigger run's `chat_id` is its `card_id`, so
  this trips the run's cancel event and unblocks any parked Approve/Deny gate.
- **Copy buttons** on the Triggers drawer agent-output and approve-prompt
  preview blocks. Right-click copy is suppressed outside webview debug mode;
  these give a deterministic copy path (selectable text still supports Ctrl+C).

### Changed
- The Triggers detail drawer is wider and responsive (`min(820px, 92vw)`,
  was a fixed 560px) so longer agent output and drafts fit.

### Fixed
- **Send reply on a trigger draft no longer throws "This method can't be used
  with an inline response mail item".** `save_reply_draft` built the draft from
  `MailItem.Reply()`, which under Outlook's reading-pane inline replies returns
  an *inline response* item; saving it (and re-opening it by EntryID to send)
  hit that error. The reply is now rebuilt onto a fresh `CreateItem` draft —
  recipients, the "RE:" subject and the quoted original are copied off the reply
  (reading inline-response properties is allowed), but the saved/sent item is a
  plain mail that saves and sends cleanly. Threading-by-subject and the quote
  are preserved.


## [0.78.0] — 2026-06-16

### Added
- **White-labeling from package metadata.** When the console is wrapped in a
  downstream distribution that ships a custom LLM adapter (depends on
  `runspec-console`, advertises a `runspec_console.adapters` provider), the app
  now brands itself to **that** package — no config file or deployment model
  required. Derived from the distribution that supplies the **active** `[llm]
  provider`: the window/page **title** comes from the prettified distribution
  name (`mycorp-console` → "Mycorp Console"), the **documentation** link from a
  `Documentation` (then `Homepage`/`Home-page`) `Project-URL` in its metadata, and
  the **update check** targets that package (`pip index versions <dist>` against
  the venv's configured index). A built-in provider (anthropic/openai/bedrock/
  langserve) or no provider keeps runspec's own identity. Resolution is in a new
  `branding.py` module (`resolve_branding`), is best-effort (any metadata failure
  falls back per-field), and is memoised on the bridge (re-derived on a Settings
  provider change). New `app_title()` bridge method. The existing `[console]
  docs_url` override still wins for the docs link. A wrapper package can also add
  its own `[project.scripts]` for a branded launcher command — see the
  console-adapter-plugin example. (`version.installed_version` / `latest_version`
  now take an optional `package` argument; defaults unchanged.)

---

## [0.77.0] — 2026-06-16

### Added
- **`all` extra.** `pip install runspec-console[all]` (or depending on
  `runspec-console[all]`) pulls every runtime feature extra — `anthropic`,
  `openai`, `bedrock`, `langserve`, `credentials`, `git-sync`, `notifications` —
  in one name, so a downstream internal project doesn't have to enumerate them or
  track new ones. Implemented as a recursive self-referencing extra; excludes
  `dev`. The Windows-only deps inside `notifications` keep their markers, so
  `[all]` installs cleanly on any OS.

## [0.76.0] — 2026-06-16

### Fixed
- **Quiet room reconnects when the room server is stopped.** With a fleet host
  whose room server (`runspec-room`) is down, the SSH tunnel still reaches the
  host, but opening the direct-tcpip channel to the room port fails with
  paramiko's `ChannelException(2, 'Connect failed')`. The reconnect supervisor
  didn't recognise it, so it logged a full ~20-line traceback on *every* backoff
  cycle — flooding the launching terminal. The supervisor now summarises known
  connect failures (room server down, forwarding blocked, slow/again proxied
  connect, timeout) to a single concise line and logs it **once per failure
  state** per room (deduped until the connection succeeds or the error changes);
  only genuinely unexpected errors keep a traceback. The final `"away"` presence
  sent on a dropped channel during teardown is also no longer logged as an
  `ERROR` traceback (it's expected) — it drops to `DEBUG` (visible under `--dev`).
- **Stop the tight reconnect + catalog-republish loop on a rejected room
  connection.** When the room server is up but *rejects* an agent console right
  after its `hello` (e.g. the queue mailbox can't be watched, or an
  identity/mailbox mismatch), the channel opens and immediately closes. The
  supervisor used to treat the open as a success and reset its backoff every time,
  so it spun a ~2s reconnect loop — re-publishing the catalog onto the dying
  socket each pass (the `chat: channel send failed` traceback). The backoff is now
  reset only when a connection stays up past a min-healthy window
  (`_CHAT_MIN_HEALTHY`, 10s); a connection that flaps (opens then drops almost
  immediately) keeps backing off (up to 60s) and logs one concise line naming the
  likely cause, instead of looping. Combined with the `channel send failed`
  → `DEBUG` change above, a misconfigured queue mailbox no longer floods the
  terminal.

## [0.75.1] — 2026-06-16

### Fixed
- **Credential dropdowns refresh live.** Adding (or removing) a credential on
  Settings → Credentials now updates the Config Sync credential picker
  immediately, instead of requiring a console restart. The Settings drawer keeps
  its tab panes mounted, so the Config Sync tab's one-time credential load went
  stale; the Credentials tab now broadcasts a `runspec:credentials-changed` event
  that the Config Sync tab listens for and reloads.

## [0.75.0] — 2026-06-16

### Added
- **Config Sync TLS: CA bundle + proxy support.** A pull from a private git host
  whose certificate is issued by an internal CA (or re-signed by a TLS-inspecting
  corporate proxy) failed with `SSL CERTIFICATE_VERIFY_FAILED` — dulwich verifies
  against certifi, not the OS / git cert store. New `[config_sync] ca_bundle` (and
  a "CA bundle (optional)" field in the Config Sync tab) points verification at a
  PEM file; the standard `SSL_CERT_FILE` / `GIT_SSL_CAINFO` / `REQUESTS_CA_BUNDLE`
  env vars are honored as a fallback. The HTTPS pull also routes through the
  console's `[ssh] proxy` (or the `https_proxy` env var) so a repo reachable only
  via the corporate proxy works. Prefer the CA bundle; a **Verify TLS** toggle
  (`[config_sync] tls_verify = false`) is an escape hatch that skips verification
  for a trusted network with no CA bundle (mirrors git's `http.sslVerify false`).
  Note dulwich **ignores `~/.gitconfig`**, so system-git settings (`http.proxy`,
  `http.sslVerify`, `http.sslBackend`, `credential.helper`) don't apply — these
  Config Sync settings replace them.

## [0.74.0] — 2026-06-16

### Changed
- **Config Sync is now a non-destructive merge.** A pull no longer overwrites
  config files wholesale — it tops up from the repo and keeps your own settings:
  - **Collection files** (`runspec_hosts.toml`, `runspec_triggers.toml`,
    `runspec_filters.toml`, `runspec_credentials.toml`) merge by each entry's
    natural identity (host/trigger/filter `name`, credential `id`): repo entries
    are added or update the matching local entry, and **your own entries (keys not
    in the repo) are left alone**. (Credentials already worked this way; hosts /
    triggers / filters now do too.) The triggers `[rota]` (working hours) is
    **never synced** — it's per-person, so a pull keeps the local rota and ignores
    any `[rota]` in the repo.
  - **`config.toml`** is a **deep overlay**: repo values land on top, but your
    local-only keys (e.g. an `[llm] api_key`) survive.
  - A new **`preserve`** list in `[config_sync]` (Settings → Config Sync → "Preserve
    (keep local)") pins specific keys to your local value, written
    `"<file>:<dotted.path>"` with `*` matching any entry — e.g.
    `config.toml:mailbox.folder`, `runspec_hosts.toml:*.room_user`. Built-in
    defaults (`mailbox.folder`, every host's `room_user`) are always applied, so
    the common machine-local keys are protected with no configuration.
  Implemented in the new pure `configmerge` module (deep overlay, merge-by-key,
  preserve-path application). See `docs/design/config-git-sync.md` §6.

## [0.73.0] — 2026-06-16

### Added
- **Config Sync (Settings → Config Sync).** Pull this console's config files from
  an external git repository **at a tag**, so a fresh install can be fully
  configured from one versioned source (and rolled forward/back by re-pulling a
  different tag). Syncs `config.toml`, `runspec_hosts.toml`,
  `runspec_triggers.toml`, `runspec_filters.toml`, and `runspec_credentials.toml`
  (metadata only — secrets stay in each machine's keyring); the file list is
  data-driven, so files added later sync with no code change. Authentication
  reuses a saved **credential** (Credentials tab): HTTPS uses its username + token,
  SSH repos use the console's SSH key. A pull **validates every fetched TOML before
  touching disk** (a broken pull writes nothing), **backs up** each existing file
  first, then replaces it — except `runspec_credentials.toml`, which is **merged by
  id** so the credential used for the pull (and any other local-only entry) can't
  be deleted. The local `[config_sync]` settings are preserved across the
  `config.toml` overwrite and stamped with `last_ref`/`last_commit`/`last_synced`.
  After a successful pull the running app **hot-reloads** hosts / filters /
  credentials / SSH / triggers — no restart needed. New bridge methods
  `get_sync_config` / `save_sync_config` / `list_sync_tags` / `sync_pull`; git is
  handled by **dulwich** (pure-Python — no system `git` binary required) behind the
  new `git-sync` optional extra (`pip install runspec-console[git-sync]`), with SSH
  over the already-bundled paramiko. See `docs/design/config-git-sync.md`.

## [0.72.0] — 2026-06-15

### Added
- **Reply suppression for triggers (`once_per_thread`).** A trigger can fire on the
  **first** message in a conversation and ignore later replies, so an email
  back-and-forth doesn't re-run the rule on every reply. Keyed on the event's thread
  id (`conversation_id` / `thread_id`); **fail-open** when the payload has no thread
  id. The dedup is **durable** (a small JSON state file under the app dir), so a
  reply after a console restart is still recognised. Tri-state, resolving like
  `autonomy`: a per-trigger override beats the mailbox-wide default
  (`[mailbox] once_per_thread`) beats the library default (`false`). Settings →
  Triggers gains a **Suppress replies by default** switch on the mailbox and a
  **Mailbox default / Once per thread / Every message** selector per trigger.

## [0.71.0] — 2026-06-15

### Added
- **Teams-management UI (Settings → Teams).** A new tab to manage the customer
  teams a visitor picks at sign-up — stored in the helpdesk room DB, shared across
  operators. List teams, add (name → slug), remove (Default protected). Backed by
  new bridge `list_teams` / `add_team` / `remove_team` that ride the live helpdesk
  agent connection (chat source `send_team_*` frames); the room's reply arrives as
  a `runspec:teams` event the tab refreshes from. Shows a "not connected" hint when
  no helpdesk room is up. Pairs with runspec-room 0.25.0.


## [0.70.0] — 2026-06-15

### Added
- **Triggers editor — UI (Settings → Triggers).** The Triggers tab now has a full
  trigger editor below the monitored-mailbox config: list existing triggers with an
  enable toggle + edit/delete, and an add/edit form covering name, source (dropdown),
  event (autocomplete from the source's event types), mode (agent/rule), autonomy,
  `match` (field→regex pairs), the mode-specific fields (agent: instructions +
  runnable allowlist; rule: `run` + arg `map` pairs), and `reply` (draft + style).
  Saving calls `save_trigger` and restarts the engine; parse errors surface inline.
  The Settings drawer widened (640 → 760px) for the six tabs + the wider form.


## [0.69.0] — 2026-06-15

### Added
- **Triggers editor — backend.** New bridge `save_trigger` (upsert one
  `[triggers.<name>]` table by name) and `delete_trigger`, both restarting the
  engine. `Bridge._clean_trigger` normalises a UI trigger dict into the on-disk
  shape (drops blank scalars / empty `match`/`map`/`reply` sub-tables, coerces the
  `runnables` list) so the written `runspec_triggers.toml` stays clean. The
  Settings → Triggers tab editor (UI) lands next.


## [0.68.0] — 2026-06-15

### Added
- **Visitor's team on the operator card (D2, slice 2c).** The `assignment` / `page`
  payloads now carry the visitor's `team`; it's threaded onto the portal trigger
  card (`_portal_card`) and shown as a tag beside the requester's name in the
  Triggers detail. Pairs with runspec-room 0.24.0.


## [0.67.0] — 2026-06-15

### Changed
- **Console advertises its `[mailbox]` as the sign-up verify-from (D2, slice 2).**
  The helpdesk's verify-from / domain is the console's configured `[mailbox]`
  (the room identity), so the email-verify `advertise` now sends that address as
  `queue_email` — preferred over a legacy `room_queue_email`. Pairs with
  runspec-room 0.23.0 (sign-up by team).


## [0.66.0] — 2026-06-15

### Changed
- **Helpdesk connection is mailbox-driven — no `room_queue` needed (D2, slice 1).**
  A fleet host with a `room_port` now staffs the helpdesk on its own; `host_rooms`
  defaults the queue to `helpdesk` when no `room_queue` is set (an explicit
  `room_queue` still overrides the name). The Settings → Jump Hosts form drops the
  **Queue** field and relabels **Queue port → Room port**; `save_jump_hosts` writes
  `room_port` on its own. A host with only a group-chat `room` stays group-chat-only.
  The room's `--mailbox` is the identity (one email = one room). Existing
  `room_queue` configs still round-trip (kept hidden). See
  docs/design/teams-and-email-monitoring.md.


## [0.65.0] — 2026-06-15

### Fixed
- **config.toml no longer corrupts/loses settings (incl. the SSH key).** The
  hand-rolled `_dict_to_toml` serialiser emitted **invalid TOML** for anything
  beyond one-level-flat sections — an array-of-tables or a nested table was
  written as a Python `repr` (`[{'a': 'b'}]`), which then failed to re-parse.
  Worse, `read_config()` **silently swallowed the parse error and returned `{}`**,
  so the next Save overwrote the file with near-empty config — wiping `[ssh]
  identityFile`, after which the console had no key (and no password), so SSH
  auth failed both ways and the host sat in connect-backoff. Now:
  - **Correct serialiser** — config is written with `tomli-w` (valid TOML for
    nested tables, arrays-of-tables, and string escaping).
  - **No silent wipe** — an unparseable config.toml is moved aside to
    `config.toml.corrupt-<ts>` and logged, instead of being treated as empty and
    clobbered by the next write.
  - **Atomic writes** — config is written to a temp file and `os.replace`d, so a
    concurrent/partial write can't leave a half-file.
  - **Consolidated, locked writers** — all read-modify-write of config.toml goes
    through one re-entrant lock (`update_config`), and the SSH key generate/rotate
    commits now merge `[ssh]` atomically, so two Settings-tab saves can't lose
    each other's sections.

### Added
- **Console advertises its mailbox to the room.** The chat source now sends its
  configured `[mailbox]` address in the `hello` handshake, so a room started with
  `--mailbox` only accepts this console when the two match (one email = one room;
  see runspec-room 0.21.0). Omitted when no mailbox is configured.


## [0.63.0] — 2026-06-15

### Added
- **Settings → Triggers tab: monitored-mailbox editor.** A new tab in the Settings
  drawer to set up the email trigger source without hand-editing TOML. Toggle
  **Monitor a mailbox** and fill the **email address** (blank = your own default
  account), an **optional folder** (blank/`Inbox` = the Inbox), and an optional
  **label** — Save writes `[mailbox]` to `config.toml` and restarts the trigger
  engine so the Outlook source re-resolves the watched folder. Entering a shared
  address surfaces the no-cross-console-coordination warning inline. Trigger
  *rules* (what to do with matched mail) live in `runspec_triggers.toml`. The
  Settings drawer widened (480 → 640px) so the six tabs sit comfortably.
- `config.read_mailbox()` / `write_mailbox()` + bridge `get_mailbox` /
  `save_mailbox` back the tab. One inbox is monitored (the `[mailbox]` table); the
  Outlook source reads it via `read_mailbox()`. **The queue/team concept is no
  longer tied to a mailbox or a host** — teams move to the runspec-room database,
  managed from the consoles (see `docs/design/teams-and-email-monitoring.md`).

## [0.62.0] — 2026-06-14

### Added
- **Credentials management (Settings → Credentials) — storage layer.** A new tab
  to save named credentials (username + password / password / token). **Secret
  values are stored in the OS keychain** — Windows Credential Manager, macOS
  Keychain, or Secret Service / KWallet on Linux — via the optional `keyring`
  dependency (`pip install runspec-console[credentials]`); only metadata (label,
  kind, username, the runnable args each value binds to, and host/runnable scope
  globs) is written to `runspec_credentials.toml`. New bridge API:
  `list_credentials` (metadata + a `has_secret` flag, never the value),
  `save_credential` (upsert; the write-only `secret` field rides to the keyring
  and a blank one leaves the stored value untouched), `delete_credential`, and
  `credentials_backend` (so the UI warns when no keychain is available).
- **Credentials apply to runnable invocations.** A credential's `username_arg` /
  `secret_arg` bind its values to a runnable's arguments — the username is passed
  normally, the secret rides the `RUNSPEC_*_ARG_*` env channel (never argv). They
  apply on **both** the manual (`invoke_runnable`) and agent (`_run_tool_sync`)
  paths: every **in-scope** credential (host- and runnable-glob match) auto-applies,
  and a caller can override with an explicit `credential_id` (the agent's
  per-runnable tool gains a `credential_id` enum of in-scope ids — the model picks
  an id, never sees the secret). A value the caller already supplied is never
  overwritten; two in-scope credentials binding the same arg is ambiguous and
  skipped; auto-injection only fills args the runnable declares (an explicit id
  still applies). Applied credential ids are recorded on the run for the audit
  trail.
- **Credentials apply to relays.** An ad-hoc relay source (and its directory
  browser) can reference a saved credential by id instead of a typed password:
  the stored secret becomes the SSH password and the credential's username fills
  an empty user, resolved from the keyring at connect time and never left on the
  source dict / in_flight / logs. The Relay dialog gains a credential picker
  (when credentials exist) alongside the password field. `start_relay` and
  `list_dir_adhoc` accept a `credential_id`; a typed password still wins.
- **Forms run dialog — credential override picker.** When in-scope credentials
  exist for a runnable, its run dialog shows a credential picker (default "auto
  (by scope)"); choosing one pins it as the `credential_id` override threaded to
  `invoke_runnable`. New `credentials_for_runnable(host, runnable)` bridge method
  returns the in-scope id+label list.
- **Agent `relay_file` can relay from an ad-hoc source.** The tool gains optional
  `source_via` (jump) + `source_target` (ad-hoc hostname) + `source_credential`
  (a saved credential id for password auth); `source_host` is now optional
  (provide it *or* the ad-hoc trio). This lets the agent pull a file from a host
  outside the fleet — including a password-only host — via a stored credential,
  the secret read from the keyring at connect time and never seen by the model.

### Fixed
- **Downloading a `/proc` (or `/sys`) file no longer fails with "size mismatch".**
  procfs/sysfs files report `st_size` 0 but stream real bytes, and paramiko's
  `sftp.get` asserts the downloaded size equals the remote `stat()` size — so a
  pull of e.g. `/proc/cpuinfo` always failed (and the agent's repeated retries of
  the impossible transfer were a plausible trigger for remote fail2ban bans). The
  download path now streams via `getfo` (no size assertion) for regular files and
  a bounded raw read (capped at 256 MiB) for size-0 special files; pulling a
  streaming/special file past the cap surfaces a clear "use a filter" error.

### Security
- **The agent (LLM) runnable path now keeps `password`-typed args off argv.**
  `_run_tool_sync` splits `password`-typed arguments out of the command line and
  into the `RUNSPEC_*_ARG_*` env channel, exactly as the manual `invoke_runnable`
  path already did — previously an agent-supplied secret would have been passed as
  a CLI flag, leaking into the remote's `ps` table and shell history.

## [0.61.0] — 2026-06-14

### Added
- **Email-verified queue signup — visitor name on the operator card (slice 5 of 5,
  final).** Completes the feature: the `assignment` and `page` payloads now carry
  the requester's verified **display name** (`visitor_name`), and the portal card
  shows `Alice Smith <alice@corp.com>` (summary prefers the name; the drawer shows
  `Name <email>`) instead of a bare id — so the operator knows exactly who they're
  enacting for. The "no operator on shift" page toast names them too. Display-only;
  the verified email stays the trust anchor, and the card falls back to the id when
  there's no name (OIDC / dev mode). See `docs/design/email-queue-signup.md` §5.

## [0.59.0] — 2026-06-14

### Added
- **Email-verified queue signup — the console side (slice 4 of 5).** A helpdesk
  agent console now powers the room's `email-verify` sign-up door
  (`docs/design/email-queue-signup.md` §4/§7), completing the end-to-end flow:
  - **Advertises its queue for sign-up.** On connect, an agent console with a
    `room_queue_email` set sends an `advertise` frame (`queue` + `queue_email` +
    `display_name`) so the room offers the queue on `/signup`. New host fields:
    **`room_queue_email`** (the queue's mailbox — the verify-from address *and* the
    sign-up domain allowlist) and optional **`room_queue_label`** (the dropdown
    label; defaults to the queue name). A queue without `room_queue_email` still
    takes requests — it just isn't offered as a self-service sign-up target.
  - **Sends the verify mail as the queue mailbox.** On a `send_verify` frame the
    console composes the verify mail (`sources/verify_mail.py` — pure, tested) and
    sends it via Outlook **send-as** the queue mailbox (`SentOnBehalfOfName`, a new
    `outlook.send_verify_mail` — the one auto-send, since it's a system mail not an
    agent reply), then acks `verify_sent`. A failed send acks `ok=false` so the
    room returns the signup to its outbox and retries on another console. The
    Outlook send is Windows-only; the composition + frame handling are tested on any
    platform (a pluggable `verify_sender` seam), mirroring the email folder-watch
    split.

## [0.58.0] — 2026-06-14

### Added
- **`/run` messages are surfaced as their own trigger event.** A visitor message
  in a help-desk thread that starts with `/run` is fed to the trigger engine as a
  distinct `chat`/`run` event (the chat analog of an email's `new_mail`), so it
  lands in the Triggers **Event log** and a future trigger (`source = "chat"`,
  `event = "run"`) can act on it. **No action is wired yet** — logged only; the
  natural-language → runnable derivation is a later version. Every other chat
  message feeds unchanged.

## [0.57.0] — 2026-06-14

### Changed
- **The host editor's "Group chat room" field is hidden** (portal-focused build —
  the help desk is the product). The field is kept but hidden, so an existing
  `room` value on a host still round-trips on save; remove `hidden` in
  `SettingsDrawer` to re-expose it. Use **Queue** (`room_queue`) for the help desk.

## [0.56.0] — 2026-06-14

Portal requests land in the operator's **Triggers inbox** — the same surface, the
same Approve/Deny gate and audit drawer (runnables portal, step 4 UI).

### Added
- **A routed portal `assignment` becomes a Triggers card.** The bridge creates a
  `source:"portal"` card (`card_id = "portal:<request_id>"`, detail = runnable /
  args / visitor) the moment the hub assigns a request, and walks it through the
  request lifecycle (assigned → running → done/failed), writing the enactment
  output into `detail.result`/`error`. The `card_id` is kept **equal to the
  autonomy-gate `chat_id`**, so the existing inline **Approve/Deny** prompt and
  the result render on the portal card with no new approval UI — the drawer is the
  operator's enact + audit record. New `Bridge._portal_card` / `_portal_card_id`
  / `_portal_fail`.

### Removed
- **The Triggers tab's dead claim/email controls.** With chat + email claim
  retired (0.54.0 / 0.55.0), the operator UI's Claim/Resolve/Reopen/Close buttons,
  the email reply composer, and the per-card lifecycle chip called bridge methods
  that no longer exist — all removed. A chat card still opens the live conversation
  pane (read + reply via `send_room_message`); a portal card opens the detail
  drawer. (`console-ui`: `TriggersView`, plus the dropped bridge bindings in
  `bridge/index.ts` + `bridge/mock.ts`.)

## [0.55.0] — 2026-06-14

The email help-desk channel is rebuilt as a **folder watch** — no claim, multi
mailbox/folder, built for "watch my own mailbox" (docs/design/email-folder-watch.md).

### Added
- **`[[mailboxes]]` folder-watch list.** `config.read_mailbox_watches()` parses an
  array of `{mailbox, folder}` watches (blank/`"inbox"` → the Inbox; blank mailbox
  → the default account; duplicates collapse). The Outlook source binds a
  `Folder.Items.ItemAdd` sink to each resolved folder (+ a 30 s poll fallback for
  the bulk moves ItemAdd misses, deduped by EntryID) and tags every `new_mail`
  event with its `mailbox`/`folder`, so a trigger's `match` can scope to a
  specific mailbox or folder. A watch that can't resolve logs one line and is
  skipped. `Reminder` is unchanged.

### Removed
- **The shared-mailbox claim/queue email channel.** No more `[mailboxes]`
  address→queue map, `NewMailEx` (primary-inbox-only), `mailbox_claim` markers /
  the validated fold, `send_claim_marker`, the transport-header recipient/ordering
  parse (`recipients`/`received_ts`/`message_id`/`claim_marker` and their helpers),
  the email queue-card synthesis (`_ingest_email`/`_email_queue_for`/the fold/
  `_email_feed_gate`), or the `claim_email_thread` / `resolve` / `reopen` / `close`
  / `reply_email_thread` controls. A matched `new_mail` is now just an ordinary
  trigger card + action via the normal trigger path.

### Note
- ⚠️ **No claim coordination.** Two consoles watching the same shared folder both
  react to every mail (duplicate replies/turns). Recommended: watch your own
  mailbox, or one console per shared folder, or per-operator folders via Outlook
  rules. The COM sink/poll + folder resolution are verified on Windows only; the
  pure pieces (config, payload tags, folder path-walk, dedupe) are unit-tested.

## [0.54.0] — 2026-06-14

The chat help-desk becomes the **runnables portal** end to end: claim is gone and
an assigned request is enacted automatically through the autonomy gate.

### Added
- **A portal `assignment` is enacted through the autonomy gate.** When the hub
  routes a request to this console, the bridge resolves the runnable from the
  per-queue catalog index, runs it via `_gated_run_tool` (autonomy gate unchanged)
  on a worker thread, delivers the output on the request thread, and reports the
  request `in_progress` → `done` / `failed` (`request_start` / `request_complete`).
  `page` events raise a desktop chime; `request_status` events flow to the UI.

### Removed
- **The chat claim system is retired** (runnables portal, step 7 — console half).
  Assignment is the gate now, so the claim machinery is gone: `_feed_engine_for`
  (the claim gate), `_thread_states`, the `conversation` frame handling in
  `_on_chat_event`, `ChatSource.send_control`, and the `claim_thread` /
  `resolve_thread` / `reopen_thread` / `close_thread` bridge methods (+ the
  per-thread state on `get_room_state`). A staffed-queue message now feeds the
  trigger engine ungated (only a configured chat trigger fires).
- The **email** help-desk channel still uses mailbox-marker claim (`_email_feed_gate`
  / `mailbox_claim`) — its retirement is the email folder-watch redesign.

## [0.53.0] — 2026-06-14

The chat source learns the **runnables-portal** wire protocol (the console half of
the end-to-end path; see `docs/design/runnables-portal.md` §3).

### Added
- **Agent rota presence is now sent.** An agent (helpdesk/queue) connection used to
  suppress presence frames; with the portal, an agent's presence *is* its rota
  (on shift → `available`, off shift → `away`), and the room moves the console's
  published runnables in/out of the live catalogue on it. `_RotaPresenceWatcher`
  already drives `set_presence`; it now reaches the hub.
- **Portal frames are parsed into events.** `parse_inbound` now yields `assignment`
  (a request routed to this console — runnable + args + thread room), `request_status`
  (a request's claim-free lifecycle), and `page` (a chime naming an unassigned
  request's runnable). New `assignment_payload` / `request_status_payload` /
  `page_payload`; `event_types()` extended.
- **Request-control senders.** `ChatSource.send_request_start(id)` /
  `send_request_complete(id, error=)` emit the `request_start` / `request_complete`
  control frames (agent-only) so the operator console reports a request moving to
  `in_progress` and `done` / `failed`.

### Note
- This is the transport layer only. Routing an `assignment` to actually enact the
  runnable through the autonomy gate and post the result lives in the bridge and is
  done alongside the claim-flow retirement (portal steps 4/7).

## [0.52.0] — 2026-06-13

The email help-desk channel becomes **"only triggered mail, forward-only"** — it
surfaces the *flow* of tickets, not a mirror of the whole shared mailbox.

### Changed
- **An email opens a ticket only when it matches a trigger.** A shared-mailbox
  `new_mail` becomes a claimable card only if it matches a configured
  `outlook`/`new_mail` trigger's `match` (the operator curates which mail is a
  ticket); everything else stays ordinary inbox mail — no card, no per-conversation
  state kept. A mapped mailbox with no matching trigger surfaces nothing. Claim
  still gates the agent turn; markers still fold into an *existing* ticket (a
  marker never opens one); chat is unchanged (a help-desk conversation is triggered
  by construction). New `TriggerEngine.would_match` + `bridge._email_is_triggered`.
- **Email channel is forward-only — the startup backlog scan is removed.** It only
  reacts to mail that arrives while the console is running (`NewMailEx`). Claim/
  lifecycle state lives in the mailbox markers and re-coordinates on the next
  message after a restart, so the scan wasn't load-bearing. Drops
  `_scan_email_backlog` and the COM scan helpers (`read_mailbox_items`,
  `_shared_inbox`, `_recent_filter`, `_recent_items`) and the `_ingest_email`
  `replay` plumbing. (The live-mail marker fix and event-log default-open from
  0.51.1 stay.)


## [0.51.1] — 2026-06-13

### Fixed
- **Shared-mailbox backlog scan: tolerate how the mailbox is mounted.** The
  startup scan resolved the shared mailbox only via `GetSharedDefaultFolder`,
  which is for *delegate* access and raises **E_NOTIMPL (0x80004001)** when the
  mailbox is added to the profile as its own store (an account, or an auto-mapped
  mailbox — the common setup). `read_mailbox_items` now resolves the Inbox via a
  new `_shared_inbox` helper that tries, in order: delegate access, an
  `Accounts` SMTP match (`DeliveryStore.GetDefaultFolder`), then an auto-mapped
  top-level store folder. On total failure it logs one actionable warning and
  skips the scan (the live `NewMailEx` channel is unaffected) instead of a noisy
  traceback.
- **Backlog scan is bounded for a massive shared mailbox.** It now restricts to
  mail received in the last `days` (default 14) via a locale-independent DASL
  `Restrict` — so a folder with hundreds of thousands of items is **never sorted
  or enumerated whole** — and reads only the newest `limit` lazily via
  `GetFirst`/`GetNext`, touching at most `limit` message bodies. Falls back to an
  unfiltered scan only if the store rejects the `Restrict`.
- **Recovered email cards order newest-on-top.** The backlog scan now replays its
  (newest-first) items **oldest-first**, so each recovered card's `created_at`
  rises with mail recency and the Triggers inbox (newest-created first) shows the
  newest mail on top — matching live mail instead of inverting the backlog.
- **Claim markers are detected on internal mail.** The `X-Runspec-Claim` marker is
  *set* via a PS_INTERNET_HEADERS named property but was *read* only from the
  transport-header blob — which is **empty for internal same-org mail** (it never
  traverses SMTP), so an internal claim ack was missed and mistaken for a fresh
  requester email (a reply→agent-turn loop). `_mail_payload` now reads the marker
  from the named property first (the canonical source we set; survives internal
  delivery), falling back to the transport header for externally-routed mail.
- **Event log opens by default.** The Triggers audit feed (every event seen + its
  disposition) is expanded on load, so the trail is visible without relying on the
  collapse toggle.


## [0.51.0] — 2026-06-13

The email help-desk channel reaches **audit parity** with chat: an owned email
ticket now runs a **claim-gated agent turn** that drafts a reply behind the
autonomy gate, emitting the same agent audit / `run_summary` records as a chat
queue (so the History/Analytics tabs no longer go half-dark for email queues).
See `docs/design/helpdesk-channels.md` (slice 6). Also adds a third point on the
rule↔agent trigger spectrum: an agent trigger can be **scoped to an allowlist of
runnables** (`docs/design/console-triggers.md` §5).

### Added
- **Claim gate for email agent turns.** `bridge._email_feed_gate` — the email
  analog of `_feed_engine_for` — lets a shared-mailbox `new_mail` reach the
  trigger engine (and so start an agent turn) only once this console **owns** the
  folded conversation. A claim/lifecycle marker ack never starts a turn, and
  non-queue (personal) mail routes ungated, so a user's own Outlook trigger is
  unaffected. This stops both consoles drafting a reply on an unclaimed thread
  (which would burn model spend and break first-ack-wins), mirroring chat where an
  unclaimed thread never feeds the engine.
- **`TriggerEngine` gained an injectable `feed_gate`** — a per-event predicate
  checked in `_route` after the `on_source_event` hook (so the queue card is
  surfaced first) and before trigger matching. A rejected event is dropped
  entirely — no card, no run, no audit record — exactly like an unclaimed chat
  message. Fail-open: a raising gate never blocks routing. The engine stays
  bridge-agnostic (the gate is injected, fake-tested), parallel to `clock_ok`.
- **Agent drafts reply as the shared mailbox.** A help-desk email agent turn now
  drafts the reply *as the queue identity* (threads `from_mailbox` through
  `run_agent_turn` → `_gated_draft_reply` → `save_reply_draft`), matching the
  human reply path (slice 5). The trigger handler resolves the send-as mailbox
  from the event via a new `BridgeOps.queue_mailbox_for`; personal/other mail and
  chat drafts (propose_reply) are unaffected — they draft from the operator.
- **Agent trigger `runnables` allowlist.** An agent-mode trigger may set
  `runnables = ["host__name", "name", …]` to scope the LLM's toolset to a curated
  set — the third point between `mode = "rule"` (one fixed runnable) and `mode =
  "agent"` (every runnable). Each entry matches a runnable's bare name or its
  qualified `host__name`. Empty/omitted keeps the current "any runnable"
  behaviour; an allowlist that matches nothing logs a warning and the agent runs
  tool-less (rather than silently falling back to all). Rejected in `rule` mode.
- **Draft on claim — claiming is a trigger event.** Taking ownership of an email
  ticket now feeds a synthetic `email`/`claimed` event (carrying the latest
  requester message) to the trigger engine, so a one-message ticket can auto-draft
  — the new_mail gate only fires on a *follow-up* message. Bind a normal trigger
  (`source = "email"`, `event = "claimed"`, `reply.draft = true`); opt-in by config
  presence. `_ingest_email` retains the requester subject/body and fires once on the
  unowned→owned flip (only with a requester message to reply to); the startup
  backlog scan replays with `replay=True` so a restart never re-drafts a claimed
  thread. `"email"` is registered as an external engine source. Also fixes a latent
  bug where a card first created by a claim ack seeded its reply `entry_id` from the
  ack instead of a requester message.

### Notes
- The agent turn reuses the existing `draft_reply` path (save-to-Drafts, never
  sent), gated by the autonomy prompt.


## [0.50.0] — 2026-06-12

A shared mailbox becomes a **second channel** on a help-desk queue, reusing the
queue-card / claim / lifecycle model. An internal help request or alerting email
shows up as a ticket in the Triggers inbox; operators claim it (coordinated across
consoles with no central server), work it, and reply — and a restart recovers
in-flight state. See `docs/design/helpdesk-channels.md`.

### Added
- **Shared mailbox → queue card.** A new `[mailboxes]` table in `config.toml` maps
  a mailbox address to a queue; an Outlook `new_mail` to a mapped mailbox folds
  onto one Triggers card per conversation (`source = "email"`). Recipients are
  matched against the real SMTP addresses from the transport headers (To/Cc +
  `Delivered-To` / `X-Original-To` / `Received: … for`), so distribution-list
  delivery is recognised and a lookalike address can't match. Personal/other mail
  never becomes a card.
- **Mailbox claim + lifecycle (first-ack-wins).** Claiming sends an
  `X-Runspec-Claim` marker email to the mailbox itself (threaded); every console
  folds the thread (`mailbox_claim.fold_conversation`) and converges on the same
  owner/state. The email card gets Claim / Resolve / Reopen / Close; ownership is
  read-after-write (never optimistic).
- **Ownership-gated reply.** Only the owning console may reply to the requester;
  the reply drafts + sends via Outlook **as the shared mailbox**
  (`SentOnBehalfOfName`), so the requester sees the queue identity.
- **Startup backlog scan.** `NewMailEx` is forward-only; on startup the console
  replays each mapped mailbox through the fold (`GetSharedDefaultFolder`) to
  recover in-flight conversations + claim state. Items are deduped (Message-ID /
  EntryID) so the scan and the live source can't double-count.


## [0.49.0] — 2026-06-12

The helpdesk surfaces collapse into **one operator tab — Triggers**. A queue
message is now just another trigger, so the separate **Rooms** and **Inbox**
tabs are gone and everything an operator acts on lives in one place.

### Changed
- **Queue conversations are first-class Triggers cards.** A message in a staffed
  help-desk queue surfaces as a card in the Triggers inbox (one per conversation,
  updated in place), carrying the conversation's lifecycle state. The row shows a
  state chip + inline **Claim / Resolve / Reopen**, and **Approve/Deny** for a
  request reuses the exact same right-hand detail panel as a trigger run — no
  separate gate. Model spend stays gated on **Claim** (an unclaimed conversation
  is visible but never starts an agent turn).
- **Conversation pane under the inbox.** Opening a queue card shows its transcript
  + reply composer in a pane below the inbox (where the old Rooms tab's chat now
  lives); an agent-proposed reply pre-fills it for review-and-send.
- **Removed the Rooms and Inbox tabs.** The Triggers tab is the single operator
  surface; its badge now covers pending approvals **and** waiting (unclaimed)
  queue callers.
- **Triggers layout** — dropped the "Recent activity" strip (the event log
  supersedes it) and moved the event log into a collapsible, freeing room for the
  conversation pane.
- **Vocabulary: room → queue.** The Hosts editor now leads with **Queue** /
  **Queue port** (group chat is a clearly-secondary "Group chat room" field),
  matching the browser side that always said *queue*.

### Fixed
- **A many-arg request no longer pushes the Approve/Deny gate off-screen.** The
  autonomy-gate tool-call card rendered every argument inline on one un-truncated
  line, forcing the row wider than the window so the **Approve & run / Deny**
  buttons scrolled out of view. The header preview now ellipsises to a single
  line and the full argument list is shown **wrapped** in the gate body, directly
  above the buttons — so the operator reads exactly what they're approving.
- **Packaging:** `pywin32` now carries a `sys_platform == 'win32'` marker, so a
  non-Windows `pip install runspec-console` can resolve its dependencies.


## [0.48.0] — 2026-06-12

### Added
- **Unified work inbox (v1).** A new **Inbox** tab (first in the nav, with a
  count badge visible from anywhere) gathers everything needing the operator into
  one list across the three attention surfaces: parked autonomy-gate confirms
  (chat *and* trigger), unclaimed helpdesk threads in queues you staff, and
  in-progress trigger cards. Rows are tiered — **Needs approval** (blocking;
  oldest first, since gates time out) → **Action needed** (an unclaimed visitor —
  Claim inline) → **In progress** (your assigned threads + triage/running cards).
  Inline **Approve/Deny** (confirms) and **Claim** (threads) route to the existing
  bridge methods; **Open** deep-links to the owning tab. It's a *projection*, not
  a new store: the owning surfaces stay authoritative and a row leaves when they
  emit their terminal event. A trigger card's `awaiting_human` gate folds into its
  single confirm row (the same id-match the tab badges use), so it's never
  double-counted. New `get_pending_confirms()` bridge getter supplies the
  cold-start snapshot for gates parked before the view mounted. Design:
  `docs/design/console-work-inbox.md`.

## [0.47.0] — 2026-06-12

### Added
- **Hosts-editor fields for helpdesk staffing.** The Jump Hosts editor now
  manages `room_queue` (the helpdesk queue this console staffs on the host's
  room server, agent mode) and `room_catalog` (an optional per-queue
  self-service catalog allowlist of bare runnable names; empty = every
  `self-service`-marked runnable). Both ride the existing **Room port** and
  share the room transport (`room_via` / relay command), so a host can host a
  group-chat room, staff a queue, or both over the one server. Previously these
  keys needed hand-edited TOML — they were preserved across editor saves but
  not surfaced. Export (`jump_hosts.toml`) emits them too. The queue field
  rejects slashes/whitespace (mirrors the room server's token rule), and a
  queue without a room port is dropped (it can't connect).
- **Desktop notifications for room messages.** An inbound chat-room / helpdesk
  message now raises an OS toast + taskbar flash (clicking it focuses the
  console), at parity with the confirm-gate notification — so help-desk work
  pulls you back even when the console is minimised. Suppressed when the console
  is already in the foreground (the in-app toast covers that). Windows-only
  (`[notifications]` extra), best-effort, and opt-out via
  `[console] room_notifications = false` (keeps the in-app toast + unread
  badges). A helpdesk toast names the visitor + queue; a `request`-kind message
  reads as a request.

## [0.46.0] — 2026-06-12

### Added
- **Helpdesk inbox notifications.** The **Rooms** tab now carries a count badge
  of rooms/threads with unread messages (visible from any tab); a new visitor
  message you're not looking at raises a corner toast (visitor + preview); and
  each inbox thread shows an unread dot + count, with the queue/room entries in
  the left list badged too. Opening a thread (with the tab visible) marks it
  read. Keeps you aware of incoming help-desk work without living in the tab —
  the first step toward a unified attention surface across Triggers and Rooms.

### Fixed
- **`room_command` with arguments is now used verbatim** (exec transport). A
  bare `room_command` (e.g. `console-room`) still gets `connect --host … --port …`
  appended, but one that *already* carries arguments — `nc 127.0.0.1 8765`,
  `socat - TCP:127.0.0.1:8765` — is run as-is. Previously the suffix was appended
  to *everything*, so `room_command = "nc 127.0.0.1 8765"` actually ran
  `nc 127.0.0.1 8765 connect --host … --port …`, which nc rejected → the relay
  exited → the reconnect loop. (Custom relays still belong in `room_exec_command`;
  this just stops `room_command` being a footgun.)
- **Connection tester no longer hangs on a spinning button.** It now short-circuits
  to an instant OK when the room is already connected, and bounds the fresh-attempt
  path with a hard 12s timeout (returning a clear "timed out" verdict) instead of
  blocking on a slow proxied SSH connect. The Rooms-tab "Test" button shows a
  "Testing…" indicator immediately and always resolves to a dialog.

---

## [0.45.0] — 2026-06-12

### Added
- **Room connection tester (Rooms tab → "Test").** Diagnoses a room/queue's
  connection on demand over the *real* paramiko path the supervisor uses (HTTP
  proxy, key, ssh_config, then the tunnel or exec relay), in an isolated attempt
  that ignores the pool's connect-backoff and doesn't disturb the live
  connection. It reports the actual blocker with a localised stage —
  **ssh** (reach/auth the host, incl. the proxy), **channel** (tunnel/exec
  relay), or **room** (the relay reached the host but the room server isn't
  answering on the port) — plus an actionable hint, in a dialog. No more reading
  tracebacks or fumbling with a local `ssh` command (which exercises OpenSSH
  ControlMaster/config the console never uses).

### Changed
- **Blocked SSH port-forwarding now logs one actionable line, not a repeating
  traceback.** A host with `AllowTcpForwarding no` refuses the direct-tcpip room
  tunnel (`Administratively prohibited`) — a permanent condition the reconnect
  loop would otherwise re-report as a full traceback every backoff. The
  supervisor now detects it and points at the fix (`room_via = "exec"`); the same
  classification powers the connection tester.
- **Agent-mode chat source logs its queue.** `listening on queue 'helpdesk/<q>'`
  instead of the empty `listening in room ''` (an agent subscribes to a namespace,
  not a room).

---

## [0.44.2] — 2026-06-12

### Changed
- **Helpdesk inbox moved on top of the conversation.** In queue mode the inbox is
  now a full-width strip above the conversation (was a cramped side column), so
  each thread row carries a real summary line — status · visitor · assignee ·
  last-message snippet (catalog requests prefixed) · time — and the open count
  shows in the header. Still collapses (now up/down) to give the conversation the
  full screen. Group chat is unchanged.

---

## [0.44.1] — 2026-06-12

### Fixed
- **Helpdesk catalog re-published on every discovery refresh.** The periodic
  refresh cycle re-sent (and re-logged `published N tool(s)`) each staffed
  queue's catalog unconditionally — log spam plus redundant `catalog` frames to
  the room. The publisher now tracks the last catalog sent per queue namespace
  and only re-sends when its contents change. Subscribe still forces a send (a
  fresh connection's hub has no catalog yet); a disconnect clears the cache so a
  reconnect republishes from scratch.

### Changed
- **Helpdesk inbox layout.** The queue inbox column is now collapsible (fold to
  a thin rail to focus on one conversation full-width), and the presence sidebar
  is hidden in queue mode — a helpdesk thread is 1:1 (you + the visitor), so that
  width goes to the conversation. Group chat is unchanged.

---

## [0.44.0] — 2026-06-12

### Added
- **Helpdesk agent mode — staff a runspec-room queue from the console.** A fleet
  host entry with `room_queue = "it-support"` (plus the usual `room_port`)
  subscribes the console to that queue's namespace on the host's room server:
  the Rooms tab gains an **inbox** of that queue's private visitor conversations
  with lifecycle chips (open / assigned / resolved / closed) and **Claim /
  Resolve / Reopen / Close** controls. Replies go through the existing composer
  (the agent only ever pre-fills it — the human sends). A host may declare both
  `room` and `room_queue`; group chat is unchanged. See
  `docs/design/helpdesk-rooms.md`.
- **Claim-gated agent turns.** A message in a staffed queue feeds the trigger
  engine — and so can start an agent LLM turn — only once the thread is
  **assigned to this console** (someone clicked Claim). Unclaimed threads are
  visible in the inbox but never spend the model; group-chat triggers behave as
  before.
- **Self-service catalog publisher.** On subscribe (and after every fleet
  discovery refresh) the console publishes the queue's catalog to the room: the
  discovered runnables marked `discoverable ∋ "self-service"` (H0 keyword),
  converted to the visitor form contract (`name`/`description`/`inputSchema`,
  password args omitted, subcommand runnables skipped), de-duplicated across
  hosts. `room_catalog = ["reset-password", …]` on the host entry narrows it
  (absent = all marked). Hosts running an older runspec surface nothing —
  safe by default.
- **Visitor requests in the transcript.** A catalog submission arrives as a
  `request`-kind message tagged in the inbox transcript, carrying the
  `{tool, args}` payload for enactment through the normal autonomy gate.
- New bridge API: `claim_thread` / `resolve_thread` / `reopen_thread` /
  `close_thread`; `get_room_state()` entries carry `queue` + initial `threads`;
  new `runspec:room_conversation` UI event.

---

## [0.43.2] — 2026-06-11

### Added
- **Per-host exec relay command + Hosts-editor fields for the exec transport.**
  A fleet host can now override the exec-channel relay command verbatim with
  `room_exec_command` (e.g. `socat - TCP:127.0.0.1:8765`), bypassing the default
  `console-room connect …` builder — useful where only a specific relay binary is
  available. The Hosts editor gained matching **Connect via** (tunnel/exec) and
  **Relay command** fields, so a room that needs the no-forwarding exec transport
  can be configured from the UI instead of hand-editing `runspec_hosts.toml`.

## [0.43.1] — 2026-06-11

### Fixed
- **Rooms presence list was unreadable in dark mode.** The participant names used
  an unthemed `<span>` (black text), so they vanished against the dark panel. They
  now use the themed text component, like the rest of the view.

## [0.43.0] — 2026-06-11

### Added
- **Exec-channel transport for rooms — join without SSH port-forwarding.** Set
  `room_via = "exec"` on a host entry and the console runs `console-room connect`
  on that host over a normal SSH *exec* channel (then it connects to the room's
  local port there), instead of opening a `direct-tcpip` tunnel. This works on
  networks that block forwarding — a hardened `sshd` (`AllowTcpForwarding no` /
  `PermitOpen`) or a security appliance that resets tunnels. `room_command` sets
  the `console-room` path on the host (default: on `PATH`); the NDJSON framing is
  shared with the tunnel transport, so behaviour is otherwise identical. Requires
  `runspec-room` ≥ 0.4.0 on the host (for the `connect` subcommand).

## [0.42.1] — 2026-06-11

### Fixed
- **Room display name showed the local OS user instead of the SSH login.** When a
  host's `ssh` target carried no `user@` (the login comes from the `[ssh]` default
  username), the room announced you as your *local* (e.g. Windows) user. It now
  prefers the configured `[ssh]` username, so a Windows console connecting to a
  Linux host shows the Linux identity. Order: `room_user` → `user@host` →
  `[ssh]` default user → local OS user.
- **Saving a host in the editor dropped hand-added keys.** `save_jump_hosts`
  rebuilt each entry from a fixed field list, silently deleting anything else
  (e.g. `room_dest_host`, `role`) on the next save. It now preserves unmanaged
  keys verbatim — so a `room_dest_host = "localhost"` (to work around an sshd
  `PermitOpen` that allows `localhost` but not `127.0.0.1`) survives.

### Added
- **Display name** field in the Hosts editor (next to Chat room / Room port) —
  sets `room_user` on the host entry without hand-editing TOML.

## [0.42.0] — 2026-06-11

### Changed
- **Chat rooms are now a property of a fleet host, not a standalone `[chat]`
  table.** Add `room` + `room_port` to a host entry in `runspec_hosts.toml` (or via
  the Hosts editor) and the console joins that room over **that host's own SSH
  tunnel** — inheriting its ssh target, key, and proxy, and reusing the pooled
  connection. This fixes rooms failing to connect because the old `[chat] host`
  string carried none of the host's SSH settings. **Multi-room:** every host that
  declares a room is joined; the Rooms view lists them and you switch between
  them. `room_user` overrides the display name (defaults to the SSH login).
- A legacy `[chat]` table still works **when its `host` names a fleet entry** —
  it's resolved to that host (so it now picks up the correct ssh target + key too).
  Migrate it to the host entry when convenient.

## [0.41.1] — 2026-06-11

### Fixed
- **Confirm-event desktop toasts never appeared on Windows.** The toast send
  called `ToastNotificationManager.create_toast_notifier(app_id)`, but pywinrt
  projects that name as the *no-arg* `CreateToastNotifier()` overload — passing
  it an AUMID raised `TypeError: Invalid parameter count`, which the best-effort
  guard swallowed (so only a debug-log line, no toast; the taskbar flash still
  fired). The AUMID overload is projected as `create_toast_notifier_with_id`;
  the notifier is now built via that, falling back to the no-arg form (which
  uses the process AUMID set by `set_app_id()`) when it isn't present. The
  `winrt`-free unit tests now cover the overload selection.


## [0.41.0] — 2026-06-11

### Changed
- **The chat room is now always-on.** The console joins the room whenever
  `[chat]` (host/port/room) is configured — no longer only while a `source =
  "chat"` trigger is enabled. The room is a coordination surface in its own right:
  you can read the transcript, see who's present, and chat with people without
  authoring any automation. A reconnecting supervisor keeps the connection up for
  the session (exponential backoff on drops), and presence still follows the
  working-hours rota. Chat triggers still fire — the trigger engine receives room
  messages even though it no longer owns the connection. The Rooms view shows live
  connection status (`offline — reconnecting…` while a tunnel is re-establishing).

## [0.40.0] — 2026-06-11

### Added
- **Rooms — a team chat room inside the console.** A new tab showing the live
  transcript, who's present, and a composer, so the console is a participant in a
  shared on-call room alongside the people who log in from a browser. A message in
  the room is a `chat` trigger event: an on-shift console reacts through the
  normal trigger machinery (`match` → rule/agent → autonomy gate). Nothing the
  console says is automatic — an agent can **propose** a reply, which only
  pre-fills the composer (`runspec:room_draft`); **you** edit and send it.
- The console joins over its existing **SSH tunnel** (a `direct-tcpip` channel to
  the room server's localhost port) — its right to be in the room *is* its SSH
  access, so there's no new exposed port and no second credential. Presence
  follows the working-hours rota: available on-shift, away otherwise. Identity is
  the configured `[chat] user`, defaulting to the SSH login (`alice@web01`).
- Configure with a `[chat]` table (`host` / `port` / `room` / optional `user`) and
  a `source = "chat"` trigger. The room server itself is the new **`runspec-room`**
  package (`packages/console-room/`).


## [0.39.0] — 2026-06-11

### Added
- **Desktop notifications when an agent tool call needs your approval.** When a
  runnable hits the autonomy gate (the default `confirm` level — and `supervised`
  too) the console now raises a Windows toast ("Approval needed: <runnable>")
  *and* flashes the taskbar button, so you're pulled back to answer instead of
  the prompt silently timing out as a deny after five minutes while the window
  is in the background. Clicking the toast foregrounds the console; you still
  approve/deny in the existing in-app card. Covers every confirmation path (chat
  agent, trigger board, supervised runs) from the single `_register_confirm`
  chokepoint.
  - *Works on locked-down corporate machines:* **sending** a toast uses
    `ToastNotificationManager` and is gated only by the per-app "send
    notifications" toggle — it does **not** need the "let apps read my
    notifications" consent the existing listener trigger source requires. So a
    machine with reading disabled still gets confirm toasts. The taskbar flash
    needs no notification permission at all and is the fallback if the toast is
    suppressed.
  - *Off-Windows:* a safe no-op (logged only).
  - Opt out with `[console] confirm_notifications = false` in `config.toml` to
    silence the toast while keeping the in-app card.
  - The optional `[notifications]` extra now also carries
    `winrt-Windows.Data.Xml.Dom` (needed to build the toast); the existing
    `winrt-Windows.UI.Notifications` projection does the send. No new
    permissions, Windows-only.


## [0.38.4] — 2026-06-10

### Fixed
- **History no longer reloads from empty every time you open the tab.** The
  History view was conditionally rendered, so navigating away unmounted it and
  coming back remounted it — resetting its rows and re-running the SSH history
  read from scratch on every visit, with no spinner, so it looked frozen even
  though the log volume is small (the cost is the round-trip latency, paid each
  time). History now stays mounted (display-toggled, like Console and Triggers),
  so its rows persist and returning shows them instantly; its 8 s poll is gated
  to only run while the tab is visible, so a hidden History tab isn't doing an
  SSH round-trip in the background. A spinner now shows on the genuine loads
  (first open and host switch) instead of a blank table.
- **Analytics no longer re-scans from scratch every time you open the tab.**
  Same root cause as History — it was conditionally rendered, so each visit
  unmounted it, dropped its `data`, re-ran the per-host scan, and re-flashed the
  full-screen spinner. Because Analytics is lazy-loaded (the heavy plots chunk),
  it's now mounted on first visit and kept mounted thereafter (display-toggled),
  so its data survives tab switches and returning shows the dashboard instantly;
  its 45 s poll is gated to run only while the tab is visible.
- **Logs inventory no longer re-runs its SSH scan every time you open the tab.**
  The Logs view's `logs status` round-trip ran on every remount; it now stays
  mounted (display-toggled) so the inventory persists, with the scan gated to
  run only while the tab is visible and on host change (not eagerly at startup
  or in a hidden tab). (Forms, Specs and Schedules were checked and left as-is —
  they render from already-loaded data, with no per-open SSH round-trip.)


## [0.38.3] — 2026-06-10

### Changed
- **History and Analytics are scoped to the selected host again.** Both views
  fetch for the host clicked in the sidebar instead of fanning out across the
  whole fleet (`get_history_all` / `get_analytics(['all'])`). The all-hosts
  model made every read an N-host scan: on a fleet with a slow or offline
  remote, an Analytics load outran its 45 s refresh, loads piled up, the SSH
  pool saturated, and the view spun forever. One host per read is a single SSH
  round-trip — clicking a host drives both views. The Analytics Host filter
  still lets you opt into comparing specific hosts on demand.

### Fixed
- **Analytics can't get stuck on its loading spinner.** The fetch now always
  clears the loading flag — on success *or* failure (`.catch`) — and skips a
  scheduled refresh while one is still in flight, so a slow or rejected scan
  shows the empty state instead of spinning indefinitely.


## [0.38.2] — 2026-06-10

### Fixed
- **Per-host spinners no longer get stuck on after a restart.** The loading
  spinner shown next to every host in the sidebar is driven by a single
  `runnablesLoading` flag that was only ever cleared inside the initial
  `get_runnables('all')` call — with no error path. If that bridge call rejected
  or never settled (e.g. right after a restart, before the webview API is
  injected), the flag stayed `true` and every host spun forever with no
  recovery. The flag is now also cleared on failure (`.catch`) and whenever
  discovery completion (`runnables_updated`) arrives — which is delivered
  Python→JS through the dispatch queue, so it lands even if the initial
  JS→Python call stalled.
- **In-tree `__version__` re-synced to the packaged version.** The source-run
  fallback in `__init__.py` had drifted to `0.34.2` while the package was at
  `0.38.x`, so running from a non-installed checkout showed a misleading version
  in the sidebar. Bumped to match.


## [0.38.1] — 2026-06-10

### Fixed
- **A pending trigger approval survives switching tabs.** The Triggers view is
  now kept mounted (display-toggled, like the Console view) instead of being
  unmounted when you navigate away. Previously, leaving and returning while an
  approval was waiting dropped the live `tool_confirm`, so the Approve/Deny
  buttons never came back and the run sat stuck until it timed out.


## [0.38.0] — 2026-06-10

### Fixed
- **Trigger confirmations and cards now appear reliably.** All UI dispatches go
  through a single FIFO queue drained by one thread, so `evaluate_js` is never
  called concurrently from background threads (trigger workers, the Outlook COM
  pump, the agent loop). Concurrent background dispatch was racy on pywebview —
  under a burst of incoming events the autonomy-confirm prompt could be lost (a
  rule-mode run "stuck in running" with no buttons; the Approve/Deny panel
  vanishing during a flood of unrelated mail). Serialising delivery fixes the
  flakiness and keeps events in order.
- **History shows remote runs regardless of how runspec was installed there.**
  Remote History guessed the log dir from the binary path (`{rp}/../../logs`),
  which is wrong for pipx / `~/.local` / system installs — so a run the Logs tab
  found (it uses the runnable's real `sys.prefix`) was missing from History.
  History and Analytics now ask the remote runspec for its actual log dirs
  (`logs status --json` → `log_dirs()`), cached per venv, with the path-derived
  dir and `$HOME/logs` as fallbacks.


## [0.37.0] — 2026-06-10

### Added
- **Send button for agent-drafted replies.** `draft_reply` still only ever
  *drafts* (the agent never sends), but once a draft is saved its trigger card
  now shows a **Send** button: you review the draft and send it with one click
  from the console, instead of switching to Outlook. The send is always
  human-initiated — `save_reply_draft` returns the draft's `EntryID`, the card
  surfaces it, and the new `send_trigger_draft` / `outlook.send_draft` send it.

### Changed
- **`post_to_chat` now mirrors a trigger run into the Console as the same card a
  manual invocation makes**, instead of a plain text note — so a triggered run
  looks just like the same command run by hand, only the source differs:
  - **`mode = "rule"`** → a standard **slash-command run card** (`/runnable on
    host` heading, the runnable's output as run lines, and a non-zero exit code
    surfaced from the gate's `[exit N]` prefix).
  - **`mode = "agent"`** → an **agent/chat card** showing the model's reply (the
    same shape as a Console LLM turn).

  The Triggers tab's "Recent activity" list now shows the card's label rather
  than the raw output.


## [0.36.2] — 2026-06-10

### Fixed
- **A denied trigger confirm no longer leaves the card stuck on "awaiting
  human".** On Deny (and a declined `draft_reply`), the gate returned without
  advancing the card stage, so it sat in `awaiting_human` while the agent turn
  actually continued — reasoning about the denial, often producing an analysis.
  A declined tool now moves the card to `running`, so the ongoing work is
  visible and the awaiting-confirm tab badge clears promptly.
- **History now shows runs on remote hosts that landed in `~/logs`.** The
  0.36.0 fix covered the local History/Analytics scan but not the SSH path:
  `_get_remote_history` read only the *first* venv's `logs/` dir, so a remote
  runnable whose venv dir isn't writable (it logs to `~/logs`) — or one in a
  second venv — was missing from History even though the Logs tab showed it
  (the CLI reads `log_dirs()`). The remote History and Analytics scans now
  cover every venv `logs/` dir plus `~/logs`, matching local.


## [0.36.0] — 2026-06-10

### Added
- **Collapsible Triggers table.** The configured-triggers list is now a
  collapsible panel (like Working hours), with the trigger count / engine status
  in the header and the reload control in the panel.
- **Awaiting-confirmation badge on the Triggers tab.** A count badge appears on
  the Triggers nav tab (like the Console in-flight badge) whenever a trigger run
  is waiting on a human confirm, so a pending approval is visible from any tab.

### Fixed
- **Outlook-calendar working-hours rota now works.** Outlook source events were
  emitted synchronously on the COM message-pump thread, so the rota's calendar
  read ran *re-entrantly inside the COM callback* and failed — silently falling
  back to the grid / always-on. Events are now marshalled onto a queue and routed
  by a separate dispatcher thread, off the pump thread. (The manual console-grid
  rota was unaffected and already worked.)
- **`post_to_chat = true` now actually posts to the Console tab.** The trigger
  note was dispatched on an event only the Triggers tab listened for, so it never
  reached the chat transcript. The Console tab now renders it as a finished chat
  block.
- **History shows triggered (and other ~/logs) runs.** When a runnable's venv
  `logs/` dir isn't writable (common on managed machines) it logs to `~/logs` —
  but the venv dir is still created empty, so History read the empty venv dir and
  never looked in `~/logs`. History and Analytics now scan both, matching what
  the Logs tab already saw via the CLI.


## [0.35.0] — 2026-06-10

### Changed
- **Triggers are now written as named tables, one canonical form** — matching
  `runspec.toml`'s named runnable sections. Each trigger is a `[triggers.<name>]`
  table with its sub-tables namespaced under the same name
  (`[triggers.<name>.match]` / `.map` / `.reply`). The table name *is* the
  trigger's name, so the separate `name` key is gone (one less keyword). Grouping
  each trigger under one unambiguous prefix means a second trigger can no longer
  absorb the keys of the one above it — the failure mode behind "if I comment out
  one trigger the other starts working".

### Removed
- **The old `[[triggers]]` array-of-tables form is no longer supported.** It was
  error-prone: a bare `[triggers.match]` sub-table attached to whichever
  `[[triggers]]` preceded it, so file-wide ordering mattered. If a config still
  uses it, the Triggers tab now shows a clear message telling you to switch to
  `[triggers.<name>]` (rather than silently loading nothing).


## [0.34.2] — 2026-06-10

### Fixed
- **A TOML error in one trigger no longer silently disables them all.**
  `runspec_triggers.toml` is parsed as a whole, so a single trigger's syntax
  error (e.g. a duplicate key) made `tomllib` reject the entire file and *every*
  trigger vanished with no explanation. The Triggers tab now surfaces the
  file-level parse error (and it's logged), instead of just showing zero
  triggers. (Reminder: within a `[[triggers]]`, scalar keys must precede the
  `[triggers.match]` / `[triggers.map]` / `[triggers.reply]` sub-tables.)


## [0.34.1] — 2026-06-10

### Fixed
- **Closing the app no longer hangs when a turn is parked at the autonomy gate.**
  If a trigger (or chat) turn was awaiting a confirm, its worker thread sat in
  `event.wait()`, so the process couldn't exit (Ctrl-C didn't help; it only
  freed after the 5-minute confirm timeout). On window close / interpreter exit
  the console now wakes every pending confirm and signals every cancel flag, so
  in-flight turns abort immediately and shutdown is clean. (Restarting the engine
  — e.g. an enable/disable toggle — only aborts *trigger* turns, never your chat.)


## [0.34.0] — 2026-06-10

### Changed
- **History spans all hosts.** The History tab now aggregates runs across every
  host (local + connected remotes) instead of only the sidebar-selected host, so
  a trigger run shows up regardless of which host it ran on. The **Host** column
  filter narrows it on demand. Disconnected remotes are skipped (no SSH hang).
  Per-host scoping is unchanged for Forms, Logs, Analytics, etc.


## [0.33.1] — 2026-06-10

### Fixed
- **Dark-mode text in the Triggers tab.** The event log, run detail drawer, and
  working-hours editor used raw elements with no explicit colour, so their text
  rendered black (invisible) in dark mode. They now use a theme-aware text
  colour.


## [0.33.0] — 2026-06-10

### Changed
- **Triggers view → inbox list + detail drawer.** The kanban board is replaced
  by a compact list of runs (status · summary · trigger · time); clicking a run
  opens a large slide-out **drawer** with the full event, the agent's output, the
  complete timeline, and the Approve/Deny gate — so the rich detail isn't crammed
  into tiny cards. The event log and configured-triggers table are unchanged.
- **Real awaiting-human → running → done staging.** Agent-mode runs now report
  proper stages on the backend: a card sits in **awaiting_human** while a confirm
  is pending and **running** while a tool executes (previously it only used
  triage/done and bounced back to triage after a confirm). Each stage is recorded
  in the timeline, so a run parked on a *second* confirm is now visible.

### Fixed
- The Working-hours editor notes that **changes apply on Save** (the switches are
  pending until then — toggling alone didn't take effect).

### Docs
- Clearer `mode = "rule"` guide: `run = "host__runnable"` and that
  `[triggers.map]` keys are the runnable's own argument names.


## [0.32.0] — 2026-06-10

### Added
- **Event log — audit every event, matched or not.** The Triggers tab now shows
  a feed of *every* event the engine sees with its disposition: which triggers
  **fired**, which **matched but were skipped by the working-hours rota**, or
  **no match**. This answers "why didn't my trigger fire?" at a glance — an email
  that's present but shows "no match" or "skipped (working hours)" tells you it's
  a `match` or rota issue, while an email that never appears means the source
  didn't see it. Backed by `get_trigger_events()` + a `runspec:trigger_event`
  stream (bounded ring buffer). Note: the rota uses **local machine time**.


## [0.31.0] — 2026-06-10

### Added
- **Outlook-calendar rota source.** The working-hours rota gains a `source`:
  `"console"` (the manual weekly grid) or `"outlook"` (your calendar). With
  `source = "outlook"`, "on shift now?" is read from the **desktop Outlook
  client** (COM) — you're on shift when a calendar event whose subject contains
  `shift_marker` (default `"shift"`) covers the moment. Ideal for rotating
  shifts: put them in your calendar and the triggers follow. Optional
  `hold_during_meetings` (default off) also pauses while you're in a meeting/OOO.
  If the calendar can't be read (Outlook closed, non-Windows), it **falls back
  to the console grid**. The two never run at once — `source` picks one
  authority. (Outlook's Work-Hours *setting* isn't used: it isn't exposed in
  free/busy and is only readable via Graph, which managed tenants block — hence
  the marker-event approach.)


## [0.30.0] — 2026-06-10

### Added
- **Working-hours rota.** A global weekly schedule (Triggers tab → *Working
  hours*) gates when triggers may fire: per-day on/off with a start and end
  time, in local machine time. Outside the windows, matching events are
  **dropped, not queued** — so automation only runs while someone's around to
  handle confirms, and you don't get a flood when the window opens. Off by
  default (triggers run anytime until you set it up). A per-trigger
  `ignore_rota = true` escape hatch lets a specific trigger run regardless.
  Stored as `[rota]` in `runspec_triggers.toml`.
- **Per-card audit timeline.** Each kanban card now keeps a timestamped trail of
  what happened — stage changes (triggered → triage → … → done) plus each tool
  the agent ran — recorded on the backend so it survives even fast-moving cards
  and tab switches. Expand a card's *timeline* to read the full trace.


## [0.29.3] — 2026-06-09

### Changed
- **See what a trigger's agent is doing — on the card, live.** The Triggers tab
  now streams the agent's text onto its card as it arrives (not only the final
  summary at Done), so an agent-mode trigger's reasoning/reply is visible in the
  app instead of only in the debug log.
- **The approve prompt shows what you're approving.** A card parked at *Awaiting
  human* now previews the tool's input — for `draft_reply`, the proposed reply
  body; for a runnable, its arguments — so you can review the draft/command
  before clicking Approve instead of approving blind.


## [0.29.2] — 2026-06-09

### Fixed
- **Reject a misplaced key in `[triggers.match]`.** A trigger scalar (e.g.
  `rules`) written *after* the `[triggers.match]` header is parsed by TOML into
  the match table, which silently made the trigger never fire (a phantom match
  field no event payload has). `from_config` now raises a clear error for this,
  so it surfaces in the Triggers tab instead of failing silently. The
  `docs/console.md` example is corrected (scalars before sub-tables).
- **No more spurious `dispatch runspec:trigger_status failed` at startup.** The
  trigger engine starts during window setup, before the web view is ready, so
  the one-shot status push raised (harmlessly) in `evaluate_js`. Removed it; the
  Triggers tab already pulls this state on mount.

### Changed
- **Trigger cards now show the agent's output.** An agent-mode trigger turn has
  no chat transcript, so its reply + any tool actions were invisible (only in
  the debug log). The turn now returns a summary that lands on the card, and the
  card surfaces *why* nothing ran (e.g. "No LLM provider configured") instead of
  sliding silently to Done. The card's result area is scrollable for longer
  replies.
- **Source-unavailable warnings only show for sources you use.** The Triggers
  tab no longer nags about a registered-but-unavailable source (e.g.
  `notifications` on a locked-down machine) unless a configured trigger actually
  references it. The warning is also dismissible.


## [0.29.1] — 2026-06-09

### Fixed
- **Notification-listener source installs on Python 3.13+.** The `notifications`
  extra now uses the maintained PyWinRT packages (`winrt-*`) instead of the
  archived `winsdk` (whose wheels stop at 3.12, forcing a from-source build that
  needs a C toolchain / Visual Studio). PyWinRT ships wheels through current
  Python versions, so `pip install runspec-console[notifications]` works with no
  build step. The Outlook source is unaffected (it never needed this extra).


## [0.29.0] — 2026-06-09

### Added
- **Event triggers — react to local Windows events and run runnables.** A new
  **Triggers** tab and a backend engine that lets the connected LLM (or a
  deterministic rule) react to things that happen on the machine. A *trigger*
  binds an event source + type (optionally narrowed by a regex `match`) to an
  action; matches flow across a kanban board (`Triggered → Triage → Awaiting
  human → Running → Done / Failed`). Triggers live in `runspec_triggers.toml`
  in the app config directory. See `docs/design/console-triggers.md`.
  - **Outlook source** (`source = "outlook"`): `new_mail` and `reminder` events
    via the signed-in desktop Outlook client over COM — no extra dependency,
    and it rides the existing SSO session. **Forward-only**: only mail/reminders
    that arrive while the console is running fire events; the backlog is never
    replayed.
  - **Notification-listener source** (`source = "notifications"`): every system
    toast — Teams, Slack, anything — through one subscription
    (`UserNotificationListener`). Needs the `notifications` extra (`pip install
    runspec-console[notifications]`) and a one-time Windows consent grant.
  - **Two routing modes:** `mode = "rule"` maps event-payload fields to a
    runnable's args (`{{ field }}`) and runs it; `mode = "agent"` runs a headless
    agent turn seeded with the trigger's `rules`, with the full runnable toolset.
  - **draft_reply:** an agent-mode trigger with `[triggers.reply] draft = true`
    can save a suggested reply to **Outlook Drafts** for you to review and send.
    It is **never sent** by the console, and every draft is confirm-gated.
  - **Safe by default:** every runnable a trigger drives goes through the same
    autonomy gate as chat (confirm-by-default); a parked confirm shows on the
    card as **Awaiting human** with inline Approve / Deny. Per-trigger `enabled`
    toggle and `post_to_chat` (mirror a run into the main chat) from the UI.


## [0.28.0] — 2026-06-09

### Added
- **Upload filtering.** Filters now apply to **uploads** too, not just downloads
  and relays. On an upload the pipeline runs **locally** over the local source
  (needs a POSIX `sh` on PATH); for a **fan-out** the source is filtered once and
  the small result is distributed to every host. The Transfer dialog shows the
  filter picker in both directions.
- **Filter import / export.** **Settings → Filters** gains **Import** / **Export**
  buttons (mirroring Jump Hosts) to share a `runspec_filters.toml` across machines
  or teammates. Backend: `import_filters(toml)` (skips names that already exist).
- **Agent file-transfer tools.** The chat agent can now run transfers itself via
  two built-in tools — `download_file` (remote → local) and `relay_file`
  (host → host) — each accepting an optional saved filter (and its value). The
  agent sees the configured hosts and every filter's name + description as the
  tool schema, so it can choose the right one ("pull today's log for txn-9281
  from prod"). Both are side-effecting, so they always go through the autonomy
  **confirm** gate; the operator approves before anything runs, and watches live
  progress in the transfer panel.


## [0.27.0] — 2026-06-09

### Added
- **Source-side filters for Relay and Download.** Define named, reusable stream
  filters in **Settings → Filters** (e.g. `grep -F -- {{arg}} | cut -d, -f1,4`),
  then pick one by name in the **Relay** or **Download** dialog to filter a file
  **on the source host** before it's transferred — only the matching output
  crosses the network. Pulling a few relevant lines out of a multi-gigabyte log
  no longer means copying the whole file.
  - The pipeline runs on the source over SSH `exec` with the file fed on stdin
    (works for relay's configured *and* ad-hoc/jump sources, and for a download's
    remote source). The source path is shell-quoted into an stdin redirect; it is
    never interpreted by the pipeline.
  - **Parameterized:** a `{{arg}}` placeholder in the pipeline prompts for a value
    at transfer time (labelled by the filter's *value label*), so one filter
    serves every identifier. The supplied value is shell-quoted on substitution.
  - Each filter carries a free-text **description** (shown in the picker) — also
    intended to give the agent context to choose a filter in future.
  - A pipeline that exits non-zero surfaces its stderr and removes the partial
    destination file; `grep`'s exit 1 with no stderr ("no lines matched") is
    treated as a legitimately empty result, not a failure. Progress shows bytes
    written (output size isn't known up front).
  - Filters apply to **Relay** and **Download** (pull) only — there's no remote
    source to filter on for an Upload. Saved to `runspec_filters.toml`.
  - Backend: `get_filters()` / `save_filters()`; `start_relay(...)` and
    `start_transfer(...)` gain optional `filter_name` / `filter_arg`.


## [0.26.0] — 2026-06-08

### Added
- **Browse for the runspec path when adding a jump host.** The **Settings → Jump
  Hosts** add/edit form's *runspec path(s) on host* fields now have the same
  autocomplete + file-browser as the Forms path pickers and the file-transfer
  dialogs — so you can navigate the host and pick where runspec lives instead of
  typing the path by hand.
  - Browses the host **straight from the form's connection details**
    (hostname / user / port / identity file) before it's saved — no jump, no
    prior probe required — using the same key/agent → `[ssh] proxy` →
    `~/.ssh/config` → host-key handling as every other SSH operation. The browse
    button stays disabled (with a hint) until a hostname is entered; the field is
    always editable by hand.
  - The browse connection is opened lazily, cached for the editing session, and
    torn down when the form is saved/cancelled or the drawer closes.
  - Backend: `list_dir_unsaved(hostname, path, user, port, identity_file, dirs_only)`.


## [0.25.1] — 2026-06-08

### Fixed
- **Copy SSH public key now goes through the `[ssh] proxy`.** 0.25.0's copy-id
  opened a *direct* connection, so it couldn't reach hosts that are only
  reachable via an HTTP CONNECT proxy — i.e. exactly the corporate fleets the
  feature is for. It now connects through the shared client builder, using the
  same `[ssh] proxy` / `host_key_checking` / `~/.ssh/config` handling as every
  other SSH operation in the console. Password auth still uses the password
  alone (no agent/on-disk-key fallback).


## [0.25.0] — 2026-06-08

### Added
- **Copy SSH public key to a host (in-app `ssh-copy-id`).** Authorize this
  machine on a freshly added host without leaving for PuTTY: enter the host's
  SSH password once and the console appends its public key to the remote's
  `~/.ssh/authorized_keys`, then re-probes so the sidebar dot flips green.
  - Available from the **host right-click menu** (offered even while the host is
    disconnected — that's the bootstrap case) and as a per-host **Copy key**
    button in **Settings → Jump Hosts**.
  - Connects **directly with the password only** (agent/key lookup disabled so
    the password path is genuinely tested). The password is used for that one
    connection and **never stored, logged, or pooled**.
  - Pushes the host's **effective** key (a per-host `identityFile` override wins
    over the global key) with the same idempotent append the key-rotation path
    uses, so re-running never duplicates the key. If no key is configured it asks
    you to **generate one first** rather than silently creating a key.
  - Backend: `copy_ssh_id(host, password)`.
  - *Known limitation:* the direct connection does not use the HTTP-CONNECT
    `[ssh] proxy`, so copy-id may not reach hosts only reachable via the proxy.


## [0.24.0] — 2026-06-07

### Added
- **Relay files between hosts (host → host, through the console).** A new toolbar
  action streams file(s) from a **source** host to a **destination** host without
  the two ever connecting to each other — the console pulls from one and pushes
  to the other in chunks. The win for jump-host fleets: the source can be an
  **ad-hoc target** (a hostname you type, not in config) reached **via a chosen
  jump host**, so you can pull a log off a box you don't keep configured straight
  onto the jump's filesystem for a local runnable to consume — using **your** SSH
  access, not a runnable's service-account password.
  - Auth tries your **key/agent first** (tunnelled through the jump via a
    `direct-tcpip` channel) and falls back to an **optional password** that is
    used for that one connection and **never stored, logged, or pooled**.
  - The source can also be a **configured host**. Bytes pass through your machine
    (fine for logs); reads happen as your login user (no sudo). Backend:
    `start_relay(source, source_paths, dest_host, dest_dir)` + a pure
    `fs_transfer.relay_items`; streams the usual `runspec:transfer_*` events.
  - **Browse the source** to pick file(s) — including an **ad-hoc target over the
    jump** — instead of typing paths: a `list_dir_adhoc` over an SFTP listing
    (the jump tunnel is reused warm from the pool), surfaced through the existing
    file browser via an injectable lister. Typing paths still works too. The
    target connection is **cached for the browse session** (and torn down when the
    dialog closes via `close_adhoc`), so navigating directories on a slow link
    handshakes the target only once instead of per click.

---


## [0.23.0] — 2026-06-07

### Added
- **Fan-out transfer — push the same file(s) to many hosts at once.** In the
  Transfer dialog's **Upload** direction the target-host selector is now
  multi-select: pick several hosts and the chosen source file(s) are uploaded to
  the same destination folder on **all of them in parallel** (one SFTP transfer
  per host over its own pooled transport). The dialog shows a **per-host progress
  bar** and a ✓/✗ per host at the end. The cluster move: push an updated config or reference-data file to every server in one action.
  A **pre-flight check** verifies the destination folder exists on each selected
  host (the dialog only browses it on the first host) and skips — with a clear
  warning — any host whose layout differs, so a mismatch is caught before sending.
  Backend: `start_fanout(source_paths, dest_hosts, dest_dir)` and
  `check_dir_exists(hosts, path)`; progress events
  carry a `host` field and `transfer_end` carries per-host results. Download and
  single-host upload are unchanged.

---


## [0.22.0] — 2026-06-07

### Added
- **File transfer (SFTP) — a Transfer dialog for copying files between your
  machine and a remote host.** A new toolbar action opens a dialog: pick a
  **direction** (download remote → local, or upload local → remote) and a remote
  host, then choose the **source file(s)** and **destination folder** using the same
  in-app file browser as the Forms path fields (so you browse the right host on
  each side). Multiple source files can be checked off at once. Progress streams
  live with a cancel button. v1 is **local↔remote only** (remote↔remote relay and
  fan-out to many hosts are planned follow-ups); directories aren't supported yet.
  - Transfers run over the host's **existing pooled SSH transport** via a new
    `open_sftp` on the connection pool (no new connection/auth) — `bridge`
    methods `start_transfer` / `cancel_transfer`, streaming
    `runspec:transfer_progress` / `runspec:transfer_end` events, mirroring how
    runnable output is streamed. New pure, unit-tested `fs_transfer` planning
    module and a `TransferModal` UI component.

---


## [0.21.0] — 2026-06-07

### Added
- **Service-account view in the path browser.** The file browser now has an
  identity toggle on remote hosts that have a `run_as` configured for the
  runnable: switch from **You** (your SSH login) to the runnable's service
  account and the listing re-runs as that user, so you see exactly the files the
  job itself will see. The escalation reuses the core `build_become_argv`
  (`sudo -u <svc> sh -c …` / `su` / etc.), identical to how the runnable is
  invoked — `list_dir(host, path, group, dirs_only, runnable, as_service_account)`.
  Inline autocomplete stays login-user (no sudo-per-keystroke); the toggle lives
  in the Browse modal. Local hosts and runnables without a `run_as` show no
  toggle (login-user only, unchanged).

---


## [0.20.0] — 2026-06-07

### Added
- **Browsable path fields in Forms.** `path` arguments now render as a picker
  instead of a plain text box: type-ahead autocomplete (debounced) as you enter
  a path, plus a folder button that opens an in-app file browser. Crucially, it
  browses **the host the runnable will run on** — local or the selected remote
  host — so you populate the field against the right filesystem instead of
  guessing or dropping out to a terminal. `ui = "directory"` restricts picking to
  folders (and adds a "Use this folder" action); otherwise files are selectable.
  - New bridge method `list_dir(host, path, group, dirs_only)` returning
    `{cwd, parent, entries, truncated}`. Local hosts list via `os.scandir`;
    remote hosts list with a single `find` command over the host's existing
    **pooled SSH transport** (no new connection or auth). Login-user identity for
    now; a service-account view (the same listing wrapped in the host's
    `run_as`/become) is a planned fast-follow, as is SFTP-backed file transfer.
  - New `fs_browse` module (pure, unit-tested) and a `PathInput` UI component.

---


## [0.19.0] — 2026-06-07

### Added
- **Version display, update check, and a documentation link.** The installed
  version now shows at the bottom of the host sidebar. On launch the console
  does a best-effort check for a newer release and, when one exists, shows an
  `↑ <version>` chip beside it (offline / blocked networks just show the
  version — no indicator, no error). A docs icon in the toolbar opens the
  documentation site.
  - The update check shells out to `pip` from the interpreter runspec-console
    is installed in (`sys.executable`), so it honours that venv's index
    configuration — an **internal PyPI proxy / mirror** is queried instead of
    public PyPI when the venv is pointed at one.
  - New `[console]` config section with a `docs_url` key (default
    `https://jasonfinestone.github.io/runspec/`) so a fork or internal mirror
    can retarget the docs link.
  - New bridge methods `app_version`, `check_for_update`, `docs_url`, and
    `open_docs`, backed by a dependency-free `version` helper (no `packaging`).
  - Fixed the stale in-tree `__version__` so the source-run fallback matches the
    packaged version.


## [0.18.0] — 2026-06-07

### Added
- **`require-command` is now visible everywhere a runnable is shown.** When a
  runnable (or a nested command) sets `require-command = true`, choosing one of
  its commands is mandatory — but the console gave no sign of that, so the run
  modal looked optional and the agent didn't know a command was needed.
  - **Discovery** tags every node with `requireCommand` (read from the CLI's
    normalised `require_command`, or the raw `require-command` for direct callers).
  - **Forms view** marks such runnables with a `required` chip on the card and a
    "command required — this runnable can't run on its own" banner above the
    subcommand picker.
  - **Spec view** shows a `one required` tag on the Subcommands heading (and a
    `command required` tag on any nested command that requires its own), and no
    longer leaks `require_command` into the raw "Custom" field list.
  - **Agent (LLM) tools** now expose a `command` parameter — an enum of the
    runnable's commands, summarised with their descriptions — and mark it
    **required** in the tool schema when the spec requires a command. Each
    command's own args are offered as optional, tagged inputs. The chosen
    `command` is threaded through execution as the command path, so the agent can
    finally run subcommands at all (previously it always ran at the top level and
    require-command runnables were unrunnable).


## [0.16.1] — 2026-06-05

### Fixed
- **Saving settings no longer needlessly drops SSH connections.** `save_config`
  rebuilt the SSH connection pool (closing every live connection) on *every*
  save — so changing the LLM system prompt or model name in Settings tore down
  all host connections. The pool now reacts only to `[ssh]` changes (it never
  consumed `[refresh]`), and even then it does the least disruptive thing: it
  rebuilds (the only case that drops connections) **only** when pooling is
  toggled on/off or `max_concurrent` changes; behavioural tunables
  (`keepalive`/`idle_ttl`/`max_sessions`/`backoff`) apply to the live pool in
  place via the new `SSHConnectionPool.update_settings`, and connection-shaping
  changes (`user`/`proxy`/`identityFile`) migrate lazily via the connection key.
  Unrelated saves leave warm connections untouched; the cached adapter is still
  invalidated on every save so LLM changes take effect immediately.
- **Misleading install hint for plugin providers.** When a plugin LLM provider
  (entry point / dotted path) failed to load because one of *its* dependencies
  was missing, the console swallowed the real `ImportError` and told the user to
  `pip install runspec-console[<provider>]` — an extra that doesn't exist. It now
  preserves the real cause (e.g. "No module named 'httpx'") and points at the
  plugin package. The built-in providers still get their `runspec-console[...]`
  extra hint.


## [0.16.0] — 2026-06-05

### Security
- **Remote invocations are now shell-quoted (command-injection fix).**
  `run_remote` executes through the remote login shell (`exec_command`), and the
  argv was space-joined without quoting — so an arg value containing `;`, `|`,
  `$(...)`, backticks, `&&`, or whitespace was interpreted by the shell, i.e.
  command injection (and worse under `sudo -u`). Every token is now quoted via
  `shell_join` before execution; a value is parsed back as a single literal
  argument. Local execution was never affected (it execs the argv list, no
  shell), and `build_become_argv`'s `su` path already quoted its inner command.
  Regression tests fire real payloads and assert single-token survival.

### Fixed
- **Forms view dropped global args for runnables with subcommands.** A runnable
  whose top-level `args` are global (inherited by every subcommand, per runspec
  0.20.0) showed only the subcommand picker, so its globals were never collected;
  leaf forms showed only their own args, never the inherited effective set. Now
  discovery merges each ancestor's args down the command path (child overrides
  parent on a name clash, mirroring core `_resolve_subcommand`) and tags the
  inherited ones. The leaf form renders them in an **Inherited** section above
  the command's own args, validated together in one pass. Intermediate
  `require-command` nodes stay picker-only. No change to the executor — the
  existing `binary <command path> <flags>` ordering already parses correctly
  (command tokens are resolved first, then all flags against the merged set).
  Blank optional fields are no longer emitted as `--flag ""`.
- **Positional args are sent positionally, not as `--name`.** A runnable arg
  with a `position` was emitted as `--name value`, which core rejects as an
  unknown argument — a hard failure on run for any runnable using positionals.
  The executor now emits positionals as bare values in position order (after the
  flags); the bridge resolves which args are positional from the spec (the leaf
  node for a subcommand). Covers both the forms and the agent paths.

### Added
- `SECURITY.md` — security posture and threat model for the console
  (execution/escalation model, SSH host-key verification, AI autonomy gate,
  input validation, secret handling, audit log, and known limitations).


## [0.15.0] — 2026-06-05

### Added
- **Custom LLM provider plugins.** `load_adapter` is now an open registry
  instead of a hardcoded dispatch, so an out-of-tree (e.g. proprietary,
  in-house) adapter can supply a provider without forking runspec-console.
  Three resolution paths: a `module:Class` **dotted-path** provider; the public
  **`register_adapter(name, factory)`** API (plus `[plugins] modules = [...]`
  to import self-registering modules at startup); and **entry-point discovery**
  via the `runspec_console.adapters` group (install a wheel and the provider
  appears — the recommended path). The SDK-free `ModelAdapter` / `ChatResponse`
  / `ToolCall` trio and `register_adapter` / `available_providers` are exported
  from the top-level `runspec_console` package as the stable plugin contract.
- **Provider dropdown is now dynamic** — `bridge.list_providers()` surfaces
  built-ins plus discovered plugins, so installed plugin providers are
  selectable in Settings → LLM (unknown providers get the common api_key /
  base_url / model / system fields).
- **`LangServeAdapter` is subclassable** — `chat()` routes through
  `_build_payload` / `_parse_output`, so a plugin for a non-vanilla
  LangServe-style gateway overrides just those two and inherits auth, token
  rotation, and the agent-loop plumbing.
- **Example plugin template** under
  `examples/console-adapter-plugin/` (pyproject with the entry point, an adapter
  subclassing `LangServeAdapter`, and a README) — a copy-paste start for an
  internal adapter package. Not built/tested as part of runspec-console. Ships
  with an `AGENTS.md` brief and a `.github/prompts/` Copilot prompt file for
  building the adapter with an AI agent, plus a fake-client conformance test.
- **Adapter conformance helpers** (`runspec_console.adapters.testing`):
  `assert_adapter_contract`, `assert_chat_response`, `assert_tool_turn` — a
  public, pytest-free way for plugin authors to validate an adapter against the
  contract (return types, the `stop_reason="tool_use"` invariant, `make_tool_turn`
  shape) before wiring it in.


## [0.14.0] — 2026-06-05

### Added
- **LangServe provider (`pip install runspec-console[langserve]`).** A fourth
  LLM adapter that drives a LangServe `/invoke` endpoint — a LangChain Runnable
  published over HTTP, the common shape of a corporate LLM gateway (often
  Bedrock/Claude underneath) when no native Anthropic/OpenAI API is available.
  Set `[llm] provider = "langserve"` and `base_url` to the endpoint. Tool
  calling is structured: runspec tools are sent as OpenAI-function dicts and the
  returned `AIMessage.tool_calls` map straight onto tool runs. Auth is a bearer
  token from a static `api_key` or a rotating `api_key_command` (same vending +
  TTL pattern as the Anthropic adapter). The gateway-specific input/output shape
  is configurable without code changes: `input_messages_key`, `input_tools_key`,
  `tools_in_config`, `auth_header`, `auth_scheme`. Selectable in
  Settings → LLM. The adapter's pure mapping helpers are unit-tested without
  `httpx` installed (mirrors `test_prompt_caching`).

### Added
- **Centralized SSH connection pool (#104).** The console now keeps one reused,
  keepalive'd SSH connection per host, shared by the connectivity probe,
  discovery, history, logs, and invocations. A warm connection serves new
  commands over fresh channels with no new handshake, so the 30s background
  refresh no longer produces a burst of brand-new handshakes per host per cycle
  — the root cause of *"Error reading SSH protocol banner"* against bastions
  with conservative `sshd MaxStartups` / rate limits. New `[ssh]` knobs:
  `pool` (default on; `false` restores legacy connect-per-call), `keepalive`,
  `max_sessions`, `idle_ttl`. New `[refresh]` section: `interval`,
  `max_concurrent` (caps concurrent *new* handshakes across the fleet),
  `jitter`, `backoff_base`, `backoff_max` (per-host exponential backoff so a
  dead host isn't re-hammered every cycle). See
  `docs/design/ssh-connection-pool.md`.
- **Clearer SSH failure messages.** A non-SSH banner (proxy / TLS middlebox /
  load balancer answering the port — paramiko's opaque `UnicodeDecodeError`
  inside "Error reading SSH protocol banner") and plain banner timeouts now
  render an actionable one-line cause instead of a raw traceback.
- **Pool activity in the Dev tab.** The pool surfaces its lifecycle in the Dev
  tab's `ssh` category: reconnects and connect-backoff always show
  (INFO/WARNING); per-cycle connection reuse, new handshakes, and idle reaps
  show under `--dev` (DEBUG) — so you can see at a glance that steady-state
  refresh reuses connections instead of re-handshaking.

### Fixed
- **Startup terminal noise.** The first refresh cycle previously ran from the
  Bridge constructor, before the window was attached — its background work
  leaked `dispatch … dropped: no window attached` warnings to the launching
  terminal, and any SSH error captured during that window was dropped instead
  of shown. The refresh watcher now starts when the window attaches, and SSH
  log records captured during pre-window startup are buffered and flushed to the
  Dev tab once it's ready.


## [0.12.0] — 2026-06-04

### Added
- **Run Node folders on Windows.** `run_local` now finds and invokes a Node
  runnable's `.cmd`/`.bat` shim (from `runspec bin`) on Windows — `.exe` first
  (Python venvs), then `.cmd`/`.bat` (Node folders) — running the batch shim
  through `cmd /c` since it isn't directly executable by `CreateProcess`.

### Notes
- **Node runnable output already shows in History.** Since `runspec-node`
  0.25.0 tees `console.log` into the audit log as `runspec.print` records (the
  same shape Python emits), the History tab surfaces a Node runnable's output
  with no console change. Added a regression test locking that the parser keeps
  including those records as log lines.


## [0.11.0] — 2026-06-04

### Added
- **Logs tab — per-venv log management.** A dedicated tab to view, compact, and
  prune per-invocation audit logs (`store = "per-run"`) on every venv the
  console manages. It drives `runspec logs <verb> --json` per venv — local via
  subprocess, remote via SSH — the same uniform interface discovery uses, so it
  works identically against local and remote hosts:
  - **Status** — per-runnable file count, archive count, disk size, and newest
    record, grouped by venv (read-only `runspec logs status --json`).
  - **Compact** / **Prune** — modal with a dry-run **Preview** and a confirmed
    **Apply**, scoped to a whole venv or a single runnable. Prune requires at
    least one policy (older-than / max-files / max-total-size); compact requires
    an age threshold.
  - **View** — a merged, timestamp-sorted stream for one runnable (archives
    included), rendered in a drawer.
- Bridge methods `logs_status` / `logs_view` / `logs_prune` / `logs_compact`.

### Changed
- Bumped the `runspec` floor to `>=0.29.0` for the new `runspec logs status`
  verb and the `--json` output the Logs tab parses.


## [0.10.0] — 2026-06-03

### Removed
- **Bundled `disk-usage`, `ping-host`, `check-port`, and `flush-dns` runnables.**
  These now ship in `runspec-windows` (a dependency on `win32`), where they have
  newer implementations. Keeping local copies meant the two packages declared the
  same `[project.scripts]` entry points, which conflict on install. The console
  surfaces the `runspec-windows` versions automatically via `runspec local`
  discovery, so there is no loss of functionality on Windows.



### Added
- **Grouped/section tables.** Runnable output shaped as a dict that wraps
  array(s)-of-objects now renders as titled section tables instead of an
  `[object Object]` grid: `{System:[…], Application:[…]}` becomes one table per
  key, and `{log, level, events:[…]}` renders the rows as a table with the
  scalar fields as a caption.
- **Expandable nested cells.** A nested object or array-of-objects inside a row
  expands inline (sub-table or pretty JSON) on click.

### Fixed
- **No more `[object Object]`.** The cell/value formatter now renders nested
  objects and arrays as compact JSON (and joins scalar arrays), so any runnable
  emitting nested JSON degrades gracefully instead of showing `[object Object]`.


## [0.8.0] — 2026-06-03

### Added
- **Windows runnables surface automatically.** Declared a dependency on
  `runspec-windows[graph]`, gated to `sys_platform == "win32"`, so its Windows
  system-admin + Microsoft 365 (Outlook/Teams/Calendar/OneDrive) runnables are
  discovered in the console UI without a separate install. No code changes —
  the marker keeps it off non-Windows installs and CI.


## [0.7.0] — 2026-06-03

### Changed
- **History & Analytics are now filename-agnostic.** The runnable a record
  belongs to is read from the record body (`extra.runnable`), not parsed from
  the log filename, and log discovery globs widened to match per-invocation
  files (`{runnable}.{ts}.{run_id}.log`, runspec `store = "per-run"`) and
  compacted `.gz` archives alongside the legacy `{runnable}.log`. This lets the
  console consume shared-venv per-invocation logs without fragmenting History
  or Analytics, while remaining fully compatible with existing single-file
  logs. `.gz` archives are read transparently (local) and decompressed over SSH
  (`gzip -dc`).

### Added
- **Crash frames in History/Analytics.** The structured `exc_structured` record
  (runspec ≥ 0.26 uncaught-exception handling) is lifted into a per-run
  `exception` field with the full call `frames`, instead of appearing as a
  generic `CRITICAL` log line — giving the UI a table-ready stack for failed
  runs.

---


## [0.6.0] — 2026-06-03

### Security
- **SSH host-key verification is now on by default.** `_make_ssh_client`
  previously installed paramiko's `AutoAddPolicy` with no `known_hosts` loaded,
  which accepts whatever key a server presents — host-key verification was
  effectively disabled, leaving connections open to man-in-the-middle
  impersonation (flagged by code scanning). Connections now default to
  **`accept-new`** (trust-on-first-use): a never-before-seen host is accepted
  and remembered, but a *changed* key for a known host is rejected. The user's
  `~/.ssh/known_hosts` is read for verification (never rewritten); accept-new
  additions persist to an app-managed `known_hosts` file.

  Configurable via `[ssh] host_key_checking` in `config.toml`:
  `"accept-new"` (default) or `"yes"`/`"strict"` (only connect to already-known
  hosts). An unrecognised value falls back to the safe `accept-new` default
  rather than disabling verification. There is deliberately **no** "accept
  anything, including changed keys" mode — that is the man-in-the-middle hole
  being closed, and accept-new already covers unattended first-contact trust.
  The app-managed file location is overridable via `[ssh] known_hosts`.

  **Note:** if a host's key legitimately changes (e.g. a rebuild), connections
  will now fail until the stale entry is removed — matching OpenSSH behaviour.


## [0.5.0] — 2026-06-03

### Added
- **Dev tab — SSH log capture.** A new `ssh` category in the Dev tab surfaces
  paramiko's connection lifecycle (connect / banner read / auth / disconnect)
  as timestamped, filterable, expandable rows — `ERROR` records (e.g. the
  `Error reading SSH protocol banner` banner-timeout traceback) render red and
  count toward the error tally. The `Bridge` installs a logging handler on the
  `paramiko` loggers that forwards records as `runspec:ssh` events and turns
  off propagation, so the raw tracebacks no longer leak into the terminal
  runspec-console was launched from. Capture widens to `DEBUG` (packet/kex
  detail) when started with `--dev`/`--devtools`.

### Changed
- **SSH connect resilience.** `_make_ssh_client` now sets explicit
  `banner_timeout` (30s) and `auth_timeout` (30s) on connect instead of
  relying on paramiko's tighter 15s defaults — a common cause of
  `Error reading SSH protocol banner` against slow servers, reverse-DNS-on-connect
  (`sshd UseDNS`), or new-connection throttling (`sshd MaxStartups`). All three
  connect timeouts are overridable via `[ssh]` in `config.toml`
  (`connect_timeout`, `banner_timeout`, `auth_timeout`).

See `docs/design/paramiko-errors-in-terminal.md` for the full diagnosis.


## [0.4.0] — 2026-06-02

### Added
- **Analytics tab — fleet-wide usage insight.** A new **Analytics** tab mines the
  JSON-lines audit logs that already live on every host into rich, drill-down
  charts aimed at engineer-level users. A new `get_analytics` bridge method scans
  the full (uncapped) logs across the fleet's venvs concurrently — local files
  directly, remote hosts over the same SSH framing as History — buckets every run
  by day and by host/group/runnable/operator/initiated-by/autonomy, computes
  duration percentiles and a top-exceptions ranking, and degrades gracefully per
  host (`partial`/`errors`) when a scan fails. The view (built on
  `@ant-design/plots`, lazy-loaded so it stays out of the initial bundle) shows KPI
  cards, runs-over-time with a success/failure split, per-day success rate,
  duration p50/p95 trend, top runnables, operator and user-vs-agent breakdowns, and
  a top-exceptions table. A filter bar (date range, host, group, runnable, operator,
  user/LLM) plus click-to-drill on the charts re-filters everything instantly from
  the pre-aggregated buckets, and the charts follow the dark/light theme.
- The log parsers gained an opt-in `include_extra` flag that surfaces the rich
  `run_summary` fields (autonomy, per-level event counts, exception, command path)
  for analytics. The History tab's record shape is unchanged.

### Added
- **Dev tab — a domain-specific DevTools for the console.** A new **Dev** tab
  surfaces the app's live, in-session activity across both sides of the pywebview
  bridge: every frontend→backend bridge call (with arguments, duration, and
  result/error) and every backend→frontend `runspec:*` event (output, tokens,
  tool calls, run completion, usage, discovery). It renders as a filterable,
  auto-scrolling timeline with expandable JSON rows, category filters, counters,
  and pause/clear. Capture comes from two instrumentation choke points — a wrap of
  the bridge `Proxy` and a one-time `window.dispatchEvent` tap — so coverage is
  complete with minimal wiring. High-frequency token/output streams are coalesced
  into a single row with a count badge. The buffer is in-memory only and resets
  each launch (no disk persistence).
- **Reach the native Chromium inspector from a production build.** pywebview's
  Chromium inspector was previously available only in `--dev`. A new `--devtools`
  flag (or `RUNSPEC_CONSOLE_DEVTOOLS=1`) now enables debug mode in production so
  right-click → Inspect / F12 works. The Dev tab's **Open Chromium Inspector**
  button is gated on the backend's `is_debug_enabled()` and is explicit about the
  pywebview limitation (there is no reliable cross-platform programmatic open).

### Internal
- Established Vitest in `console-ui` with a `devbus` unit suite, and wired a
  `console-ui` job (typecheck + test) into CI alongside Python tests covering the
  debug-resolution precedence and the new `Bridge` debug helpers.


## [0.2.1] — 2026-06-02

### Fixed
- **The Console now actually escalates with `run_as`.** The SSH executor
  (`run_remote`/`run_local`) and the chat bridge (`_run_tool_sync`/
  `invoke_runnable`) resolved `run_as` per host but never applied it, so a
  runnable declaring `run_as`/`become_method` still ran as the login user —
  the same gap that was fixed core-side. Both paths now build the escalation
  command via the shared `runspec.become` module and run through it, so manual
  runs *and* agent/chat tool calls escalate for real. An empty `run_as` never
  escalates. Requires `runspec >= 0.21.0` (the dependency floor is raised to
  match, since the executor now imports `runspec.become`).


## [0.2.0] — 2026-06-02

### Fixed
- **The agent chat now remembers prior turns.** Each message previously started a brand-new conversation — the model never saw earlier turns, so a follow-up like "restart the first one" had no idea what "the first one" was. The bridge now keeps one rolling conversation per app session (`_chat_history`): every turn appends the user message, the assistant's reply, and any tool_use/tool_result turns, and the next message is sent with the full history. A conversation lock serialises turns so two quick sends can't interleave. The history is trimmed to the most recent messages (without ever orphaning a tool_result) so a long-lived session doesn't grow context unbounded. New **New chat** button in the Console clears the transcript and the model's memory (`bridge.clear_chat()`).

### Added
- **Configurable system prompt.** Settings → LLM now has a **System prompt** field (`[llm] system`) for standing instructions applied to every chat turn — site policy, host context, tone — instead of the hardcoded default. `_get_adapter` passes it to the Anthropic/OpenAI/Bedrock adapter (omitted when blank, so each adapter keeps its default), and `save_config` already clears the cached adapter so it takes effect on the next message. It's part of the cached prefix, so editing it invalidates the cache once. Note: this is *guidance*, not enforcement — hard rules belong in a runnable's autonomy level, not the prompt.
- **Stop button for the agent chat.** An in-flight chat turn can now be halted — `cancel_chat(chat_id)` sets a per-turn cancel event that the agentic loop checks at safe points (before each model call, while streaming, and before running tools), so it stops without further model calls or host actions. A tool already executing is left to finish; the partial reply is kept in history (text-only, never a dangling tool_use), and a `⏹ Stopped.` notice is shown. Stop is available in **two places**: the command bar's send arrow flips to a red **Stop** control while the assistant is working (and back to send when it finishes), and there's also a Stop button on the running chat block. Stop also works **while an Approve/Deny prompt is up** — `cancel_chat` wakes the gate's pending wait (it would otherwise sit until you answer or the 5-minute timeout), refuses the tool without running it, dismisses the dialog, and halts the turn. This is a safety brake: an agentic turn can otherwise fire up to 10 rounds of tool calls against your hosts.
- **Prompt caching on the Anthropic and Bedrock adapters.** The agentic chat loop ships the full tool-schema list (one entry per discovered runnable — often 30–60 with `runspec-linux` plus jump hosts) on *every* turn. That block is static within a session, so we now mark it with an ephemeral `cache_control` breakpoint: the last tool definition is annotated (caching the whole tools block) and the system prompt is sent as a cached text block (caching tools+system together, since they render before the messages). Subsequent turns read the prefix at ~0.1× input cost instead of full price, with no change to behavior or the autonomy model — every action is still a normal gated tool call. Below the model's minimum cacheable prefix (~2K tokens on Sonnet 4.6, ~4K on Opus/Haiku) the API silently skips caching, so small tool sets are unaffected. Verify hits via `usage.cache_read_input_tokens`.

- **Cache visibility in the Console token counter.** The per-turn usage chip (`↑prompt ↓output`) now reports the *full* prompt size (uncached + cache read + cache write) and, when caching is active, adds a green `⚡N% cached` chip — the share of the prompt served from cache at ~10% cost. Hover either chip for the full breakdown. This is how you confirm caching is working: the first turn shows 0% (cache write), and subsequent turns in the session show a high hit rate. `runspec:chat_usage` now carries `cache_read_tokens` and `cache_creation_tokens`.

### Internal
- New pure helper `adapters.base.apply_prompt_caching(kwargs)` applies the breakpoints to a `messages.create()`/`.stream()` kwargs dict; it's SDK-free and unit-tested (`tests/test_prompt_caching.py`) without `anthropic` installed. Both adapters call it in `chat`, `stream_chat`, and `stream_with_tools` after assembling kwargs. The helper copies the tool dicts it annotates so the caller's shared tool list is never mutated.
- `Bridge._usage_from_response` now returns a dict (`input`/`output`/`cache_read`/`cache_creation`) instead of a 2-tuple, reading Anthropic/Bedrock cache fields (OpenAI's prompt/completion totals resolve cache fields to 0). Covered by `tests/test_usage.py`.


## [0.1.14] — 2026-06-01

### Fixed
- **The autonomy confirm prompt (0.1.13) never reached the UI — gated chat runs always "declined".** The Approve/Deny prompt was dispatched from the same worker thread that then blocked waiting for the answer (`asyncio.to_thread(_gated_run_tool)`), and the `runspec:tool_confirm` event didn't reliably reach the WebView from there. With nothing to answer, every `confirm`/`supervised` chat run sat for 5 minutes and then timed out as a deny — the symptom was a long spinner followed by a "you declined the confirmation" message. The prompt is now dispatched from the chat event-loop thread (the same path that streams tokens, which reaches the UI), and **only** the blocking wait is offloaded to a worker thread. The confirm dialog is also centered, sits above the frameless-window chrome (`zIndex`), and no longer treats a backdrop click as a deny.

### Diagnostics
- The autonomy gate now logs each step via `logging.getLogger(__name__)` (routed by runspec's logging — `--debug` for more): the decision, the prompt dispatch + `request_id`, the UI's answer, and any 5-minute timeout. `_dispatch` no longer swallows `evaluate_js` failures silently — it logs them. The frontend logs `tool_confirm` receipt and resolution to the dev console. Read these first when a gated run misbehaves.

### Internal
- New async gate `_gated_run_tool_async` (used by the agentic loop) emits the prompt on the event-loop thread; the synchronous `_gated_run_tool` is retained for direct/unit-test use. Both share `_confirm_decision` / `_register_confirm` / `_finish_confirm`.


## [0.1.13] — 2026-06-01

### Fixed
- **Autonomy is now enforced on the agent (chat) path.** Previously the LLM ran any runnable immediately regardless of its `autonomy` — the chip was shown but never gated. Now, before the assistant runs a tool:
  - `autonomous` → runs immediately (unchanged);
  - `confirm` / `supervised` → an **Approve / Deny** dialog appears and the run waits for your decision (denial is reported back to the assistant so it adapts; no answer within 5 min = deny);
  - `manual` → **hard-blocked** — the assistant cannot run it and is told to ask you to use the Run button.
  Per-arg `autonomy` escalates the level when that arg is supplied (most-restrictive wins). The manual **Run** button is unaffected — a human clicking Run has already chosen the action.

  > **Note:** this prompt did not reliably appear in practice — see 0.1.14, which fixes the event-dispatch thread so the dialog actually shows.

### Bridge
- New `resolve_tool_confirmation(request_id, approved)` callback; chat tool-calls route through an autonomy gate that emits a `runspec:tool_confirm` event and blocks until the UI answers.


## [0.1.12] — 2026-06-01

### Added
- **Settings → SSH: HTTP proxy field.** The `[ssh] proxy` setting (0.1.11) is now configurable from Settings → SSH instead of hand-editing `config.toml`. A companion **Honor ~/.ssh/config** switch toggles `[ssh] use_ssh_config`.
- **Persistent public key.** Key generation now writes a `<key>.pub` next to the private key (and rotation rotates it alongside the `.ppk`). Settings → SSH shows the public key with a copy button **at any time**, not just immediately after generating — and `get_public_key()` derives it from the private key for keys created before this release (no `.pub` yet), writing the `.pub` for next time.

### Fixed
- After generating a key, the public key was only shown once and then unrecoverable in-app (no `.pub` was written). It can now always be copied from Settings.


## [0.1.11] — 2026-06-01

### Added
- **Anthropic adapter — custom `base_url`.** Set `[llm] base_url` in `config.toml` to point the Anthropic provider at a corporate AI proxy. Forwarded to `anthropic.AsyncAnthropic`.
- **Anthropic adapter — dynamic API key via command.** `[llm] api_key_command` runs a shell command whose stdout (stripped) is used as the API key — mirrors Claude Code's `apiKeyHelper`, for token-vending helpers and credential managers. `[llm] api_key_ttl_ms` controls how long the result is cached before the command is re-run (`0` re-runs every request). The static `api_key` path is unchanged.
- **SSH through an HTTP proxy.** `[ssh] proxy = "http://proxy.corp:8080"` in `config.toml` tunnels remote runnable execution/discovery (paramiko) through an HTTP `CONNECT` proxy, and the *Launch terminal* button routes PuTTY through the same proxy (via a maintained `runspec-console-proxy` saved session — PuTTY has no proxy CLI flag). No proxy auth — front an auth-required proxy with a local bridge (cntlm/px) and point at `http://localhost:<port>`.
- **`~/.ssh/config` support (opt-in).** `[ssh] use_ssh_config = true` makes the paramiko path consult `~/.ssh/config` for `HostName`, `User`, `Port`, `IdentityFile`, and `ProxyCommand`. (PuTTY does not read `~/.ssh/config`; its proxy comes from the `[ssh] proxy` setting above.) Explicit `[ssh]` fields take precedence over `~/.ssh/config`. ed25519 keys already work on both paths — point `identityFile` at the private key.

### Internal
- `Bridge._get_adapter` threads `api_key_command`/`api_key_ttl_ms` through `load_adapter` and skips its adapter cache when a command is set, so the adapter's TTL governs refresh.
- The Anthropic client is built lazily when a command is configured; a refresh runs at the top of `chat`, `stream_chat`, and `stream_with_tools`. Empty command output raises `RuntimeError`.
- Bump minimum `runspec` to `>=0.20.2` (subcommand global-arg inheritance, help usage-line ordering, hyphenated-arg validation fixes).
- **CI** — runspec-console now has lint/format/test jobs in the umbrella `CI` workflow (it previously had none). Typecheck is omitted for now pending cleanup of pre-existing mypy debt.


## [0.1.10] — 2026-05-31

### Added
- **Custom title bar** — blue (#1677ff) 36px strip at the top of the frameless window. Shows `runspec console` on the left and minimize/maximize/close buttons on the right. Drag the bar to move the window; double-click to toggle maximize.
- **Window resize handles** — invisible 8px strips on all four edges and corners. Drag to resize via the existing `resize_window` bridge method.
- **PuTTYgen integration** — the *SSH key* section in Settings → General now prefers PuTTYgen. When PuTTY is installed alongside the configured SSH binary, a *Launch PuTTYgen* button opens it for key generation. When PuTTY is missing, the section shows a *Browse for PuTTY…* picker plus a *Download PuTTY for Windows* link.
- **Authorized-keys helper** — once a public key is available the section renders a one-shot copy block: `echo "PUBLIC_KEY" >> ~/.ssh/authorized_keys`.
- **Quick connect** — pick any connected jump host from a dropdown and click *Open PuTTY* to spawn a PuTTY GUI window for that host.
- **SSH binary picker** — the *SSH client binary* field now has a folder-open button next to it that opens a native Windows file picker.

### Bridge
- New methods: `puttygen_path()`, `launch_puttygen()`, `open_putty_url(url)`, `browse_ssh_binary()`.

### Fixed
- `launch_terminal` (and the new `puttygen_path`) now find `putty.exe` even when the configured SSH binary is a bare name like `plink.exe` (previously `Path("plink.exe").parent` resolved to `.` and the lookup always failed). New `_find_putty_exe` helper checks the SSH binary's directory, common Windows install locations (`C:\Program Files\PuTTY`, `C:\Program Files (x86)\PuTTY`), and the system `PATH`. The error message now points users at *Settings → SSH client binary* with a concrete example path.
- **`_dict_to_toml` now escapes backslashes and double quotes in string values.** Windows paths such as `C:\Program Files\PuTTY\plink.exe` were being written verbatim to `config.toml`; `tomllib` then refused to re-parse the file (`\P` is not a valid TOML escape) and the SSH binary setting silently reverted on every restart. Backslashes (`\` → `\\`) and embedded double-quotes (`"` → `\"`) are now escaped, and a `tests/test_config.py` round-trip suite locks in the behaviour for common Windows paths.
- **Bridge save paths normalise filesystem paths to forward slashes.** `browse_ssh_binary`, `save_config` (`ssh.binary`, `ssh.identityFile`), and `save_jump_hosts` (`identityFile`, `runspec_paths`) all convert `\` → `/` before writing. Windows file APIs accept either separator, and forward-slash paths sidestep TOML escaping entirely so `config.toml` and `runspec_hosts.toml` stay readable. The `_dict_to_toml` escape fix above stays as a safety net for any other string values.


## [0.1.9] — 2026-05-29

### Changed
- SSH terminal now launches PuTTY in a separate window instead of embedding xterm.js — simpler, no PTY resize issues, full PuTTY feature set
- Removed @xterm/xterm and @xterm/addon-fit dependencies (wheel size reduced)


## [0.1.8] — 2026-05-29

### Fixed
- Display name in log operator field now shows full Windows display name (`GetUserNameEx(3)`) instead of login name
- Local history now correctly reads from `~/logs` fallback when the venv path has no logs directory
- Settings drawer footer now shows correct config filename (`config.toml` in `%APPDATA%\runspec-console\`)


## 0.1.7 (2026-05-29)

### Added
- SSH terminal tabs: right-click any connected remote host in the sidebar → **Open SSH terminal**
- Full xterm.js terminal with ANSI colour support, scrollback, and dark theme matching the app
- Multiple terminal tabs open simultaneously, each closeable with ×
- Terminal panes stay mounted when switching to other tabs (session preserved)
- Uses plink (PuTTY) on Windows for reliable PTY allocation; falls back to OpenSSH `ssh -t`
- `open_terminal`, `terminal_input`, `resize_terminal`, `close_terminal` methods on Bridge
- 19 unit tests covering terminal session lifecycle in `tests/test_terminal.py`

## [0.1.6] — 2026-05-28

### Added
- **Configurable SSH client** — a new *SSH client binary* field in Settings → General lets you point runspec-console at any SSH-compatible binary. Set it to `plink.exe` (or a full path) to use PuTTY's plink instead of the Windows OpenSSH client — useful on corporate machines where the built-in OpenSSH client has MAC negotiation issues. The setting is stored as `[ssh] binary` in `runspec_config.toml` and applies to all SSH operations: connectivity probes, runnable discovery, invocation, history retrieval, and host tests. plink's `-batch` / `-connecttimeout` flags are used automatically when plink is detected.

---

## [0.1.5] — 2026-05-28

### Fixed
- **Window controls non-functional** — `minimize_window`, `toggle_maximize_window`, `close_window`, `resize_window`, and `move_window` were missing from `bridge.py`. The custom title bar buttons and resize handles now work correctly.

---

## [0.1.4] — 2026-05-28

### Fixed
- **Corrupted wheel** — `bridge.py` was written with null bytes during the 0.1.3 build due to a stale Linux mount cache. The wheel now contains clean source files and imports correctly.

---

## [0.1.3] — 2026-05-28

### Added
- **Smart output rendering** — run blocks that produce a JSON array of objects are automatically rendered as a sortable table; JSON objects render as a key-value grid. Numeric fields with `_mb`, `_kb`, `_bytes`, `_pct`, or `_seconds` suffixes are humanised (e.g. `1073741824 bytes` → `1.0 GB`).
- **View toggle** — table/grid blocks show table-view and raw-output toggle buttons so you can switch between the structured view and the raw JSON at any time.
- **Copy output** — each run block has a copy-to-clipboard button; copies the formatted table text when in table view, raw JSON otherwise. Uses `execCommand` fallback for WebView2 compatibility.
- **Ask LLM** — each run block has a robot button that forwards the raw JSON output into the chat input so the LLM can reason over it.
- **`RUNSPEC_AGENT=1` env var** — runnables invoked via the LLM chat (MCP tool calls) now have `RUNSPEC_AGENT=1` set in their environment, so they can detect agent context and adjust output format accordingly.

---

## [0.1.2] — 2026-05-27

### Added
- **Multi-venv host support** — hosts config now accepts `runspec_paths` (a list of runspec binary paths, one per virtual environment). A single jump host entry can now target multiple venvs; the console discovers runnables from all of them and routes invocations to the correct one automatically