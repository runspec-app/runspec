# Automation — triggers, schedules, rota

Three tabs turn the console from an interactive tool into an operations
station:

- **Triggers** react to events on the machine — a new email, a meeting
  reminder, a system toast — and run runnables or a headless agent turn.
  *(Windows)*
- **Schedules** run runnables, runbooks, or agent prompts on a clock.
- **Rota** is the shared working-hours gate over both, so automation only
  runs while someone is around to approve confirmations.

Everything automated goes through the **same autonomy gate as chat** —
confirm-by-default, with run cards parked at *Awaiting human* until you
approve or deny.

The chat agent can set all of this up in conversation — `create_schedule`,
`manage_triggers`, and `manage_rota` each show a confirmation card before
writing anything, and `emit_event` fires synthetic events for testing. The
reverse is deliberately impossible: a turn that was *started by* a trigger or
schedule can never use the config tools, so automation cannot rewrite its own
rules. See [Configure the console](console-agent.md#configure-the-console).

---

## Triggers (Windows)

A trigger binds an **event source** + **event type** (optionally narrowed by
regex `match` rules) to an **action** — either a deterministic runnable call
(`mode = "rule"`) or a headless agent turn (`mode = "agent"`) that follows
the trigger's `rules` with the full runnable toolset. The **Triggers** tab
shows your triggers and a **run inbox** where each matched event flows
through its lifecycle: **Triggered → Triage → Awaiting human → Running →
Done / Failed**.

> Triggers only fire while the console is running, and never replay a backlog
> — your inbox and existing toasts are snapshotted at startup, so reopening
> the console doesn't re-handle old messages. An LLM provider must be
> configured for `mode = "agent"`.

### Event sources

| Source (`source =`) | Events (`event =`) | Notes |
|---|---|---|
| `outlook`       | `new_mail`, `reminder` | Uses the **signed-in desktop Outlook** client over COM — no extra dependency, rides your existing SSO session. |
| `notifications` | `toast`                | Every system toast (Teams, Slack, anything) via one listener. Needs the `notifications` extra **and** a one-time Windows consent grant. |

```
pip install "runspec-console[notifications]"
```

The Triggers tab shows each source's availability and the reason any is off.

### Configuring triggers

Create and edit triggers on the **Triggers** tab, or describe one to the
agent ("when an email about an outage arrives from bigcustomer.com, …") and
approve the validated config it proposes. The tab's editor has a host/group +
runnable picker that lists the runnable's declared arguments as labelled
rows, so you rarely hand-write the config — but it lands in
`runspec_triggers.toml` as named tables you can also edit directly:

```toml
[triggers.vip-outage-triage]
source = "outlook"
event  = "new_mail"
mode   = "agent"               # "agent" (LLM-in-the-loop) | "rule" (direct runnable)
autonomy = "confirm"

# agent mode: the rules the model follows for this trigger. (A scalar key, so
# it must appear BEFORE the [triggers.<name>.*] sub-tables below.)
rules = """
If this reports an outage, identify the affected host, run the diagnostics
runnable against it, and draft a short reply summarising findings. Never
restart a service or send mail without confirmation.
"""

# Cheap regex pre-filter on the event payload — all patterns must match.
[triggers.vip-outage-triage.match]
from    = ".*@bigcustomer\\.com"
subject = "(?i)outage|down|urgent"

# Optional: let the agent save a suggested reply to Outlook Drafts (never sends).
[triggers.vip-outage-triage.reply]
draft = true
style = "Concise, professional. Acknowledge and state the next step."
```

Each trigger row has a **"Trigger now"** button that fires it with a
*synthetic* event — a pasted message plus `key=value` fields — so you can
test without waiting for a real event.

!!! warning "Key ordering inside a trigger table"
    Scalar keys (`mode`, `rules`, `run`, …) must come **before** that
    trigger's sub-table headers — a key written after
    `[triggers.<name>.match]` is parsed *into* the match table, emptying the
    real key and adding a phantom match field no event can satisfy. The
    Triggers tab surfaces a clear error if it spots this.

### Rule mode — deterministic, no LLM

```toml
[triggers.log-incoming-ticket]
source = "outlook"
event  = "new_mail"
mode   = "rule"
run    = "local__record-ticket"   # host__runnable ("local" = this machine)

[triggers.log-incoming-ticket.map]
# left = the runnable's arg name; right = a template from the event payload
summary = "{{ subject }}"
sender  = "{{ from }}"
queue   = "support"               # a literal (no template) is passed verbatim
```

`map` keys must be the runnable's real argument names — a wrong key fails
validation (visible in the Event log). Payload fields you can `match` on and
map from:

- `outlook` `new_mail`: `from`, `to`, `subject`, `body`, `received`, `categories`, `entry_id`
- `outlook` `reminder`: `subject`, `start`, `location`, `is_meeting`, `organizer`
- `notifications` `toast`: `app`, `title`, `body`, `text`

A rule trigger can also run a **bounded list of runnables** (an `actions`
array with `action_mode = "sequential"` or `"parallel"`, up to 10) behind one
grouped confirmation, with an optional single LLM extraction step whose
output feeds the arg maps as `{{ _llm.* }}` templates. The Triggers editor
builds all of this.

### Agent-mode triggers and action tools

An agent-mode trigger's turn has the full runnable toolset plus the agent's
action tools (`create_document`, `run_runbook`, `create_schedule`,
`emit_event`) — scoped per trigger by a `runnables` allowlist and a parallel
`action_allow` allowlist. With `[triggers.<name>.reply] draft = true` on a
`new_mail` trigger, the model can save a suggested reply to your Outlook
**Drafts** folder — the console never sends mail from a trigger; each draft
save is confirm-gated.

### Debugging: the Event log

Below the run inbox, the **Event log** records *every* event the engine saw:
**fired** (which triggers ran), **skipped (working hours)** (matched but
rota-gated), or **no match**. First place to look when a trigger doesn't
fire: *no match* → your `match` regex; *skipped* → the rota; *not in the log
at all* → the source never delivered it (e.g. the mail landed outside the
Inbox). Every run card also keeps a timestamped **timeline** of stage changes
and each tool the agent ran.

---

## Schedules

The **Schedules** tab runs work on a clock — cron or interval — with the same
run inbox, event log, and lifecycle as triggers. A schedule's action can be:

- a **runnable** — targeting one host, a whole **group** (fans out), or a
  candidate **host list** (runs on the first connected one — failover);
- a **runbook** by id, with its arguments filled;
- a **prompt** — a headless agent turn on a fixed instruction ("check the
  backup log and flag anything unusual").

The agent creates schedules directly:

> Schedule a check of the backup log on nas-01 every morning at 07:30 — if anything failed overnight, summarise it.

`create_schedule` shows a confirmation card; the schedule then lives on the
Schedules tab where you can pause, edit, or delete it.

---

## Rota — working hours for automation

Because automated runs still need a human for confirmations, the **Rota** tab
restricts automation to a weekly working-hours grid: outside the window,
matching events and scheduled fires are **skipped** (dropped, not queued — no
backlog dump when the window opens). Off by default; per-day on/off with
start/end times (local machine time). A single trigger or schedule can opt
out with `ignore_rota = true` (e.g. an off-hours maintenance watcher).

```toml
[rota]
enabled = true
[rota.mon]
on = true
start = "09:00"
end = "17:30"
```

**Drive it from your Outlook calendar** instead of the grid with
`source = "outlook"`: you're on shift whenever a calendar event whose subject
contains `shift_marker` (default `"shift"`) covers the moment;
`hold_during_meetings = true` also pauses during meetings/OOO. If Outlook
can't be read, it falls back to the grid.

Two presence controls layer on top (both **Windows**):

- **Take a break** — a Rota-tab button suspends triggers and schedules for N
  minutes (15/30/60/custom) and auto-resumes, staying paused if the screen is
  still locked at the return time.
- **Lock auto-suspend** (default **on**) — reactive triggers don't fire while
  the screen is locked, with a live indicator on the Rota tab. It fails
  closed (an uncertain probe means paused), applies to all triggers with no
  per-trigger bypass, and exempts schedules (pre-authorised work, not
  reactions-while-away). It's a governance control, not a security boundary;
  every suppression is logged.

The chat agent is told the rota state and your configured triggers and
schedules each turn — ask it "why didn't my trigger fire?" and it can reason
about the gates. Interactive chat requests are never rota-blocked.

---

## Microsoft 365 setup (Windows)

Two independent integrations:

**Outlook desktop (COM)** — the trigger source and the agent's
`outlook_*` tools use the signed-in desktop Outlook client directly. No
setup beyond having Outlook running and signed in; works where Graph is
blocked.

**Microsoft Graph** — the `runspec-windows` Graph runnables (Teams, OneDrive,
and Graph-based mail/calendar) authenticate as you via the device-code flow
(no client secret). One-time setup:

1. **Register an app** in your Entra (Azure AD) tenant
   (*App registrations → New registration*).
2. **Authentication → "Allow public client flows" → Yes**.
3. **API permissions → Microsoft Graph → Delegated**: add `User.Read`,
   `Mail.Read`, `Chat.Read`, `Calendars.Read`, `Files.Read.All`; grant
   consent (`Chat.Read` typically needs a tenant admin).
4. Copy the **Application (client) ID** and **Directory (tenant) ID** into
   `%APPDATA%\runspec-windows\graph.toml` (the client id is not a secret):

    ```ini
    client_id = "00000000-0000-0000-0000-000000000000"
    tenant    = "yourcompany.onmicrosoft.com"
    ```

5. Run `graph-login` once (it prints a URL + code); the token caches and
   refreshes silently.

For a **personal** Microsoft account, register the app for personal accounts
and set `tenant = "consumers"` (or `"common"`).
