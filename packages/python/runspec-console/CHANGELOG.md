# Changelog — runspec-console

All notable changes to this package are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers follow [Semantic Versioning](https://semver.org/).

---


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