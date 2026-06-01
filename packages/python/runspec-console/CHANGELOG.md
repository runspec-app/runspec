# Changelog — runspec-console

All notable changes to this package are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers follow [Semantic Versioning](https://semver.org/).

---


## [0.1.13] — 2026-06-01

### Fixed
- **Autonomy is now enforced on the agent (chat) path.** Previously the LLM ran any runnable immediately regardless of its `autonomy` — the chip was shown but never gated. Now, before the assistant runs a tool:
  - `autonomous` → runs immediately (unchanged);
  - `confirm` / `supervised` → an **Approve / Deny** dialog appears and the run waits for your decision (denial is reported back to the assistant so it adapts; no answer within 5 min = deny);
  - `manual` → **hard-blocked** — the assistant cannot run it and is told to ask you to use the Run button.
  Per-arg `autonomy` escalates the level when that arg is supplied (most-restrictive wins). The manual **Run** button is unaffected — a human clicking Run has already chosen the action.

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