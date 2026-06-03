# Changelog — runspec-console

All notable changes to this package are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers follow [Semantic Versioning](https://semver.org/).

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