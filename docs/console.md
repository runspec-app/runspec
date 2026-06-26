# The Console kingdom

*Act IV · The Console Kingdom — command the fleet, runbooks and all.*

You arrive as the hero: a desktop throne room where you and your companion run forms, triggers, and full runbooks across the whole fleet.

`runspec-console` is a desktop application for running, scheduling, and
monitoring your runspec runnables from a graphical UI. It's built on
[pywebview](https://pywebview.flowrl.com/) with a Vite/React frontend,
discovers every runnable installed in its environment, and can reach runnables
on remote hosts over SSH.

---

## Install

```bash
pip install runspec-console
runspec-console
```

On **Windows**, installing the console automatically pulls in
[`runspec-windows`](https://pypi.org/project/runspec-windows/) (with its
`[graph]` extra), so its Windows system-administration and Microsoft 365
runnables appear in the UI with no extra step. The dependency is gated to
`sys_platform == "win32"`, so it's skipped on macOS/Linux.

To add more runnables, `pip install` the package that provides them into the
same environment (e.g. `runspec-linux`) — the console picks them up on the next
launch.

### Where runnables come from

The console lists whatever `runspec local` can discover: any installed package
that ships a `runspec.toml` and entry points. There's no registry and no
filesystem scanning — `pip install` (or `pip install -e .`) is how a runnable
becomes visible.

---

## SSH connections & tuning

The console refreshes every host's connectivity and runnable inventory on a
background timer. To avoid hammering bastions/jump hosts with a burst of fresh
SSH handshakes (which trips `sshd MaxStartups` and surfaces as *"Error reading
SSH protocol banner"*), it keeps **one reused, keepalive'd SSH connection per
host** — shared by the connectivity probe, discovery, history, logs, and
invocations. A warm connection serves new commands over fresh channels with no
new handshake; a host going offline is detected on the next cycle and retried
with exponential backoff.

All knobs live in `%APPDATA%\runspec-console\config.toml` and have safe
defaults — you only need them for unusual topologies (very strict bastions,
large fleets, high-latency proxies):

```toml
[ssh]
# Connection reuse (issue #104). Set false for legacy connect-per-call.
pool           = true
keepalive      = 30   # seconds; transport keepalive (0 disables)
max_sessions   = 8    # max concurrent channels per connection (< sshd MaxSessions)
idle_ttl       = 300  # seconds before an unused connection is closed
max_concurrent = 5    # max simultaneous NEW handshakes across the whole fleet
backoff_base   = 5    # seconds; per-host exponential backoff after a failure
backoff_max    = 300  # seconds cap

# Connect-phase timeouts (raise these behind slow proxies / VPNs).
connect_timeout = 10
banner_timeout  = 30
auth_timeout    = 30

[refresh]
interval = 30   # seconds between refresh cycles
jitter   = 3    # +/- seconds of per-host start jitter
workers  = 16   # bounded worker count for the refresh fan-out
                # (default max(16, 2 * [ssh] max_concurrent))
```

!!! note "Where each knob lives"
    `max_concurrent`, `backoff_base`, and `backoff_max` are resolved from the
    `[ssh]` section (`pool_settings`), **not** `[refresh]` — only `interval`,
    `jitter`, and `workers` come from `[refresh]` (`refresh_settings`). The
    refresh loop fans both phases out over a **bounded worker pool** (default
    `max(16, 2 × max_concurrent)`), so its thread count stays constant regardless
    of fleet size; see [SSH host capacity](console-ssh-capacity.md) for tuning by
    fleet size.

!!! tip "Behind a corporate proxy / middlebox"
    If a connection error mentions *"the remote sent non-SSH data on connect"*,
    a proxy or TLS middlebox is intercepting the SSH port. Point the console at
    an HTTP CONNECT proxy with `[ssh] proxy = "http://proxy.corp:8080"`, or set
    `use_ssh_config = true` to honour a `ProxyCommand` from `~/.ssh/config`.

---

## Microsoft 365 runnables (Outlook · Teams · Calendar · OneDrive)

These ship in `runspec-windows` and authenticate as the signed-in user via the
Microsoft Graph **device-code** flow (no client secret). One-time setup:

1. **Register an app** in your Entra (Azure AD) tenant
   (*App registrations → New registration*).
2. **Authentication → "Allow public client flows" → Yes** — device-code login
   needs this.
3. **API permissions → Microsoft Graph → Delegated**: add `User.Read`,
   `Mail.Read`, `Chat.Read`, `Calendars.Read`, `Files.Read.All`, then grant
   consent.
4. Copy the **Application (client) ID** and **Directory (tenant) ID**.
5. Drop them in `%APPDATA%\runspec-windows\graph.toml` (the client id is not a
   secret):

    ```ini
    client_id = "00000000-0000-0000-0000-000000000000"
    tenant    = "yourcompany.onmicrosoft.com"
    ```

6. Run `graph-login` once (it prints a URL + code to authenticate); the token
   caches and refreshes silently thereafter.

!!! tip "Trying it at home"
    For a **personal** Microsoft account, register the app for "personal
    Microsoft accounts" and set `tenant = "consumers"` (or `"common"`) instead
    of a tenant domain.

!!! warning "Admin consent in a managed tenant"
    `Chat.Read` (Teams) is typically **admin-consent-required**, so a tenant
    admin may need to grant consent on the app registration. The
    mail/calendar/files scopes are usually user-consentable.

The full app-registration walkthrough lives in the
[runspec-windows README](https://pypi.org/project/runspec-windows/).

---

## AI assistant (optional)

The console can drive runnables through an LLM. Install the provider extra you
want:

```bash
pip install "runspec-console[anthropic]"   # Claude
pip install "runspec-console[openai]"      # OpenAI
pip install "runspec-console[bedrock]"     # Claude via AWS Bedrock
```

---

## White-labeling

If you wrap the console in your own distribution — a package that ships a custom
LLM adapter (see the
[provider-plugin example](https://github.com/jasonfinestone/runspec/tree/main/packages/python/runspec-console/examples/console-adapter-plugin))
and depends on `runspec-console` — the app brands itself to **your** package when
your provider is active. It's **derived from the installed package's metadata**;
no config file or deployment step is required.

| Surface | Derived from |
|---|---|
| Window / page title | your distribution name, prettified (`mycorp-console` → "Mycorp Console") |
| Documentation link | a `Documentation` (then `Homepage`) URL in your package metadata |
| Update check | your package — so the "update available" badge tracks *your* releases against the venv's configured index |

The branding follows the **active** `[llm] provider`: an entry-point provider
resolves to the distribution advertising it; a dotted-path provider to the
distribution owning that module. A built-in provider (anthropic/openai/bedrock/
langserve) or no provider leaves runspec's own identity in place.

Declare the URLs in your wrapper package's `pyproject.toml`:

```toml
[project.urls]
Documentation = "https://docs.mycorp.com/console"
Homepage = "https://mycorp.com"

# Optional: a branded launcher alongside `runspec-console`, created by your
# package (entry points are static per-distribution). It runs the same console.
[project.scripts]
mycorp-console = "runspec_console.app:main"
```

To override the documentation link explicitly (e.g. an internal mirror),
regardless of package metadata, set it in `config.toml`:

```toml
[console]
docs_url = "https://docs.internal.corp/console"
```

---

## Event triggers (Windows)

Triggers let the console **react to things that happen on the machine** and run
runspec runnables — a new email, a meeting reminder, a Teams/Slack notification.
The **Triggers** tab shows your configured triggers and a **run inbox** where each
matched event flows through its lifecycle status: **Triggered → Triage → Awaiting
human → Running → Done / Failed**.

A trigger binds an **event source** + **event type** (optionally narrowed by a
regex `match`) to an **action**. The action is either a deterministic runnable
call (`mode = "rule"`) or a headless agent turn (`mode = "agent"`) that follows
the trigger's `rules` with the full runnable toolset. Either way, every runnable a
trigger drives goes through the **same autonomy gate as chat** — confirm-by-default,
so a run card parks in *Awaiting human* with inline Approve / Deny.

> Triggers are a **Windows** feature today and only fire while the console is
> running (they never replay a backlog — see *forward-only* below). An LLM
> provider must be configured (above) for `mode = "agent"` triggers.

### Event sources

| Source (`source =`) | Events (`event =`) | Notes |
|---|---|---|
| `outlook`       | `new_mail`, `reminder` | Uses the **signed-in desktop Outlook** client over COM — no extra dependency, and it rides your existing SSO session. |
| `notifications` | `toast`                | Every system toast (Teams, Slack, anything) via one listener. Needs the `notifications` extra **and** a one-time Windows consent grant. |

```bash
pip install "runspec-console[notifications]"   # enables the toast source
```

The Triggers tab shows each source's availability and the reason any is off
(not on Windows, `winrt` not installed, consent not granted, …).

### Configuring triggers

You create and edit triggers directly on the **Triggers** tab (the editor moved
here — Settings no longer has a Triggers sub-tab; the Mailbox sub-tab stays).
Each trigger row also has a **"Trigger now"** button that fires it with a
*synthetic* event — a pasted message plus `key=value` fields — so you can test a
trigger without waiting for a real event to arrive.

Triggers live in `runspec_triggers.toml` in the app config directory
(`%APPDATA%\runspec-console\`). Give each trigger its own **named table**,
`[triggers.<name>]`, with its sub-tables namespaced under the same name
(`[triggers.<name>.match]`, `[triggers.<name>.map]`, `[triggers.<name>.reply]`) —
the same shape as `runspec.toml`'s named runnable sections. The table name *is*
the trigger's name, so there's no separate `name` key:

```toml
[triggers.vip-outage-triage]
source = "outlook"
event  = "new_mail"
mode   = "agent"               # "agent" (LLM-in-the-loop) | "rule" (direct runnable)
autonomy = "confirm"           # gate level (default: confirm)
post_to_chat = false           # also mirror this run into the Console tab

# agent mode: the rules the model follows for this trigger. (A scalar key, so it
# must appear BEFORE the [triggers.<name>.*] sub-tables below.)
rules = """
If this reports an outage, identify the affected host, run the diagnostics
runnable against it, and draft a short reply summarising findings. Never
restart a service or send mail without confirmation.
"""

# Cheap regex pre-filter on the event payload — all patterns must match. Keeps
# the LLM out of irrelevant events (and tames a chatty `reminder` source).
[triggers.vip-outage-triage.match]
from    = ".*@bigcustomer\\.com"
subject = "(?i)outage|down|urgent"

# Optional: let the agent save a suggested reply to Outlook Drafts (never sends).
[triggers.vip-outage-triage.reply]
draft = true
style = "Concise, professional. Acknowledge and state the next step."
```

> **Why named tables?** Each trigger and its sub-tables are grouped under one
> unambiguous prefix, so a second trigger can't accidentally absorb the keys of
> the one above it. Within a single trigger's table, scalar keys (`mode`,
> `rules`, `run`, …) must still come **before** that trigger's sub-table headers
> — a key written after `[triggers.<name>.match]` is parsed *into* the match
> table (e.g. `rules` becomes `triggers.<name>.match.rules`, emptying your real
> `rules` and adding a phantom match field no event can satisfy, so the trigger
> never fires). The Triggers tab surfaces a clear error if it spots this.
>
> The old `[[triggers]]` array-of-tables form is no longer supported (it was
> error-prone — a bare `[triggers.match]` sub-table attached to whichever
> `[[triggers]]` preceded it). If the app finds one, the Triggers tab shows a
> message telling you to switch to the named form.

For a deterministic, no-LLM trigger, use `mode = "rule"`. Two pieces:

1. **`run = "host__runnable"`** — *which* runnable to run. `host` is a host name
   from your sidebar (use **`local`** for the local machine); `runnable` is the
   runnable's name. So a runnable `record-ticket` on the local machine is
   `run = "local__record-ticket"`.
2. **`[triggers.map]`** — *how to fill its arguments*. **The keys are the
   runnable's own argument names**, and the values are templates pulling from the
   event payload with `{{ field }}`. (A literal value with no `{{ }}` is passed
   as-is.)

```toml
[triggers.log-incoming-ticket]
source = "outlook"
event  = "new_mail"
mode   = "rule"
run    = "local__record-ticket"   # host__runnable

[triggers.log-incoming-ticket.map]
# left = the runnable's arg name; right = a template from the event payload
summary = "{{ subject }}"
sender  = "{{ from }}"
queue   = "support"               # a literal (no template) is passed verbatim
```

So if `record-ticket` takes `--summary`, `--sender` and `--queue`, the keys must
be exactly `summary`, `sender`, `queue`. If a `map` key isn't a real arg of that
runnable, the run fails validation (you'll see it in the Event log / `--debug`).
Check the runnable's args in the **Forms** tab or `runspec local`.

The values you can template come from the **event payload** (below); the run is
gated by the autonomy gate just like agent mode, and (unless `ignore_rota`) the
working-hours rota applies to rule triggers too.

Event payload fields you can `match` on / map from:

- `outlook` `new_mail`: `from`, `to`, `subject`, `body`, `received`, `categories`, `entry_id`
- `outlook` `reminder`: `subject`, `start`, `location`, `is_meeting`, `organizer`
- `notifications` `toast`: `app`, `title`, `body`, `text`

### draft_reply — suggested replies

With `[triggers.reply] draft = true` on an `agent`-mode `new_mail` trigger, the
model can call a built-in `draft_reply` tool that **saves a reply to your Outlook
Drafts folder** (the agent's text above the quoted original). The console
**never sends mail** — you review the draft in Outlook and send it yourself, and
each draft save is confirm-gated. The tool is only offered when both the trigger
enables it and the event is a mail with something to reply to.

### Working hours (rota)

So automation only runs while you're around to handle confirms, you can restrict
automation to a **weekly working-hours rota**. The rota lives on its own
first-class **Rota** tab and is the **single shared working-hours gate for both
triggers and schedules** — outside the rota neither a matching event nor a
scheduled fire runs. On the Rota tab: a master switch, then per-day on/off with a
start and end time (local machine time). Outside those windows, matching events
are **ignored** (dropped, not queued — you won't get a backlog dump when the
window opens). It's **off by default** (automation runs anytime until you set it
up), and stored as `[rota]` in its own `runspec_rota.toml` (machine-local, not
synced; installs from before the split are read from — and migrated out of — the
legacy `[rota]` in `runspec_triggers.toml`):

```toml
[rota]
enabled = true
[rota.mon]
on = true
start = "09:00"
end = "17:30"
[rota.sat]
on = false
start = "09:00"
end = "17:00"
```

A single trigger can opt out of the rota (e.g. an off-hours maintenance watcher)
with `ignore_rota = true` in its `[triggers.<name>]` table.

**Driving the rota from your Outlook calendar.** Set `source = "outlook"` and the
window comes from your calendar instead of the grid — you're *on shift* whenever
an event whose subject contains `shift_marker` (default `"shift"`) covers the
moment. Great for rotating shifts: put them in your calendar and the triggers
follow. `hold_during_meetings` (default off) also pauses while you're in a
meeting / OOO. If Outlook can't be read (closed, or non-Windows) it falls back to
the console grid below, so set one as a safety net. (Note: Outlook's *Work Hours*
setting isn't used — it isn't exposed in free/busy and is only readable via Graph,
which managed tenants block; hence the marker-event approach.)

```toml
[rota]
enabled = true
source = "outlook"            # "console" (grid below) | "outlook" (your calendar)
shift_marker = "shift"        # on shift when an event's subject contains this
hold_during_meetings = false  # also pause during meetings / OOO
[rota.mon]                    # fallback grid, used if Outlook can't be read
on = true
start = "09:00"
end = "17:30"
```

**The chat agent knows about all of this.** Each interactive turn tells the agent
whether the rota is on and whether it's **currently inside or outside** working
hours, plus the triggers and schedules you have configured. So if you ask it to
run, schedule, or trigger something outside your hours it can point that out and
confirm before acting — and it can reason about your existing automation rather
than being oblivious to it. (Interactive requests are never hard-blocked by the
rota — that gate only applies to *automated* triggers and schedules.) The agent
also has a read-only **`console_status`** tool it can call to re-check the rota
and your triggers/schedules mid-conversation — handy in a long session where the
clock may have crossed a working-hours boundary.

### Audit timeline

Every run card keeps a timestamped **timeline** of what happened to it —
stage changes (triggered → triage → … → done) and each tool the agent ran.
Expand a card's *timeline* to read the full trace; it's recorded on the backend,
so even a card that flew through the early statuses leaves a complete record.

### Event log

Below the run inbox, the **Event log** shows *every* event the engine saw and what
happened to it: **fired** (which triggers ran), **skipped (working hours)** (it
matched but the rota gated it), or **no match**. It's the first place to look
when a trigger doesn't fire:

- the email shows **no match** → your `match` regex (or the `from`/subject) didn't
  match — check the trigger's `[triggers.match]`;
- **skipped (working hours)** → the rota gated it — you're outside the window /
  off-shift, or the rota's master switch is on when you didn't intend it;
- the email **isn't in the log at all** → the source never delivered it (e.g.
  Outlook's `NewMailEx` didn't fire, or it landed outside the Inbox).

Rota times are **local machine time**, not UTC.

### Safe by default

- **Confirm-by-default.** Trigger-driven runnables use the same autonomy gate as
  chat; a `manual` runnable is still never run by the agent. Approve / Deny is
  surfaced on the run card (the *Awaiting human* status).
- **Forward-only.** Sources only react to events that arrive **while the console
  is running** — your inbox / existing toasts are snapshotted at startup and
  never replayed, so reopening the console doesn't re-handle old messages.
- **Kill switch.** Toggle any trigger off in the Triggers tab (it stops
  immediately); delete it from `runspec_triggers.toml` to remove it.

---

## Schedules

The **Schedules** tab runs runnables (or emits trigger events) on a clock, and
mirrors the Triggers view's layout:

- the **schedules table** sits inside a collapsible bar at the top;
- a **run inbox** + detail drawer shows the runs of runnable-action schedules
  (reusing the same component as the Triggers tab) so each scheduled run flows
  through the same lifecycle status as a triggered one;
- an **Event log** records *every* scheduled fire and its disposition —
  **fired** or **skipped (working hours)**;
- the shared **rota gates schedules too**: a fire that lands outside working
  hours is skipped, exactly like a trigger. A single schedule can opt out with
  `ignore_rota = true` for an off-hours job.

A nav badge appears on the tab while a schedule run is in flight.

---

## Runbooks & offline runbook docs

A **runbook** is a saved, reusable procedure the chat agent can follow — "when
you see *this symptom*, run *these runnables in this order*". Runbooks live in
`runspec_runbooks.toml` as a `[[runbook]]` collection and **ride Config Sync** (merged
by `id`), so a team ships vetted procedures from one git tag. The agent reads them
with its `load_runbook` / `list_runbooks` tools, renders one inline as a trigger
action with `run_runbook`, and you can invoke one from the chat `$` menu. Author
them by asking the agent to save one (its `save_runbook` tool writes the entry), by
adding a `[[runbook]]` block to the config-sync repo, or by editing the file
directly.

```toml
# runspec_runbooks.toml — a runbook collection (rides Config Sync, merged by id)
[[runbook]]
id = "web-502-triage"
title = "Triage 502s on the web tier"
symptom = "Web nodes returning 502s after a deploy"
tags = ["web", "incident"]
steps = """
1. Probe each node: web-01__http-check and web-02__http-check.
2. For any unhealthy node, run {{host}}__restart-service --name nginx.
3. Re-run http-check and confirm 200s before resolving.
"""

[runbook.args]
host = { type = "choice", options = ["web-01", "web-02"], description = "Node to act on" }
```

`steps` is a prompt template: `web-01__http-check` is the `host__runnable` tool
token the agent calls (use the runnables actually installed on your hosts), and
`{{host}}` is filled from the `[runbook.args]` table — which uses the **same arg
grammar** as a `runspec.toml`.

### Generating the runbook wiki

The same runbooks generate a human-readable **wiki** for when the console itself
is down — so a person can SSH to a host and run the same runnables the agent
would. In **Settings → Config Sync → Runbook docs**, point the output directory
at your local clone of the config-sync repo and click **Generate**. It renders,
from the team's runbooks + fleet + cached runnable catalogue (offline, from the
last sync — no live SSH):

- `README.md` — index with an outage preamble
- `runbooks/<id>.md` — one page per runbook (when-to-use, steps, params)
- `hosts.md`, `runnables.md`, `runnables/<host>.md` — the offline host/runnable
  catalogue
- `maintaining-docs.md` — how to regenerate, plus a paste-able
  `regenerate-runbook-docs` runbook you can drop into `runspec_runbooks.toml` so
  anyone can trigger a refresh from the `$` menu

The output is **deterministic** (everything sorted, no timestamps), so it diffs
cleanly — review, commit, and push it back to the config repo, and the team gets
the update on their next Config Sync pull. Re-running changes nothing unless the
runbooks or fleet actually changed.

---

## Output rendering

When a runnable prints JSON, the console renders it instead of dumping raw text:

- a **JSON array of objects** → a table
- a **dict that wraps array(s)-of-objects** → titled section tables
  (e.g. `{System:[…], Application:[…]}`, or scalar metadata plus one array of
  rows)
- a **plain dict** → a key/value view
- nested objects/arrays inside a row → an **expandable** sub-table or JSON

Each block has a table/raw toggle and a "copy as table" action. For the
cleanest tables, a runnable should emit a **top-level array of flat row
objects**; the console still renders nested shapes gracefully when it doesn't.

---

## Usage

```bash
runspec-console              # production (bundled UI)
runspec-console --dev        # point pywebview at a running Vite dev server
runspec-console --devtools   # enable the Chromium inspector in a prod build
```

---

→ **The hero's work:** [legendary runbooks across the fleet](console.md#runbooks-offline-runbook-docs) — where the journey has been leading all along.
