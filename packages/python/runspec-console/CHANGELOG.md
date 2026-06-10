# Changelog — runspec-console

All notable changes to this package are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers follow [Semantic Versioning](https://semver.org/).

---


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