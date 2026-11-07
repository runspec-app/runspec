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

The claim also writes a **`Claimed: <name>`** category on the email, using the
claimer's Outlook display name (`Ruiz, Ana` is written as `Ana Ruiz`; the
Windows login is used when Outlook can't say). After the move the action runs
against the moved email, so a drafted reply finds it.

### Outlook categories — see progress on the email

Turn on **Settings → Mailbox → Outlook categories** to tag each email a trigger
handles, so anyone looking at the mailbox in Outlook can see what happened
without opening the console:

| Category | When |
|---|---|
| `runspec: <trigger>` | The trigger matched the email (stays) |
| `runspec: awaiting claim` | A claim trigger parked it behind a Claim button |
| `Claimed: <name>` | Someone claimed it (also clears the status) |
| `runspec: done` | The action finished |
| `runspec: failed` | The action failed |
| `runspec: needs a person` | A step was declined, timed out at the approval, or is manual-only |
| `runspec: skipped` | This console held back (outside working hours or the screen was locked) |
| `runspec: reply drafted` | A reply was saved to Drafts |

Only one status (`awaiting claim`, `done`, `failed`, `needs a person`,
`skipped`) is on an email at a time; a new one replaces the old. `skipped` never
replaces another status, so an off-shift console doesn't hide what an on-shift
one is doing. A trigger can turn categories on or off for itself (**Outlook
categories** in the Triggers editor, or `categories = true/false`).

```toml
[mail_categories]
enabled = true
prefix  = "runspec"   # the text before the colon
colors  = true        # add the categories, with colours, to the category lists
```

How it behaves with a shared mailbox:

- The category names are saved on the email in Exchange, so everyone with
  access sees them in classic Outlook, new Outlook and Outlook on the web. In
  cached mode they usually show up within a minute.
- Colours come from a master category list. With `colors = true` the console
  adds its categories to your own list and tries the mailbox's list too. For a
  shared mailbox opened with full access, Outlook shows the mailbox's own list;
  with only folder access, each person sees their own list. A name that isn't in
  the list shows without a colour.
- Writing a category needs permission to edit items in the folder.
- Writes happen in the background and are best-effort: an email that has moved
  or can't be saved is logged and skipped, never failing the run. "Trigger now"
  test runs don't write categories.

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

Give a schedule a **Name** (`name` on the `[[schedule]]` entry) to label its
run cards and session chips. Without one, the console labels a run from what it
does: a prompt's first few words, `runbook <id>`, `saved run <id>`, or the
runnable and subcommand. The generated id (`rs-prompt-…`) is never shown.

These schedules fire only while this console is open. For **always-on, shared**
schedules that run on the host itself, use `runspec-scheduler` on that host: its
cron backend (the default, see [the cron scheduler design](design/cron-scheduler.md))
or, from 0.5.0, its daemon backend (`runspec-scheduler use daemon`, which adds
`every` intervals and catch-up; see [the daemon design](design/venv-scheduler.md)).

### All schedules: this console and every host

Four option cards across the top of the Schedules tab pick what you see. Each
card shows how many schedules it covers and, in amber, how many have a problem.
The choice is remembered.

| Card | What it shows |
|---|---|
| **All** | Every schedule that fires: this console's own plus every host schedule |
| **This console** | The console's own schedules (the table above, where you add and edit them) |
| **Host cron** | Schedules each venv's crontab fires (`runspec-scheduler sync`) |
| **Host daemon** | Schedules a venv's `runspec-scheduler run` process fires (runspec-scheduler 0.5.0 and later); *none yet* until a venv uses it |

The host cards read every venv on **local and every connected host in the
active profile** in one go, one `runspec-scheduler status` (≥ 0.3.0) per venv,
in parallel within `[refresh] workers`. The list is read when you open the tab,
when the profile's connected hosts change, and when you press **Refresh**; reopening
the tab shows the last read at once and refreshes underneath.

One row per schedule: its state, id (or name), runnable and args, cron or
interval, next fire, last run (with a button that opens it in **History**, which
searches the run's `sched:<id>` label), host / venv, and the venv owner → the
account it runs as. On **All** each row also says where it fires. The state tag:

- *ok*, *running*, *disabled*;
- *last run failed* — expand the row for its recent runs (including recorded
  **skipped** fires) and the last run's captured **stdout/stderr** and errors;
- *not installed* — in the config but not in the crontab (`runspec-scheduler
  sync` on the host); *not installable* — cron can't run it (an `every` interval);
- *stale line* — a crontab line for a schedule the config no longer has, or left
  behind when the venv moved to the daemon. It can still fire;
- *scheduler stopped* / *scheduler stale* — the venv uses the daemon but its
  process isn't running, or stopped sending heartbeats;
- *can't read* — the venv's `status` failed; expand it for the reason;
- *legacy line* — an older scheduler wrote the crontab line and its fires aren't
  recorded (`runspec-scheduler sync` to upgrade); *unknown* — the owner's crontab
  couldn't be read without a password.

The **Host daemon** card also shows each daemon venv's process: running /
stale / stopped, pid, uptime, last heartbeat, the runspec-scheduler version it
runs, and what keeps it running (systemd --user, supervisord or the cron
watchdog; *no keeper* means nothing restarts it after a crash or reboot). A
*restart needed* tag means pip installed a different runspec-scheduler since
the daemon started, so it still runs the old code. Expanding a daemon schedule
shows whether a missed fire runs on start (`catch_up`) and its timeout. With
more than three daemons, the strips fold into one summary line ("8 scheduler
daemons: 7 running, 1 stopped, 2 need a restart"); while folded, up to three
that need a look still show. Click the line to open or fold the list; the
choice is remembered.

Each strip has two buttons (when editing is available):

- **Restart**, or **Start** for a stopped or stale daemon, runs the venv's own
  `runspec-scheduler restart` as the venv owner. It goes through the venv's
  keeper (`systemctl --user restart`, `supervisorctl restart`, or stop and
  start for the cron watchdog) and waits for the new daemon to report back.
  Fires already running carry on.
- **Settings** shows and changes the daemon's defaults: **Fire timeout**
  (how long one fire may run; `0` = no limit; a schedule can set its own),
  **Misfire grace** (how late a fire may still start before it is recorded as
  skipped) and **Catch-up window** (how far back a catch-up schedule looks on
  start). Values are seconds or `90m` / `6h` / `1d`, saved with
  `runspec-scheduler settings` as the owner; the daemon picks them up by
  itself. The dialog also names the daemon's log file.

Both need runspec-scheduler 0.7.0 in the venv; on an older one the console
says to upgrade it with **Update venv…**. Each change is in the console's
audit log (`scheduler.host.restarted` / `scheduler.host.settings`).

Filters: the search box matches id, runnable and args, and takes the same
`host:` and `group:` tokens as Forms and Marketplace (`host:web group:prod`).
Quick filters narrow by source (on **All**), state, owner and run-as account;
**Only problems** keeps failed, not installed, stale, stopped and unreadable
rows. Sort by next fire (the default), last run, host or runnable. Filters are
remembered.

The console reads host schedules with `status`. In a venv owned by a service
account the scheduler only runs as that owner, so the console runs it as the
owner the same way it runs the venv's runnables (`sudo -n -u <owner>
{venv}/bin/python …`); if that sudo is refused, the venv shows as *can't read*
naming the owner. Venvs without `runspec-scheduler` are listed under the table;
install it there from the **Marketplace** to schedule that venv's runnables.
Every fire runs through `runspec exec`, so it also appears in **History**, where
a skipped fire shows as *skipped* rather than as a success.

#### Adding and editing host schedules

On **All**, **Host cron** and **Host daemon**, **New host schedule** adds a
schedule to a venv's runspec-scheduler, and each host row has **Edit**,
**Pause** / **Resume** and **Remove**. The dialog asks for:

- **Where**: a venv whose scheduler was read, with its backend (cron or daemon)
  and owner. Fixed when editing.
- **Runnable** and **Subcommand** from that venv, and its arguments as the same
  form Forms uses. Only arguments you change from their default are stored.
- **Schedule id**: the crontab marker and the History label `sched:<id>`.
  Suggested from the runnable; changing it when editing renames the schedule.
- **When**: a cron expression (presets or custom) and an optional timezone. On a
  daemon venv you can also pick an **Interval** (`15m`, `1h`) and **Run once if
  fires were missed**.
- **Enabled** and **Timeout** (`90m`, `6h`, seconds; `0` = no limit).

Saving runs that venv's own `runspec-scheduler add` (or `remove`) as the venv
owner, the same way the console reads `status`. On a **cron** venv it then runs
`runspec-scheduler sync`, so the crontab matches the config; only lines marked
`# runspec:sched:` change, and from runspec-scheduler 0.6.1 the crontab is
backed up to `{venv}/runspec_scheduler_backups/` before each rewrite. A **daemon**
venv reloads its config by itself. Rows the crontab is out of step with (*not
installed*, *stale line*, *legacy line*) get a **Sync crontab** button.

A host schedule never carries credentials. Password arguments aren't offered.
Put what its fires need in the venv's **schedule-only env file**,
`{venv}/runspec_schedules.env` (runspec-scheduler 0.7.1, runspec 0.82.0): every
fire (cron, daemon and Run now) loads it on top of `.runspec_env`, and nothing
else in the venv does. Console, agent and hand-run runs only load
`.runspec_env`, so a service account's credentials kept in the schedules file
never stand in for an operator who didn't pick their own. Make the file mode
600, owned by the account the fires run as; `runspec exec` refuses one that
others can write or that belongs to another account (the fire is recorded as
failed, exit 78).

Once you pick a runnable, the dialog lists the credentials it reads from the
environment and, for each, whether the schedules file sets it (and with which
variable), whether only `.runspec_env` does (it works, but every run in the
venv uses it, so move it), or which variable to add. **Open Env files** opens
the Env files window on that venv's schedules file, and **Check again**
re-reads it. Only variable names are shown, never values. Each change is recorded in the console's audit
log (`schedule.host.saved` / `removed` / `synced`, or `*_failed`). Local
Windows venvs can't be edited: the scheduler fires with cron or its daemon,
which need Linux.

#### Run now

Each **Host cron** and **Host daemon** row has a **Run now** button. After a
confirm it runs that venv's `runspec-scheduler run-now --detach` as the venv
owner. The fire runs as a scheduled fire would (the schedule's run-as account
on a cron venv, the venv owner on a daemon venv), in its own session, so it
keeps going after the console's call returns. Its run shows in History a few
seconds later. If the schedule is already running, the second fire is recorded
as *skipped*. Needs runspec-scheduler 0.7.0 in the venv. Recorded in the
console's audit log as `schedule.host.run_now` (or `_failed`).

#### Who ran it

History's **Run by** column tells host schedule fires apart from the agent:
**cron** (a crontab line fired), **daemon** (the scheduler daemon fired) and
**run now** (the Run now button). Fires from runspec-scheduler before 0.7.0 show
as **host sched**. A cron line picks up the new marker on its next sync, a
daemon after a restart.

#### Who is handling a failure

Host schedules are shared: several people may see the same failure. To stop two
of them working on it, or a fixed job reading as broken, failing host cron and
daemon rows get a checkbox. Tick one or several (across venvs and hosts), then:

- **I'm on it** claims them, with an optional note. The row reads **being
  handled** with your name and note.
- **Dealt with…** marks them handled, with a required note saying what was done.
  The row reads **dealt with**.
- **Release** lets go of a claim (or a dealt-with mark), and the row reads
  *last run failed* again.

A mark covers the schedule's failures until it next succeeds, so a job that
keeps failing for the same reason is claimed once and the next good run clears
it. *Being handled* and *dealt with* don't count as problems on the cards or
with **Only problems**, so a quick look shows what still needs someone. A claim
with no update for 4 hours counts as a problem again.

The mark is kept on the host, in the venv's runs index: the console runs the
venv's own `runspec history claim|resolve|release` as the venv owner, one call
per venv, and records your login as who did it. Everyone else's console sees
it, and so does `runspec history jobs` on the host (see
[`runspec history`](cli.md#who-is-handling-a-failure)). Needs runspec 0.83.0
and runspec-scheduler 0.7.2 in the venv. Recorded in the console's audit log as
`schedule.host.claim` / `resolve` / `release`, and on the venv's own ledger as
`run.claim` / `run.resolve` / `run.release`.

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
