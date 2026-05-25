# Plan: replace runspec-chat with an ops console

## Context

`runspec-chat` (0.4.7) is a Chainlit web app that wraps `runspec serve` (MCP
stdio) for local + SSH-jumped runnables, with an Anthropic LLM driving
natural-language tool calls. Chainlit gives us a chat interface, history DB,
SQLite-backed sessions, settings, slash commands. It does **not** give us
what the actual audience (technical support, developers, DevOps) needs from
runspec: lifecycle visibility for running and scheduled runnables, live
logs, application monitoring, audit trails.

This plan replaces Chainlit with a custom localhost web UI that is an
**ops console first** (runnables, schedules, live logs, monitoring) with a
**chat panel** that drives runnable invocation via natural language. The
chat surface stays — the LLM is the convenient "natural-language `/tool`"
that the current app already does — but the app stops *being* a chat app.

### What runspec already provides (verified via exploration)

- `runspec serve` — MCP stdio only. No HTTP, no SSE, no auth. UI must
  wrap it (one server per host, including local).
- `runspec local --format json` — machine-readable runnable inventory,
  includes args, autonomy, source.
- Per-runnable rotating JSON logs at `{venv}/logs/{runnable}.log` with
  one record per log call + a `run_summary` event per invocation
  (`runnable`, `duration_ms`, `exit_code`, event counts, `user`,
  `autonomy`, exception).
- `_meta.runspec` on every `tools/call` response (`tool`, `duration_ms`,
  `exit_code`) — already populated.
- No native scheduling, no execution-history index, no auth between UI
  and `runspec serve`.

### What does not exist and the UI must own

- Scheduling (cron/interval/triggers)
- Execution history index across runnables
- Live log tail surface (filesystem-only today)
- Monitoring/heartbeat for long-running runnables
- HTTP wrapper around `runspec serve` (the UI itself is that wrapper)

## Operational framing (from user clarification)

This app is a **human-operated** ops console used during a work shift,
**not** an always-on AI agent. The human launches it at start of shift
and closes it at end of shift. The chat panel and live-tail panels are
for active human use.

Scheduling has two distinct flavors:
1. **During-shift polling.** Frequent short-interval jobs that poll for
   work: new emails, instant-message pings, git PR/CI activity, ticket
   queues, daily-ops tasks. Mostly want to fire while the human is at
   the desk.
2. **After-hours admin cleanup.** Runnables that fire overnight (log
   rotation cleanup, cache purges, scheduled reports). Must fire whether
   the console UI is open or not.

Both flavors are served by registering them as Windows Scheduled Tasks
calling `runspec` directly — the UI is just the editor. After-hours tasks
log to the runnable's normal JSON audit log, and the UI replays that log
the next time the human opens the console.

---

## Decisions (filled in as the interview lands them)

### 1. Core identity — **DECIDED**
Ops console with chat as one panel. The home screen is a runnables/jobs/logs
dashboard. The chat panel exists to orchestrate runnables in natural
language, and is not the default view.

### 2. Windows launch — **DECIDED**
Native Windows Python (or Node) launched from PowerShell. Browser opens
`http://127.0.0.1:PORT`. No WSL, no Git Bash dependency.

**SSH gap-fillers as runnables.** Today's app embeds `_builtin_setup_keys`
that shells out to `sshpass` / `ssh-copy-id` (unix) and falls back to a
"paste this command yourself" message on Windows. We replace that with a
proper Windows-capable runnable (`setup-keys`) that uses `paramiko` (or
`ssh.exe` + a remote append-to-authorized_keys command) to perform the
key copy itself. Other missing posix utilities follow the same pattern —
ship them as runnables, not as shell-outs.

### 3. Tech stack — **DECIDED (SPA confirmed)**

Python backend + pre-built static SPA frontend, shipped as one wheel.

**Why SPA over native (PySide6/Electron):** the target audience runs on
**locked-down work machines** where installing additional runtimes
(Qt, Electron) may be blocked. A browser is universally available
already. Localhost HTTP + browser is the most install-tolerant path.

**Scope simplifications confirmed in this round:**
- **Single user only.** No multi-user functionality, no sessions, no
  permissions, no concurrent edits. The console runs on one machine for
  one operator at a time. This kills a huge amount of accidental
  complexity in the backend.
- **No external API consumers.** The backend exists only to serve the
  SPA on the same machine. No public surface, no API versioning, no
  OpenAPI docs needed.
- **We own both ends.** Backend and frontend ship together as one wheel
  — they can be tightly coupled. No need for stable contracts, no
  third-party clients to break.

- **Runtime:** end user only needs Python (already permitted via pip).
- **Backend:** Python — reuses `runspec` library, MCP client, `pywin32`,
  existing `runspec-chat` adapter code. Specific framework choice still
  open — see §7b.
- **Frontend:** SPA — framework choice still open — see §7b.
- **Dev-time only:** Node for frontend build; not required for users.

### 4. Naming — **DECIDED**
**`runspec-console`**. PyPI package `runspec-console`, CLI binary
`runspec-console`, package dir `runspec_console/`. The chat panel inside
the app is just "Chat" — the product is a console.

### 5. Distribution & install model — **DECIDED**

- **`pip install runspec-console`** — a separate PyPI package that declares
  `runspec>=0.15.1` as a dependency.
- Ships a `runspec-console` CLI entry point.  Running it starts the Python
  backend, prints `http://127.0.0.1:<port>` to stdout, and opens the
  browser automatically (via `webbrowser.open`).
- Frontend static assets (pre-built SPA) are bundled in the wheel under
  `runspec_console/static/` and served by the backend.
- Dev-time build: `npm run build` inside `packages/console-ui/` outputs to
  `packages/python-console/runspec_console/static/`.  End users never touch
  Node.
- Version numbering mirrors `runspec` major/minor; patch is independent.

### 6. Backwards compatibility with current `runspec-chat` — **DECIDED**
Clean replacement.

- Publish `runspec-console` 0.1.0 as a new PyPI package.
- Final `runspec-chat` release (0.5.0) prints a deprecation notice on
  launch pointing at `runspec-console` and otherwise keeps working.
- `runspec-console` first-launch migration: copy `jump_hosts.toml`,
  `auth.secret` from `~/.config/runspec-chat/` to
  `~/.config/runspec-console/`; reuse the existing
  `~/.ssh/runspec-chat_*` key (rename target path to
  `~/.ssh/runspec-console_*` on next `setup-keys` run, not retroactively).
- History import from the old SQLite is *only* done if §8 (persistence)
  decides to keep chat history.
- **After cutover:** yank `runspec-chat` from PyPI. Note from user:
  "runspec-chat shipped totally broken a few times and never got to a
  working version." Yanking is destructive, so it stays gated behind
  explicit confirmation at release-time, not automated.

### 7. Backend architecture — **DECIDED (transport shape)**
- **Local invocation:** import the `runspec` library directly for
  discovery; use `subprocess.Popen` to launch `runspec <runnable>` for
  execution so stdout/stderr stream live into a websocket.
- **SSH invocation:** persistent stdio MCP session over
  `ssh user@host runspec serve` (same as today). Low per-call latency,
  uniform tool-call surface for the LLM panel.
- **Live output for SSH:** out of v1 scope (deferred). Phase 1 shows final
  `tools/call` response + `_meta.runspec` summary for SSH runs. If live
  streaming over SSH becomes important, add a second SSH channel that
  `tail -F`s the remote `{venv}/logs/{runnable}.log` JSONL.
- **LLM tool dispatch:** routes through the same two paths under the hood.
- **Host model:** each connected host (local + each SSH host) is a
  self-contained Python venv with `runspec` + runnables installed.
  Local is just a special case where the venv is `sys.prefix`.

### 7a. Host provisioning vision (from user) — **RESOLVED IN §14**

The console treats each SSH jump host as a **data-source layer**: its own
venv, its own runnables, its own audit logs. The console:

- Can **bootstrap** a new host: SSH in, create a venv, install `runspec`
  + runnables. (v1: stock `python -m venv` + `pip`. v2: UV.)
- Can **attach** to an existing venv on a host.
- Updates and audit happen inside each host's venv — the console is the
  remote control, the host owns the data.
- Local machine fits the same shape: "host = localhost" with the console's
  venv as the host venv.

Private PyPI / npm credentials and UV swap-in are deferred to v2 — see §14.

### 14. Host provisioning scope (v1 vs later) — **DECIDED**

**v1: attach + bootstrap with stock `python -m venv` + `pip install`.**
- SSH key setup (`setup-keys`) is in v1, modernized to a real Windows-
  capable runnable (paramiko or ssh.exe + a `>>authorized_keys` remote
  command). No reliance on `sshpass` / `ssh-copy-id`.
- New v1 runnable: `setup-host` (or similar). Given an SSH target +
  package list, it:
  1. Connects and verifies Python is present (`python3 --version`).
     If not, fails fast with a clear "install Python on this host or
     wait for v2 (UV bootstrap)" message.
  2. Creates a venv at a conventional location (e.g.
     `~/.runspec-console/venv`).
  3. `pip install runspec <packages...>` (public PyPI only in v1).
  4. Writes the venv path into `jump_hosts.toml` (`runspec_path` field
     already exists).
- v1 attach mode: connect to a pre-existing venv (today's behavior),
  unchanged.

**v2 deferred:**
- Swap stock venv for **UV** (faster install, smaller footprint, can
  install Python itself if missing).
- **Private PyPI** support (per-host index URL + credentials, stored
  encrypted in the console config dir).
- **npm** runnable installation (parallel pipeline for Node-based runnables).
- Auto-update / audit of installed runnable versions within each host venv.

### 8. Persistence model — **DECIDED**

The console is a **stateless aggregating proxy.** Persistence is
deliberately minimal because the data lives on the hosts and in the
external sources the console queries.

#### On-disk state owned by the console

- `~/.config/runspec-console/jump-hosts.toml` — list of hosts to
  connect to (existing runspec format, shared with `runspec jump`).
  Hand-editable; also editable through the Hosts view.
- Browser `localStorage` — UI preferences only (theme, collapsed
  section state, last-active view). No business data.

That's it. No SQLite. No alert DB. No mirror of runnable catalogs.

#### Session state (in-memory, dies on restart)

- Per-host runnable catalog (fetched on connect via
  `runspec local --format anthropic`).
- Aggregated alert list from the last "Get Today" fan-out.
- Polling-task schedule started by "Get Today" (cleared on restart).
- Per-(host, runnable, since) result cache for fast re-renders.

#### Source-of-truth rules

| Data | Lives where | How the console gets it |
|---|---|---|
| Hosts list | `jump-hosts.toml` on user's machine | Read on launch |
| Runnable catalog | Each host's venv | `runspec local` over SSH |
| Probe definitions & state | Each host's venv (shared by team) | Probe runnable invocation |
| Run history | Each runnable's `{venv}/logs/{r}.log` JSON audit | Tail/stream on demand |
| Alerts (email, IM, Datadog, …) | The external source | Ingestion runnable on each Get Today / poll |
| Alert ack | Pushed back to the source if it supports it; otherwise reappears next pull | API call to source |
| UI prefs | Browser `localStorage` | Local |

#### Get Today as the entry point

1. User launches console → blank dashboard with a single "Get Today"
   button (plus host-connection status indicators).
2. User clicks → console fans out to all hosts and all configured
   alert-source runnables for a `since = start_of_yesterday` window.
3. Results merge into the Today view sections (§9). Failures per host
   are shown but don't block partial results.
4. From this moment, a per-source polling schedule is established
   in-memory: each source declares its own poll interval (default
   `60s` for fast probes, `5m` for email/IM, source-specific for
   monitoring tools).
5. Console restart → polls die. User re-clicks Get Today.

#### Implications

- **Team-shared by accident, not by design.** Two teammates running
  separate consoles against the same host see the same runnable logs
  and probe state because they're reading the same files; there's no
  "console sync" to maintain.
- **Auditability is preserved without console help.** The runnables'
  own JSON audit logs are unaffected by anything the console does.
- **Cold-start cost is the Get Today fan-out latency.** For N hosts
  and M sources, ~max(N×ssh_setup, M×api_call) seconds. Show progress.
- **No "missed events while console was closed" guarantee.** That's
  the alert sources' job (PagerDuty, email, etc.). The console is a
  shift-start dashboard, not a 24/7 watcher.

### 9. Information architecture (views) — **DECIDED (v1 scope)**

**Primary surface (home): Command Interface.** A Tiptap input bar at the
bottom + a message/output area above it. This is both the runnable launcher
and the LLM chat entry point — one surface, not two. Home route lands here.

**Today view deferred to v2.** The shift-inbox / alert-aggregation surface
is out of v1 scope. v1 focuses on making the command interface and
runnable management excellent first.

Persistent left sidebar nav (v1): **Console** · **Runnables** · **Hosts** ·
**History** · **Schedules**. Home route is **Console** (the command interface).

#### Core architectural constraint (from user)

> A runspec runnable is designed to not be dependent on a front end or
> agent invocation and must always be human-runnable on the CLI of the
> jump host.

The console therefore never introduces console-only protocols. Everything
it calls, a human can call from a shell on the host. Everything it reads,
a human can read from the same file. This is the test for any feature.

#### The "Today" view (shift inbox)

A shift-oriented aggregator. Five stacked sections, each collapsible:

1. **Overnight outcomes.** Every scheduled runnable that fired since the
   last time the user opened the console: pass/fail, duration, link to
   full log. Sourced directly from each runnable's JSON audit log on
   the host — no aggregator runnable required.
2. **Active alerts.** Pulled by invoking **one aggregator runnable per
   host** (e.g. `get-alerts`). That runnable — owned by the team, lives
   on the host, runnable from the CLI — fans out to per-service
   runnables (email poller, IM mentions, Datadog wrapper, internal
   probes) and emits the combined alert stream as JSON-lines on stdout
   in the shape documented in §11. The console reads stdout, merges
   across hosts, displays. In v1, teams write their own aggregator
   (and `runspec-console` ships a documented example stub). In a future
   release `runspec-flow` (§15) makes composing these aggregators
   first-class — interval, dedup, fan-out, error rollup.
3. **Upcoming.** Next N scheduled runs in the coming few hours
   (sourced from each host's task scheduler — `schtasks /Query` on
   Windows hosts).
4. **In-flight.** Anything still running from the current session.
5. **LLM digest (optional, button-triggered).** "Brief me" sends the
   contents of §1–§4 to the chat panel as context and asks the LLM
   for a one-paragraph plain-English summary of the shift state. Not
   automatic; opt-in to spend API tokens.

#### Watch state vs. alerts

Probe state is **not** a separate Today section; it is just another
source the team's `get-alerts` aggregator fans out to. The standalone
**Watch view** still exists for managing probe definitions and
intervals, but the day-to-day red-state surface goes through the
unified alerts inbox.

#### Other views (sketch)

- **Runnables** — searchable catalog of every runnable across every
  host. Click → arg form → run (subprocess local or MCP SSH) with live
  stdout panel. Show args from `runspec local --format anthropic` schema.
- **Schedules** — calendar/grid of Windows Scheduled Tasks created by
  this console. CRUD via the existing `setup-host`-style runnable
  pattern — the schedule editor is itself just a typed form over
  `schtasks` arguments.
- **History** — chronological list of runs across all runnables, filterable
  by host / runnable / status. Sourced from the JSON audit logs.
- **Watch** — probe runnables and their current state. **Edits to a
  probe's interval, threshold, etc. are edits to the runnable's
  arguments** (the runnable already declares them). Per-session override
  is a UI form pre-filled with the team default; restart reverts to
  team setting; "save to team default" pushes back to the runnable's
  config on the host. No console-side mute table.
- **Hosts** — list of connected hosts (local + each SSH). Per-host:
  status, runnable count, recent failures, "bootstrap" action (v1: stock
  venv + pip; v2: UV). Edit jump-host config inline.
- **Chat** — natural-language tool dispatch + LLM digest target. See §13.

### 10. Scheduling model — **DECIDED (mechanism)**
Windows Task Scheduler is the scheduling backend. The UI is the editor
and the read-side viewer.

- Creating a schedule = `schtasks /Create ...` (or `pywin32`'s
  `win32com.client` to talk to the Task Scheduler COM API directly) that
  runs `runspec <runnable> <args>` at the specified time/interval. User
  scope only (no admin needed).
- Listing schedules = `schtasks /Query /FO CSV /V` filtered to
  tasks the console created (a `\runspec-console\` task folder is its
  namespace).
- Removing a schedule = `schtasks /Delete`.
- The runnable's existing `[config.logging]` JSON file at
  `{venv}/logs/{runnable}.log` is the source of truth for run results;
  the UI tails / indexes it.
- Linux/Mac story is deferred — first cut is Windows-only. (Library
  pattern can later swap in `cron` or `launchd` behind the same interface.)

### 11. Alert ingestion contract & poll cadence — **DECIDED**

#### Stdout JSON-lines, common shape

An aggregator runnable invoked by the console (e.g. `get-alerts`) emits
one JSON object per line on stdout. The shape:

```json
{
  "source": "datadog" | "email" | "slack" | "probe:cpu" | …,
  "id":     "stable per-incident key for dedup",
  "severity": "critical" | "warning" | "info",
  "title":  "short human title",
  "body":   "optional longer text",
  "url":    "optional hyperlink to source UI for manual action",
  "ts":     "ISO-8601 timestamp",
  "host":   "optional — which host this concerns",
  "runnable": "optional — which runnable this concerns"
}
```

The console dedupes by `(source, id)` across hosts, sorts by `ts`
descending, and renders in the Today view's alerts section.

A human running the same aggregator from the CLI sees the same JSON-lines
output. No console-only mechanics.

#### Acknowledgment — v1 is hyperlink-only

The `url` field is the only ack path in v1: clicking the alert opens the
source's own UI in a new tab and the human performs the ack there. The
alert re-appears on the next poll until the source itself clears it,
which is honest behavior. A richer ack model (per-alert `ack_action`
runnable, source-side mute, etc.) is **deferred to v2** once we have
real-world source scenarios to design against.

#### Poll cadence — interval is a runnable argument

The aggregator runnable declares its polling interval as a normal
runspec arg (e.g. `interval` with `default = "5m"`). The console reads
it from the runnable's standard arg schema and:

- Initial Get Today: invokes once with `--since=start_of_yesterday`.
- After Get Today: schedules repeat invocations using the interval arg's
  current value, with `--since=last_poll_ts`.
- The Today view (and the Watch view for probes) shows the current
  interval as an editable input pre-filled from the runnable's default.
- Edits apply for the session. Restart reverts to the team default.
- A "Save to team default" action edits the runnable's `runspec.toml`
  on the host (over SSH) so the team's next sessions inherit the change.

Live logs & monitoring details (websocket tail, panel UX) — see §11a.

### 11a. Live logs & monitoring — **DECIDED**

#### The runspec execution model

Runnables are always short-lived and exit. The scheduler (Windows Task
Scheduler in v1) is the long-running component — not the runnable itself.
`runspec-flow` pipelines may run longer but contain their own internal
monitoring. There is no category of "long-running runnable" that the
console needs to heartbeat-check.

Consequently the only monitoring questions that arise are audit-log
inspection questions:
1. Did the scheduled run start? (`run_start` present in audit log)
2. Did it succeed? (`run_summary` present with `exit_code: 0`)
3. Is a run overdue — scheduled window passed, no `run_start`?

All three are answerable by reading `{venv}/logs/{runnable}.log` on the
host. A team's `get-alerts` aggregator can include a "missed-runs"
probe runnable that checks these conditions and emits alerts in the
common shape. No console-side heartbeat registry needed.

#### Live-tail (active runs)

When the user manually triggers a runnable from the Runnables view:
- **Local:** subprocess stdout is piped directly; the output panel
  streams via a WebSocket to the frontend.
- **SSH/MCP:** stdout lines arrive as MCP tool-call events from
  `runspec serve` on the host; same WebSocket to the frontend. When the
  run ends, the audit log on the host already has the `run_summary`
  record — no extra write needed from the console.

#### History / audit log browsing

The History view reads JSON audit logs from all connected hosts.
Tail (polling or inotify-style) is not needed for v1 — the view loads
on navigation and refreshes on demand. Scheduled runs appear in the
Overnight outcomes panel of Today view on the next "Get Today".

### 12. Security model — **DECIDED**

- **Backend binds to `127.0.0.1` only.** No external connections, ever.
  If a team needs remote access that's a separate future concern (VPN + SSH
  tunnel, not a console change).
- **CORS locked to the origin.** The backend sets
  `Access-Control-Allow-Origin: http://127.0.0.1:<port>` (exact match,
  same ephemeral port as the backend). Cross-origin requests from any
  other origin are rejected.
- **No CSRF token needed.** CORS enforcement + localhost binding is
  sufficient. All mutating endpoints require `Content-Type: application/json`
  bodies, which triggers CORS preflight for any cross-origin caller and
  gives the browser a second line of defence. A token would be redundant.
- **SSH keys:** carried over from `runspec-chat` pattern —
  `~/.ssh/runspec-console_*` keypair, managed by `setup-keys` runnable.
- **Runnables that call external APIs handle their own auth.** The console
  never stores or proxies API credentials; those live in the runnable's
  `runspec.toml` or environment on the host.

### 13. LLM / chat panel scope — **DECIDED**

**Tool-dispatch + digest target.** The chat panel does two things:

1. **Natural-language → runnable invocations.** User types "run the backup
   on host-2" or "show me yesterday's failures on host-1". The backend
   sends the message to Claude (Anthropic API, same `adapters/anthropic_direct.py`
   pattern as `runspec-chat`) with the combined runnable tool schemas from
   all connected hosts (`runspec local --format anthropic`) as the tool
   list. Claude selects the right tool + args; the backend executes via
   the same local/SSH path as the Runnables view; the output streams back
   into the chat panel.

2. **"Brief me" digest target.** When the user clicks "Brief me" on the
   Today view, the Today view's §1–§4 data (overnight outcomes, active
   alerts, upcoming runs, in-flight) is serialized as structured text and
   sent to the chat panel as a pre-populated user message. Claude responds
   with a plain-English shift summary. The user can follow up in the same
   thread.

**Scope constraints:**
- No persistent conversation history across sessions (in-memory only;
  cleared on restart — consistent with stateless model in §8).
- No autonomous agent loop. Each user message = one LLM call (plus
  any tool-use turns within that call). No background LLM polling.
- API key is user-supplied, stored in `~/.config/runspec-console/config.toml`,
  same pattern as current `runspec-chat` auth. Exposed as an env var
  `ANTHROPIC_API_KEY` as the fallback (stdlib `os.environ` lookup).
- Default model: `claude-sonnet-4-6` (fast, inexpensive for frequent
  dispatch calls). User can override in config.

### 7b. Framework & scope mapping — **DECIDED**

#### Backend shell
**pywebview** — no HTTP server, no port. The SPA runs inside a native
WebView2 window (built into Windows 10/11 via Edge — no additional install
required). Python functions are exposed to JavaScript via the pywebview JS
bridge (`window.pywebview.api.*`). No CORS, no WebSocket protocol, no port
management.

Windows-only is explicit and deliberate — not a limitation to paper over.
The target environment is locked-down corporate Windows desktops. WebView2
is already present as a Windows system component; it is not an additional
runtime install.

#### Frontend framework
**React + Ant Design.** Ant Design was built for ops and admin tooling —
tables, forms, sidebars, status indicators, modals all pre-built to
production quality. Bundle size is irrelevant (SPA served from disk via
pywebview, not over a network). Build tooling: **Vite** (dev-only; end
users never touch Node).

#### Slash command input
**Tiptap** with the official first-party Slash Dropdown Menu component.
Typing `/` opens a filtered dropdown of available runnables across all
connected hosts. Characters typed after `/` filter the list live. Up-arrow
command history added via Tiptap's keyboard shortcut API. The input bar is
also the LLM chat entry point — same surface, same slash trigger.

#### Global command palette
**cmdk** — `Ctrl+K` anywhere in the console opens a searchable runnable
picker. Complements the slash input; same runnable list, different trigger.
Good for "I know roughly what I want but don't remember the exact name."

#### Message / output display
Chainlit-inspired step display (tool call icons, step indicators, stdout
output blocks) reproduced using Ant Design `Steps`, `Tag`, and `Card`
components. Chainlit's open-source component patterns are the UX reference;
Chainlit itself is not a dependency.

#### Dependency risk principle
Libraries ranked by abandonment / breakage risk:
- **Low:** Ant Design (Alibaba-backed, decade of history), cmdk
  (shadcn ecosystem, tiny stable API), Tiptap core (company-backed,
  migration guides on breaking changes).
- **Higher:** any community wrapper that sits between two other libraries
  (e.g. a third-party Tiptap slash-command package that could fall out of
  sync if Tiptap's suggestion API shifts).

Mitigation rules:
1. **Prefer official first-party components.** Tiptap ships its own Slash
   Dropdown Menu — use that over community wrappers unless it is missing
   something concrete.
2. **One adapter component per library.** `CommandInput.tsx` owns Tiptap,
   `CommandPalette.tsx` owns cmdk, `ConsoleLayout.tsx` owns Ant Design
   layout. No library API leaks past its own adapter file. If a library
   breaks or is abandoned, one file changes — not a dozen.

#### pywebview bridge API surface

Two interaction patterns replace the HTTP endpoint inventory:

**Pull** — JS calls Python, Python returns data:
| Call | What it does |
|---|---|
| `get_hosts()` | List of configured hosts + live connection state |
| `get_runnables(host)` | Runnable catalog via `runspec local --format json` |
| `get_history(host, runnable)` | Recent records from `{venv}/logs/{r}.log` |
| `get_schedules()` | Task Scheduler entries under `\runspec-console\` |
| `get_config()` / `save_config(data)` | Read / write `jump-hosts.toml` |
| `create_schedule(data)` / `delete_schedule(id)` | `schtasks` CRUD |
| `invoke_runnable(host, runnable, args)` | Start a run; returns invocation id |
| `send_chat(message, invocation_id)` | Send message to LLM with tool schemas |

**Push** — Python calls `window.evaluate_js()` to fire JS CustomEvents;
React listens with `addEventListener`:
| Event | Payload |
|---|---|
| `runspec:output` | `{ id, line, stream }` — one line of stdout/stderr |
| `runspec:run_end` | `{ id, exit_code, duration_ms }` — run complete |
| `runspec:token` | `{ id, token }` — one LLM streaming token |
| `runspec:host_status` | `{ host, connected, runnable_count }` |

The SPA starts a run via `invoke_runnable()` (pull); all subsequent output
arrives as `runspec:output` push events tagged with the returned id.

### 15. Forward-looking: `runspec-flow` orchestration layer — **NOTED**

`runspec-flow` (working name, formerly "flowspec") is a planned future
package that makes composing runnables into pipelines first-class:
fan-out across child runnables, dedup, schedule, error rollup, common
output shape. The Today view's aggregator pattern (one `get-alerts`
runnable that fans out to email + IM + monitoring sub-runnables) is the
canonical motivating example.

**v1 of `runspec-console` ships without `runspec-flow`** — teams write
their aggregator by hand, or use a documented stub shipped in
`runspec-console`'s examples. The contract in §11 is **designed to
forward-compatible** with `runspec-flow`: when `runspec-flow` lands, a
flow that emits the same JSON-lines shape Just Works as a console
alert source with no console changes.

---

## Verification (pre-implementation: smoke checklist)

To be filled in concretely once §7b lands. Skeleton:

- **Bind safety:** launch on Windows; `netstat -an | findstr :<port>`
  shows `127.0.0.1:<port>` only. `curl http://<machine-ip>:<port>/`
  from another machine on LAN is refused.
- **CORS enforcement:** a test page served from `http://localhost:8080`
  (different port) attempts `fetch("http://127.0.0.1:<console_port>/api/hosts")`
  with `Content-Type: application/json` — browser blocks via preflight.
- **Inventory:** `runspec local --format json` runnables appear in the
  Runnables view within ~2s of console launch (no restart).
- **Local execution:** invoking a runnable from the UI streams stdout
  live, shows `_meta.runspec` exit/duration on completion, and the
  invocation appears in History.
- **SSH execution:** existing `jump-hosts.toml` auto-connects; remote
  runnables appear under the right host; final result + `_meta.runspec`
  appears (v1: no live stream over SSH).
- **Get Today fan-out:** with a stub `get-alerts` runnable emitting two
  JSON-lines alerts, Today view shows both in the Active Alerts section
  within the configured interval, deduped by `(source, id)`.
- **Schedule:** Schedules view creates a `schtasks` entry that fires
  the runnable on schedule; the run is recorded in the audit log and
  visible on next Get Today as overnight outcome.
- **Migration:** existing `~/.config/runspec-chat/jump_hosts.toml` is
  copied to `~/.config/runspec-console/jump-hosts.toml` on first launch.

## Status: design complete — ready for implementation

All architectural questions resolved. v1 scope is locked.

**All sections decided:**
§1 identity · §2 Windows launch · §3 tech stack (pywebview + React) ·
§4 naming · §5 distribution · §6 BC with runspec-chat · §7 transport
architecture · §7a host vision · §7b framework + bridge API surface ·
§8 persistence · §9 information architecture · §10 scheduling ·
§11 alert ingestion (v2) · §11a live logs · §12 security · §13 LLM
scope · §14 host provisioning · §15 runspec-flow note.

**v1 scope (build this):**
- Command interface — Tiptap input bar + output area (home screen)
- Slash command picker (`/`) — Tiptap first-party Slash Dropdown Menu
- Global command palette (`Ctrl+K`) — cmdk
- LLM tool-dispatch — Claude API, tool schemas from `runspec local`,
  constrained to runnable invocation only (not general chat)
- Runnables view — browse catalog across hosts, run from UI
- Hosts view — connect, bootstrap remote venvs (`setup-host`)
- History view — audit log reader across hosts
- Schedules view — Windows Task Scheduler CRUD
- pywebview native window, React + Ant Design, Vite build

**v2 deferred:**
- Today view (shift inbox, alert aggregation, overnight outcomes)
- UV swap-in for host bootstrap
- Private PyPI support
- runspec-flow orchestration layer
