# Console Terminal tab — an embedded, PTY-backed local terminal

**Status:** parked design (2026-09-16). Investigated and decided with the
operator; **not yet built**. Kept here so it can be picked up later without
re-doing the research. Companion to `console-mcp-server.md` (the optional
Claude Code ↔ console MCP attach described below).

---

## Goal

A full-featured, modern **local terminal inside the console** — a real shell
for arbitrary commands, with multiple tabs, in which the operator can also run
the Claude Code CLI (`claude`) when it happens to be installed. Claude Code is
**not** a dependency: not every user has it approved, so the tab must be a
first-class terminal on its own, with Claude Code as an optional convenience
launcher that appears only when `claude` is found on the machine.

## What exists today

- No embedded terminal. `launch_terminal(host)` opens PuTTY and
  `launch_local_terminal()` opens Windows Terminal / PowerShell / cmd in a
  **separate window** (`runspec_console/bridge_internals.py`, `launch_*`).
  These stay untouched.
- Console **0.1.7 shipped an embedded xterm.js SSH terminal and 0.1.9 removed
  it** citing "PTY resize issues" and wheel size (CHANGELOG 0.1.7 / 0.1.9).
  Reading that code (tag `console-v0.1.7`: `TerminalTab.tsx` +
  `bridge.py` `open_terminal`/`terminal_input`/`resize_terminal`/`close_terminal`)
  shows both problems were design artefacts, not inherent limits: it piped
  `plink -t` over ordinary stdin/stdout **pipes** (no PTY, so
  `resize_terminal` was a documented no-op) and pushed every 4 KB chunk as
  base64 through the single serialised `evaluate_js` dispatch queue. This
  design avoids both with a **real PTY** (ConPTY via `pywinpty` on Windows,
  stdlib `pty` elsewhere) and a **loopback WebSocket** for the byte stream.
- Existing Claude Code integrations all point the other way: `runspec-claude`
  drives `claude -p` headlessly; the console-as-MCP-server and `runspec-mcp`
  let an external Claude Code call *into* the fleet. Nothing launches Claude
  Code *from* the console.

## Options considered

| Option | Verdict |
|---|---|
| **Embedded xterm.js terminal + real PTY** | **Chosen.** Most work, most value; the only option that gives a terminal *inside* the app. |
| External terminal with a Claude Code launcher (extend `launch_local_terminal`) | Rejected as the main path: days of work but nothing embedded. |
| Native Claude Code pane via the Agent SDK / `stream-json` | Rejected: not a terminal, loses Claude Code's own TUI, and makes Claude Code a dependency. |
| ghostty-web instead of xterm.js | Not for v1: young project, addon coverage unconfirmed. Its xterm-compatible API means it can be swapped in later behind `TerminalPane`. |

Decisions taken with the operator:

- Embedded xterm.js **approved**; new deps **approved** (`@xterm/*` on the
  frontend, a Python `terminal` extra).
- The Terminal tab **stays when `[console] ai = false`** — a shell is a fleet
  tool, and Claude Code is an external agent exactly like the MCP-server case.
  UI copy never names the `ai` switch (the "no advertising" rule in
  `console-ai-off.md`).
- The Claude Code launcher does **not** auto-wire to the console MCP server;
  attaching is an explicit opt-in switch in the launcher, off by default.

Claude Code facts that shaped this (official docs): interactive `claude`
needs a real TTY; native Windows is supported (PowerShell/CMD; Git for Windows
optional via `CLAUDE_CODE_GIT_BASH_PATH`); ConPTY terminals are fully
supported; `--mcp-config <file|json>` attaches MCP servers at launch (HTTP
transport with `headers`); `--continue` / `--resume <id>` / `--add-dir` /
`--model` / `--permission-mode` exist; there is no first-prompt flag for
interactive mode; corporate env is the standard `ANTHROPIC_BASE_URL`,
`CLAUDE_CODE_USE_BEDROCK`, `HTTPS_PROXY`, `NODE_EXTRA_CA_CERTS`,
`CLAUDE_CONFIG_DIR`.

---

## Architecture (v1)

```
React TerminalView ──pywebview bridge──▶ TerminalMixin  (open/close/list/profiles/cwd options/config)
   │  xterm.js pane                          │
   └──ws://127.0.0.1:<port>/pty/<sid>?t=<token>──▶ TerminalWsServer ──▶ TerminalSession ──▶ PtySession (ConPTY | posix pty)
        binary frames = raw PTY bytes both ways; text frame {"t":"resize"} client→server, {"t":"exit"} server→client
```

**Why a WebSocket, not the bridge.** The UI push channel is one FIFO drained
by one thread (`bridge.py` `_dispatch` / `_ui_dispatch_loop`; the comment
above `Bridge.__init__` explains why concurrent `evaluate_js` was racy) and
it is shared with confirm cards and chat tokens. A TUI repaint at 30–60 Hz
would sit in that queue and add per-`evaluate_js` latency to every keystroke
echo. The bridge stays for low-volume control calls; the only new dispatch
event is `runspec:terminal_exit`.

**Security posture** (to go into `docs/security.md` in this language): the
Terminal is a **human-typed-only** surface. The only write path into a PTY is
keystrokes from the console's own bundled UI. **No agent — chat, trigger,
schedule, or external MCP client — gets any tool that opens, types into, or
reads a terminal.** The PTY WebSocket is loopback-only, ephemeral-port,
**Origin-pinned** to the console's own UI origin, and requires a
**per-session secret** that exists only in the console process and its
WebView (handed out via the trusted pywebview bridge), so a web page in
another browser cannot obtain a shell. Governance (autonomy gates, History)
applies to what the operator runs *through the console's MCP server* from
inside a Claude Code session, exactly as with a terminal opened outside the
console.

---

## Backend

### `runspec_console/terminal.py` (new; pure, no Bridge import, unit-testable)

- `available() -> tuple[bool, str]` — `websockets` importable; on win32 also
  `winpty`. Same idiom as `mcp_server.available()`.
- `PtySession` protocol: `read(n) -> bytes` (blocking, `b""` on EOF),
  `write(bytes)`, `resize(cols, rows)`, `alive()`, `exit_code()`, `kill()`
  (whole tree).
  - `_WinPty`: `winpty.PtyProcess.spawn(argv, cwd=, env=, dimensions=(rows, cols))`;
    `kill` = `terminate()` then `taskkill /T /F /PID`. No `CREATE_NO_WINDOW`
    needed — ConPTY children attach to the pseudoconsole.
  - `_PosixPty`: `pty.fork()` → child `os.chdir` + `os.execvpe`; parent
    `os.read(fd)`; resize via `fcntl.ioctl(TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))`;
    `kill` = `os.killpg(SIGHUP)` → `SIGKILL` after 2 s; exit via
    `os.waitpid(WNOHANG)`.
  - `spawn_pty(argv, cwd, env, cols, rows)` dispatches on `sys.platform`.
- Env: `os.environ` + `TERM=xterm-256color`, `COLORTERM=truecolor`,
  `TERM_PROGRAM=runspec-console` + `[terminal.env]` + profile env. Do **not**
  reuse `executor._UTF8_ENV`.
- `Profile` dataclass: `id, name, kind ("shell"|"claude"|"custom"), argv, cwd,
  env, shift_enter_newline, ctrl_v_paste, available, reason`.
  - `builtin_profiles(which=shutil.which, platform=sys.platform, environ=os.environ)`:
    Windows → `pwsh.exe`, `powershell.exe`, `cmd.exe`, Git Bash
    (`%ProgramFiles%\Git\bin\bash.exe`), WSL (`wsl.exe`), each only if found;
    POSIX → `$SHELL` (fallback `/bin/sh`).
  - `claude_profile(cfg, which)`: present only when `[terminal] claude_path`
    or `which("claude")` resolves **and** `[terminal] claude_profile` is true.
    On Windows npm's `claude` is `claude.cmd` — ConPTY can't exec it directly,
    so wrap as `["cmd.exe", "/d", "/c", path, *args]` when the path ends
    `.cmd`/`.bat`; a native `claude.exe` spawns directly.
  - `claude_argv(profile, *, mcp_config_path, continue_session, resume_id, add_dirs)`;
    `--mcp-config` only when a path is given, never `--strict-mcp-config`
    (the operator's own servers stay).
  - `write_claude_mcp_config(dir, sid, url, token) -> Path`: Claude's
    `mcpServers` JSON with `{"type":"http","url":…,"headers":{"Authorization":"Bearer …"}}`,
    written 0600 with the same `os.open` recipe as
    `bridge_mcp_server._write_mcp_server_discovery`, under
    `{app_dir}/terminal/`, deleted on session close.
- `TerminalSession`: id, profile_id, title, cwd, argv, cols/rows, exit_code,
  `token = secrets.token_urlsafe(32)` (never in `list()` output); daemon
  reader thread `term-read-<id>`; 256 KB ring buffer for replay on
  (re)attach; single attached sink; `attach(sink) -> replay bytes`,
  `detach()`, `write`, `resize`, `close`; `on_exit` callback.
- `TerminalManager(spawn=spawn_pty, max_sessions=8, on_exit=None)`:
  `open(profile, cwd, cols, rows, extra_argv=(), env_overlay=None)` (raises
  `ValueError` over max / profile unavailable / cwd missing), `get`,
  `list() -> list[dict]`, `close(id)`, `stop_all()`.
- `TerminalWsServer(manager, allowed_origins)`: `start() -> port` (lazy,
  idempotent; `websockets.sync.server.serve(handler, "127.0.0.1", 0)` on a
  daemon thread `term-ws`), `stop()`. Handler: path `/pty/<sid>?t=<token>`;
  reject an Origin not in the allowlist → close **4403** (log at WARNING);
  unknown sid / `hmac.compare_digest` mismatch → **4401**; a second attach
  kicks the first with **4409**; then replay, binary frames →
  `session.write`, text `{"t":"resize"}` → `session.resize`; on exit send
  text `{"t":"exit","code":N}`; `finally: detach()`.

### `runspec_console/bridge_terminal.py` (new mixin `TerminalMixin`; add to the `Bridge` bases)

Public JS API (add every name to `EXPECTED` in
`tests/test_bridge_api_surface.py`):

| Method | Returns |
|---|---|
| `terminal_available()` | `{ok, reason, enabled}` |
| `terminal_profiles()` | `TerminalProfile[]` (built-ins + `[[terminal.profiles]]` + Claude; per-entry `available`/`reason`; env values never exposed) |
| `terminal_cwd_options()` | `[{label, path, kind}]` from `current_workspace_path()` (`bridge_transfer.py`), each `repos.checkout_dir(name)` that exists, `config.app_dir()`, `Path.home()` |
| `terminal_open(payload)` | payload `{profile_id, cwd?, cols, rows, claude?: {mcp: bool, continue: bool, resume?: str, add_dirs?: string[]}}` → `TerminalSessionInfo & {ws_url}`; starts the WS server lazily; for `kind == "claude"` with `mcp` true **and** `self._mcp_server.status()` running → write the mcp config file using the `[mcp_server]` URL + token from `config.read_mcp_server()` |
| `terminal_close(id)` | kills the tree, removes the mcp config file |
| `terminal_list()` | `TerminalSessionInfo[]` `{id, profile_id, title, cwd, exit_code, created_at}` |
| `get_terminal_config()` | `config.read_terminal()` |

Private: `_terminal` (manager) + `_terminal_ws` created in `Bridge.__init__`;
`_set_ui_origin(url)` called from `app.py` once the UI URL is known
(`http://localhost:<port>` in `--dev`, `http://127.0.0.1:<static port>` live
— **both** must be allowed or the dev loop breaks silently); session exit →
`self._dispatch("runspec:terminal_exit", {id, exit_code})`. Register
`atexit.register(self._terminal_shutdown)` (= `_terminal_ws.stop();
_terminal.stop_all()`) right after `atexit.register(self._mcp_server.stop)`
so `_abort_all_turns` still runs first (LIFO).

### `config.py` — `read_terminal()` (model on `read_mcp_server()`)

```text
[terminal]
enabled = true            # false hides the tab (no advertising of why)
font_size = 13            # clamped 9..24
scrollback = 5000         # clamped 500..50000
close_on_exit = false
max_sessions = 8
claude_profile = true     # offer "Claude Code" when `claude` is found
claude_path = ""          # explicit binary; default PATH lookup
[terminal.env]            # extra env for every session (string values)
[[terminal.profiles]]     # operator-defined extras
name = "Git Bash"
argv = ["C:/Program Files/Git/bin/bash.exe", "--login", "-i"]
cwd = ""
shift_enter_newline = false
ctrl_v_paste = true
[terminal.profiles.env]
```

Clamp numbers, drop profiles with empty `argv`, coerce env values to `str`.
`[terminal]` lives in `config.toml`, so it rides Config Sync / the seed like
the rest. When implemented, add `terminal` to the exempt-table list in
`scripts/check_docs.py` so the reference docs can use a real `toml` fence.

### `pyproject.toml`

New extra `terminal = ["pywinpty>=2.0 ; sys_platform == 'win32'", "websockets>=12"]`;
add it to `all` and `websockets` to `dev`; the mypy override list gains
`winpty`, `winpty.*`, `websockets.*`.

---

## Frontend (`packages/console-ui`, follow the `console-ui` skill)

New deps: `@xterm/xterm`, `@xterm/addon-fit`, `@xterm/addon-webgl`,
`@xterm/addon-unicode11`, `@xterm/addon-search` (wired v1, overlay UI v1.1),
`@xterm/addon-web-links` (v1.1, needs an `open_url` bridge method). CSS via
`import '@xterm/xterm/css/xterm.css'` — bundled, no CDN (per
`docs/security.md`).

| File | Content |
|---|---|
| `src/bridge/index.ts` | types `TerminalProfile`, `TerminalSessionInfo`, `TerminalOpenPayload`, `TerminalCwdOption`, `TerminalConfig`; the seven methods next to `launch_terminal` |
| `src/bridge/mock.ts` | fake shell profiles + a Claude one, `terminal_open` returns `ws_url: ''` (pane shows a mock banner, no connect), in-memory session list |
| `src/views/terminal/xtermTheme.ts` | pure `terminalTheme(isDark, accent): ITheme` — background `surfaces(isDark).sunken`, cursor/selection via `withAlpha(accent, …)`, two fixed ANSI-16 palettes (dark/light) |
| `src/views/terminal/keys.ts` | pure `classifyKey(ev, hasSelection, profile): 'copy'\|'paste'\|'search'\|'newline'\|'pass'`; `SHIFT_ENTER_SEQ = '\x1b\r'` |
| `src/views/terminal/TerminalPane.tsx` | one xterm per session: `fontFamily: MONO_FONT`, `fontSize`/`scrollback` from config, theme, `allowProposedApi`, `cursorBlink`; addons fit + unicode11 + webgl (`onContextLoss` → dispose → DOM renderer); first `fit()` after `document.fonts.ready` (the bundled JetBrains Mono is latin-only; box-drawing/braille fall back to Cascadia Mono — fine); `ResizeObserver` + `active` prop → debounced (50–100 ms) `fit()` → `{"t":"resize"}`, guarded `cols > 0` (never resize while `display:none`); WS binary → `term.write(Uint8Array)`, `term.onData` → `ws.send(bytes)`; `attachCustomKeyEventHandler` via `classifyKey`; right-click = copy-if-selection-else-paste; exit banner with Restart / Close driven by `exit_code` |
| `src/views/terminal/TerminalOpenModal.tsx` | cwd radio from `terminal_cwd_options` + Browse (`bridge.browse_folder`); for the Claude profile also a **"Connect fleet tools (console MCP server)"** switch — shown only when `mcp_server_status().running`, **off by default** — and "Continue last session" |
| `src/views/TerminalView.tsx` | tab strip (antd `Tabs type="editable-card"`, `hideAdd`, "+" `Dropdown` of profiles with availability reasons; Ctrl+Shift+T/W v1.1); holds `sessions`; listens `runspec:terminal_exit`; on mount re-lists `terminal_list()` so a reload re-attaches; empty states: `terminal_available().ok === false` → "Install `runspec-console[terminal]`" hint; `enabled === false` → tab not shown at all |
| `src/App.tsx` | `'terminal'` in `ViewKey`; nav entry `{ key: 'terminal', label: 'Terminal', icon: <CodeOutlined /> }` after Specs, hidden when `get_terminal_config().enabled === false`; **not** added to `AI_ONLY_VIEWS`/`AI_OFF_HIDDEN_VIEWS`; `terminalVisited` lazy flag + `React.lazy` like Analytics so the xterm chunk loads on first visit; **kept mounted** via a display-toggled `.rs-view` flex block like Specs, with `active={view === 'terminal'}`; add `view !== 'terminal'` to the unmount-on-switch exclusion list |

Keys (Windows Terminal conventions): Ctrl+Shift+C copy, Ctrl+Shift+V paste,
Ctrl+C copies **only with a selection** (else SIGINT passthrough), Ctrl+V
pastes (profile `ctrl_v_paste = false` to opt out), Ctrl+Shift+F search,
Shift+Enter sends `\x1b\r` (Meta+Enter newline for Claude Code; xterm.js has
no kitty keyboard protocol) when the profile's `shift_enter_newline` is true
— default true for the Claude profile, false for shells.

Always use the `ws_url` the backend returns; never compute the port in the
frontend.

---

## Docs & housekeeping (when built)

- `docs/console-terminal.md` (new; `mkdocs.yml` nav after "Console as an MCP
  server"): what it is, install extra, profiles, keys table, Claude Code
  launcher (optional MCP attach, cross-link `console-mcp-server.md`),
  `[terminal]` reference, the security posture paragraph above,
  troubleshooting (`claude` not found / `claude.cmd`, glyph fallback, WebGL
  fallback).
- `docs/console-reference.md`: `[terminal]` rows. `docs/security.md`: amend
  the "no listening sockets" row and the §4 table (PTY WebSocket binding /
  auth / Origin / who can write). `docs/console-ai-off.md`: list Terminal
  among the surfaces that remain.
- `packages/python/runspec-console/CHANGELOG.md` `### Added` + version bump;
  `CLAUDE.md` runspec-console row gains an "embedded Terminal tab
  (`terminal` extra; optional Claude Code launcher)" clause. Stop at the
  version bump; never push a tag.

---

## Tests

Python (`tests/test_terminal.py`; `pytest.importorskip("websockets")` for
the WS block):

- `FakePty` (queue-backed; records writes/resizes; `finish(code)`) injected
  via `TerminalManager(spawn=…)`: open/list/close, ring-buffer cap + replay
  on attach, `on_exit` once, `max_sessions` raises, `stop_all`.
- Profiles: `builtin_profiles` with patched `which`/platform (Windows order,
  POSIX `$SHELL`); `claude_profile` absent when not found or disabled;
  `.cmd` wrapping; `claude_argv` (`--mcp-config` only with a path, never
  `--strict-mcp-config`, continue/resume exclusive);
  `write_claude_mcp_config` shape + 0600 + removed on close.
- WS server on a real loopback port via
  `websockets.sync.client.connect(..., additional_headers={"Origin": …})`:
  wrong Origin → 4403 and `attach` never called; wrong token → 4401; happy
  path bytes both ways; resize frame reaches `FakePty`; second attach kicks
  the first with 4409.
- `tests/test_config.py`: `read_terminal` defaults, clamping, bad profile
  dropped. `tests/test_bridge_api_surface.py`: add the seven names.
  Bridge-level (stub pattern from `test_dispatch_queue.py`): a Claude open
  with `mcp: true` and a running server writes the config file and passes
  `--mcp-config`; with `mcp: false` it does not.

Vitest: `keys.test.ts`, `xtermTheme.test.ts` (pure); `TerminalView.test.tsx`
with `vi.mock('@xterm/xterm')` (+ addons) and a stubbed `globalThis.WebSocket`,
bridge via the `vi.hoisted` + `Proxy` pattern (`src/views/RotaView.test.tsx`):
unavailable → install hint; dropdown lists/disables profiles; choosing a
shell calls `terminal_open` and adds a tab; `runspec:terminal_exit` shows the
banner; closing a tab calls `terminal_close`.

---

## Verification

1. `cd packages/console-ui && npm run typecheck && npm test && npm run build`
   (confirm xterm lands in its own lazy chunk).
2. `cd packages/python/runspec-console && pip install -e ".[dev,terminal]" && pytest && ruff check . && mypy runspec_console/`.
3. Manual, Windows: `runspec-console --dev`; Terminal → "+" → PowerShell;
   run `dir`, resize the window (no garbling, prompt reflows); open a second
   tab with cmd; Ctrl+C / Ctrl+V / Shift+Enter behave per the keys table;
   close a tab → process tree gone (`tasklist`). With `claude` installed:
   "+" → Claude Code → pick a repo checkout as cwd → the TUI renders and
   Shift+Enter inserts a newline; with `[mcp_server] enabled = true` and the
   switch on, `/mcp` inside Claude Code lists `runspec` and a confirm-level
   runnable parks in the console's confirm card. Remove `claude` (or set
   `claude_profile = false`) → the profile disappears, the tab remains. Set
   `[console] ai = false` → tab still present. Set `[terminal] enabled = false`
   → tab gone, no message naming any switch.
4. Manual, Linux (GTK): same shell flow with `$SHELL`; kill a tab running
   `sleep 1000` → process gone (`pgrep`).
5. Security check: from a browser tab,
   `new WebSocket('ws://127.0.0.1:<port>/pty/<sid>?t=<token>')` with the real
   token is refused (4403, wrong Origin); a wrong token from the console
   origin is refused (4401).

## Phasing

- **v1**: everything above except items marked v1.1.
- **v1.1**: search overlay UI; `open_url` bridge method + web-links addon;
  Settings → Terminal tab (`save_terminal_config`); Windows Job Object
  teardown (pywin32 `win32job`); proxy/CA auto-env (`[ssh] proxy` →
  `HTTPS_PROXY`, `[web] ca_bundle` → `NODE_EXTRA_CA_CERTS`) behind a toggle;
  `--resume` picker from `~/.claude` sessions; "Open terminal here" buttons
  (Repos rows, Transfer dialog); ligatures addon; Ctrl+Shift+T/W.

## Risks

- ConPTY repaints the whole screen on resize — debounce and never resize at
  zero size.
- `pywinpty` wheels can lag a new CPython major — the `available()` reason
  text points at the extra; keep the tab honest, not hidden.
- `claude.cmd` under `cmd /c` may show "Terminate batch job (Y/N)?" on
  Ctrl+C — prefer a resolved `claude.exe` and document it.
- WebGL context loss in WebView2 — DOM renderer fallback.
- `websockets.sync` is thread-per-connection, bounded by `max_sessions`.
