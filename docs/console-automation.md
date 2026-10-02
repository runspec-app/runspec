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

- `outlook` `new_mail`: `from`, `to`, `subject`, `body`, `received`, `categories`, `entry_id`, `mailbox`, `folder`, `conversation_id` (`mailbox`/`folder` tag which watched folder the mail landed in — a claim trigger matches on `folder`)
- `outlook` `reminder`: `subject`, `start`, `location`, `is_meeting`, `organizer`
- `notifications` `toast`: `app`, `title`, `body`, `text`

A rule trigger can also run a **bounded list of runnables** (an `actions`
array with `action_mode = "sequential"` or `"parallel"`, up to 10) behind one
grouped confirmation, with an optional single LLM extraction step whose
output feeds the arg maps as `{{ _llm.* }}` templates. The Triggers editor
builds all of this.

### Monitoring a mailbox — folders

The `outlook` `new_mail` source watches one **primary** mailbox plus optional
extra mailboxes, configured in **Settings → Mailbox** and stored in
`config.toml`. Each mailbox takes a **folders list** — leave it empty to
monitor the Inbox only:

```toml
[mailbox]
address = "helpdesk@corp.com"   # blank/absent → your own default account
folders = ["Tickets/Open", "Tickets/Alerts"]  # empty/absent → the Inbox
label   = "Helpdesk"            # optional display label

# Additional watch-only mailboxes/folders (no room/sign-up identity):
[[watch]]
address = "alerts@corp.com"
folders = ["Inbox", "Escalations"]
```

- `folders` are `/`-separated paths under `address`; each is watched
  independently and its `new_mail` events are tagged with that `folder`, so a
  trigger's `match.folder` can scope to one folder (queue).
- Empty or absent `folders` (or the legacy single `folder = "…"`, still
  accepted) monitors the **Inbox** only.
- The folders list is machine-local: it's kept on your machine on a Config
  Sync pull (`config.toml:mailbox.folders` is a `preserve` default).

### Shared mailboxes — claim (one operator wins)

When several consoles watch the **same** shared mailbox+folder, by default
every one reacts to every new mail (duplicate replies / runs). Turn on a
**claim** on the matching trigger so exactly one operator takes each email:

```toml
[triggers.helpdesk-claim]
source       = "outlook"
event        = "new_mail"
mode         = "agent"            # after a claim, runs as usual (agent or rule)
claim        = true               # park each matched mail behind a Claim button
claim_folder = "Tickets/Claimed"  # an EXISTING folder a claimed email moves into

[triggers.helpdesk-claim.match]
folder = "^Tickets/Open$"         # required with claim — the folder to match on
```

How it works: a matched mail parks in the run inbox as **Awaiting claim** with
a **Claim** button. Clicking it **moves the email into `claim_folder`** — the
move is the claim, arbitrated by Exchange itself (no extra mail is sent). The
winning console then runs the trigger's action; every other console's card
flips to **"claimed by X"** and its Claim button disappears.

- `claim_folder` **must already exist** — create it in Outlook first (a missing
  folder shows an error on the card and the mail stays parked).
- `claim` needs `match.folder` (the scope) and applies only to `outlook`
  `new_mail`.
- The Triggers editor exposes **Folder to match**, a **Claim-based system**
  switch, and the **Claim folder** field for this.

This is the reliable, opt-in replacement for coordinating a shared helpdesk
mailbox across a team.

### Scoping a trigger to one mailbox

Every `new_mail` event is tagged with **both** `mailbox` and `folder`. A trigger
that matches only on `folder` fires for that folder in **any** monitored mailbox
— so if two mailboxes both have a `Tickets/Open`, one folder-only trigger reacts
to both. To scope to a single mailbox, add a `mailbox` match:

```toml
[triggers.helpdesk-a.match]
mailbox = "^helpdesk-a@corp\\.com$"   # only this mailbox
folder  = "^Tickets/Open$"
```

The Triggers editor exposes this as a **Mailbox to match** field (optional)
alongside **Folder to match**. Leave it blank to fire for the folder in any
mailbox.

### Agent-mode triggers know where the mail came from

When an **agent-mode** `new_mail` trigger runs, the agent is told the mailbox and
folder the email arrived in and instructed to target that location when it reads
Outlook (e.g. searching for an earlier or similar message) — so it doesn't fall
back to the default Inbox. Testing with **Trigger now** carries the same context:
for an Outlook trigger the dialog pre-fills the `folder`/`mailbox` fields from the
trigger's match (a real arriving email supplies them automatically).

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

- a **runnable** — optionally into one of its **subcommands** — targeting one
  host, a whole **group** (fans out), or a candidate **host list** (runs on
  the first connected one — failover);
- a **runbook** by id, with its arguments filled;
- a **saved run** by id — a [deterministic preset](console-saved-runs.md)
  whose own stored targeting decides the hosts, with the schedule's values
  filling its open params;
- a **prompt** — a headless agent turn on a fixed instruction ("check the
  backup log and flag anything unusual").

The agent creates schedules directly:

> Schedule a check of the backup log on nas-01 every morning at 07:30 — if anything failed overnight, summarise it.

`create_schedule` shows a confirmation card; the schedule then lives on the
Schedules tab where you can pause, edit, or delete it.

These schedules fire only while this console is open. For **always-on, shared**
schedules that run on the host itself, use `runspec-scheduler`'s cron backend on
that host (see [the cron scheduler design](design/cron-scheduler.md)).

### Shared (host cron) schedules — read-only

The toggle at the top of the Schedules tab switches between **This console** and
**Shared (host cron)**. The shared view shows, for the host selected in the
sidebar, each venv that has `runspec-scheduler` (≥ 0.3.0) installed:

- every configured schedule — runnable and args, cron/interval, `run_as`;
- whether its **crontab line** is installed. *legacy line* means an older
  scheduler wrote it, and its fires aren't recorded — run
  `runspec-scheduler sync` on the host to upgrade. *not installed* means it's in
  the config but not in the crontab. *unknown* means the owner's crontab couldn't
  be read without a password;
- the **last run** and **next run**; expand a row for the recent runs (including
  recorded **skipped** fires, when the previous run was still going) and the last
  run's captured **stdout/stderr** and errors;
- crontab lines left over from schedules no longer in the config.

It reads `runspec-scheduler status` over SSH as your login user and changes
nothing. Schedules are managed on the host (`runspec-scheduler add` / `remove` /
`sync`). Every fire runs through `runspec exec`, so it also appears in
**History**, where a skipped fire shows as *skipped* rather than as a success.

---

## Choosing the model per trigger, schedule or runbook

By default every assistant turn uses the `[llm] model`. Automation can pick its
own — a cheap, fast model for routine work, a more capable one where success
matters most. Pick it from your **Available models** list (Settings → Model /
API); blank means the `[llm] model`. Only the model changes — the provider, key,
effort and other `[llm]` settings stay the same.

| Where | Field | Applies to |
|---|---|---|
| Trigger | **Model (optional)** in the trigger editor (`model` in `runspec_triggers.toml`) | An agent-mode trigger's turn, or a rule trigger's pre-turn extract step |
| Schedule | **Model (optional)** for a *prompt* or *runbook* schedule (`model` on the `[[schedule]]` entry) | That scheduled agent turn |
| Runbook | **Model (optional)** in the runbook editor on the Forms tab (`model` on the `[[runbook]]` entry) | Any turn the runbook starts itself: the `$` menu, a runbook schedule, a room request |
| Settings | **Background model** (`[llm] utility_model`) | The console's own housekeeping: chat compaction summaries and Improve report synthesis |

A runbook schedule uses the schedule's own model if set, else the runbook's,
else the `[llm] model`. A runbook the agent follows *inside* a chat turn
(`run_runbook`) runs on that turn's model — a turn can't change model midway.

A cheap model does fine on narrow, single-step work (a routine check, extracting
a hostname from an alert). Multi-step work that must find runnables, choose
subcommands and chain calls is where a stronger model earns its cost. The Evals
tab compares models on the same suite without running anything.

On Claude Haiku 4.x the console sends no thinking/effort (Haiku only supports the
older thinking format), so `[llm] effort` is simply ignored for it.

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
