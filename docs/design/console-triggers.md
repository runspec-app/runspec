# Design: Local event triggers for runspec-console (Windows)

**Status:** Draft / for review. No code yet.
**Scope:** Windows only. Outlook COM as the first event source; Notification
Listener scaffolded to drop in immediately after.

---

## 1. What this is, and the mission line it must not cross

runspec is *"an interface specification for anything runnable"* — it declares
**how a runnable is called** and validates the call. Today the *caller* is a
human (chat / Forms) or the LLM (agent mode).

A **trigger** is the symmetric other half: it declares **what causes a runnable
to run**. An event arrives, optional rules decide, a runnable runs (possibly
with a human or the LLM in the loop). Nothing about the runnable contract
changes — we are adding a new kind of *caller*, not a new kind of runnable.

That symmetry is the test for whether this stays runspec-shaped:

- **In scope (interface):** a declarative trigger *contract* — "this source
  emits these events with this payload; map the payload to this runnable call."
  The payload→arg mapping is the existing validator doing its existing job. The
  human-in-the-loop is the existing autonomy gate. We are surfacing machinery
  that already exists, driven by an event instead of a chat message.

- **Out of scope (the cliff):** a general **workflow engine** — branching,
  parallel fan-out, retries, compensation, durable multi-step state machines.
  The moment a trigger has to model "step 2 depends on step 1's result across
  restarts," we are rebuilding Temporal / n8n / BPMN and the console stops being
  a runspec host. A trigger fires **one routing decision** (which may run
  several runnables inside a single agent turn). It does not persist a flow.

If a feature request can only be satisfied by a persistent multi-step
state machine, it belongs in a workflow engine downstream of runspec, not here.

**Host-group fan-out stays under the cliff.** A rule `run` (or an `actions[].run`)
may target a host group as `group:<group>__<runnable>`, which the runner expands
to one `host__runnable` call per group member (see `docs/design/host-groups-targeting.md`).
This is still **one runnable across N hosts** — a flat fire-and-collect batch
behind a single grouped confirm, with no branching, no step-result piping, and no
cross-restart state. The declarative `actions` list keeps its 10-step cap, and the
post-expansion total is bounded (`_MAX_FANOUT_CALLS`) so a few group steps can't
balloon into a workflow-sized matrix.

---

## 2. The loop

```
                       ┌─────────────────────────────────────────────┐
  Windows event   ──▶  │  EventSource.emit(Event)                     │
  (Outlook COM,        └───────────────────┬─────────────────────────┘
   Notification                            │
   listener, …)                            ▼
                              ┌──────────────────────────┐
                              │  TriggerEngine            │
                              │  match source+type+filter │
                              └─────────────┬─────────────┘
                                            │  (creates a run card)
                         ┌──────────────────┴───────────────────┐
                         ▼                                       ▼
                 mode = "rule"                            mode = "agent"
            payload → runnable args                synthesize a message from the
            (validated, no LLM)                    event + the trigger's rules;
                         │                          run a headless agent turn
                         ▼                                       │
              ┌────────────────────────┐                        ▼
              │  autonomy gate          │  ◀────────────────────-┘
              │  (_confirm_decision)    │   each proposed runnable call
              │  confirm → "Awaiting    │
              │   human" run inbox      │
              └───────────┬─────────────┘
                          ▼
                   runnable runs (local / SSH)  →  card → Done
```

The only genuinely new components are the **event sources** and the
**TriggerEngine + run inbox**. Everything to the right of the engine
already exists.

---

## 3. What we reuse (this is most of it)

| Existing symbol (`bridge.py`) | Role in triggers |
|---|---|
| `send_chat()` → `asyncio.run(_agentic_chat_turn(...))` on a daemon thread | Template for a **headless, trigger-driven agent turn** — same pattern, no human message |
| `_agentic_chat_turn()` | The agent loop (stream → tool_use → gate → tool_result). Reused for `mode = "agent"` triggers, run against an isolated per-card history |
| `_runnables_to_tools()` | The toolset the agent may call — unchanged |
| `_confirm_decision()` / `_effective_tool_autonomy()` | Gate decision: `run` / `manual` / needs-prompt. **Confirm-by-default** is exactly the human-in-the-loop we want |
| `_register_confirm()` → `runspec:tool_confirm` + `threading.Event` keyed by `request_id`; `resolve_tool_confirmation()` | The "Awaiting human" parking mechanism. A parked gate **is** a run card at the Awaiting-human lifecycle status |
| `_custom_agent_tools()` / `_run_custom_tool()` / `_CUSTOM_TOOL_NAMES` | The seam for any built-in tools a trigger flow needs (e.g. "draft reply") |
| `_dispatch(event, detail)` → `window.evaluate_js(dispatchEvent(CustomEvent…))` | How run card updates reach the UI — new `runspec:trigger_*` events ride the same channel |
| `config.py` (`config_path`, host/filter persistence) | Where `[[triggers]]` is stored, same as hosts and filters today |

The autonomy gate is the keystone. "Decisions by AI and humans together" is not
new machinery — it is `_confirm_decision` returning `confirm`, surfaced
as a lifecycle status in the run inbox instead of an inline chat prompt.

---

## 4. Event sources — the pluggable layer (scaffolded for #2)

Sources register the same way model adapters do (`register_adapter` in
`adapters/base.py`), so adding the Notification Listener after Outlook is purely
additive: a new file + one `register_source(...)` call + its dependency.

```python
# events.py
@dataclass(frozen=True)
class Event:
    source: str                 # "outlook"
    type: str                   # "new_mail" | "reminder"
    payload: dict[str, Any]     # source-defined, documented per source
    id: str
    received_at: float

class EventSource(Protocol):
    id: str
    def available(self) -> tuple[bool, str]:   # (ok, reason-if-not) — platform/dep/consent
    def event_types(self) -> list[str]
    def start(self, emit: Callable[[Event], None]) -> None:  # begin pushing
    def stop(self) -> None

def register_source(factory: Callable[[], EventSource]) -> None: ...
def get_source(id: str) -> EventSource | None: ...
```

`available()` returns a reason so the UI can say *why* a source is off ("not on
Windows", "Outlook not installed", "notification access not granted") instead of
silently hiding it.

### 4.1 Source #1 — Outlook COM (`sources/outlook.py`)

The desktop Outlook client is **already signed in** with the org's blessed SSO
session, so this rides that session and sidesteps the Conditional-Access wall
that blocks `runspec-windows`' MSAL device-code flow. `pywin32` is already a
console dependency (`pyproject.toml:12`) — **no new dependency**.

Events:

| `type` | Source COM event | Key payload fields |
|---|---|---|
| `new_mail` | `Application.NewMailEx` (→ resolve `EntryID`) | `from`, `to`, `subject`, `body`, `received`, `categories`, `entry_id` |
| `reminder` | `Application.Reminder` | `subject`, `start`, `location`, `is_meeting`, `organizer` |

**Threading / COM detail (the crux):** COM event sinks require a thread that
calls `pythoncom.CoInitialize()`, binds the sink with
`win32com.client.DispatchWithEvents("Outlook.Application", Handler)`, then runs a
message pump (`pythoncom.PumpMessages()`). So the Outlook source owns its **own
daemon thread** holding the pump; the sink handler marshals each event onto a
thread-safe queue and calls `emit(...)`, which hops back to the engine. `stop()`
posts a quit to the pump. This isolates all COM apartment concerns from the
bridge's existing threads. The console already spawns daemon threads liberally
(SSH pool, refresh cycles), so this fits the existing model.

**Forward-only — no backlog replay (A1).** `start()` subscribes to *future*
occurrences only; it must never scan the inbox / reminders collection and emit
existing items. This is the EventSource contract that makes "no persistence" the
right call: the apps keep their own notifications across restarts (we don't clear
them), so nothing is lost — the console simply doesn't re-react to messages that
arrived while it was closed. `NewMailEx` and `Reminder` are naturally
forward-only, so honouring this is mostly a matter of *not* adding a startup
scan.

### 4.2 Source #2 — Notification Listener (`sources/notifications.py`) ✅ *implemented*

`Windows.UI.Notifications.Management.UserNotificationListener` reads **every**
system toast — Teams, Slack, Outlook, anything — through one subscription. This
is the answer for Teams (no clean COM model): don't integrate Teams, listen to
the toast it already raises.

New surface this one needs (the *only* reason it's #2, not #1):
- a new dependency: `winrt` / `winsdk` (WinRT projection),
- a one-time **user-consent grant** (`RequestAccessAsync`) — `available()`
  reports `(False, "notification access not granted")` until granted.

Because of the registry, that was the entire delta: a new file
(`sources/notifications.py`, registering `"notifications"`), the `winsdk`
dependency (the `notifications` extra), and a synchronous `get_access_status()`
check in `available()`. Everything downstream (engine, gate, run inbox) was
unchanged — the prediction held. The listener polls `get_notifications_async`
on an interval and diffs ids (forward-only: the startup backlog is snapshotted,
never replayed); the toast payload extractor is pure and unit-tested with fakes.

### 4.3 Source #3 — Schedule (`sources/schedule.py`) ✅ *implemented*

The clock as an event source. Unlike the other sources, a schedule is **not a
trigger** — it's its own thing, managed in the **Schedules tab** and persisted to
`runspec_schedules.toml`. This keeps the two concepts cleanly separated: a
*schedule* says **when** something happens; a *trigger* says **what to do** when
an event happens. Scheduling inside the console (rather than the OS task
scheduler) is what gives a scheduled run the console's audit record, history, and
— for the event path — the full agent/confirm-gate machinery.

When a schedule fires it does one of two things, chosen by `action`:

- **`action = "event"`** — emit a rich, matchable `Event` into the trigger
  engine, exactly like a new mail arriving. Triggers with `source = "schedule"`
  pick it up by `event` type + `match` rules, so a **single** scheduled emit can
  fan out to **many** triggers, each with its own autonomy / agent-or-rule /
  match. The clock becomes just another event source, reusing the whole email
  mental model (and a scheduled *agent turn* is simply an `action="event"`
  schedule with a `mode="agent"` trigger listening for it).
- **`action = "runnable"`** — run a specific runnable on a host with fixed args,
  **directly and autonomously**: no agent, no confirm gate (scheduling it *is*
  the authorization), audited as the `Scheduled Task` operator via the normal
  `invoke_runnable` path.

The timing lives in a `when` table — exactly one of `cron = "30 17 * * mon-fri"`
(5-field crontab) or `every = "15m"` (interval, `<n>` + `s`/`m`/`h`/`d`), plus an
optional `timezone` (IANA name; default PC-local).

```toml
# action = "event" — fan out to triggers
[[schedule]]
id = "nightly-report"
action = "event"
event = "nightly_report"
when = { cron = "0 2 * * *", timezone = "Europe/London" }
fields = { scope = "prod", report = "billing" }

# action = "runnable" — direct fixed run, no trigger needed
[[schedule]]
id = "log-rotate-weekly"
action = "runnable"
host = "prod-1"
runnable = "log-rotate"
when = { cron = "0 3 * * 0" }
args = { keep = "7" }
```

And the trigger that reacts to the event is an ordinary trigger — no schedule
knowledge, just a `match` like any mail trigger:

```toml
[[triggers]]
name = "billing-report-agent"
source = "schedule"
event = "nightly_report"
mode = "agent"
[triggers.match]
report = "billing"
```

The `event` payload carries the user's `fields` plus built-ins the trigger can
match or template on: `schedule` (id), `event`, `fired_at`, and
`date`/`time`/`weekday`/`dow`/`hour`/`minute`/`day`/`month` (PC-local).

The engine is **APScheduler** (`BackgroundScheduler`), shipped as the optional
`schedule` extra (`pip install runspec-console[schedule]`); the source reports
`(False, …)` from `available()` until it's installed. Like the chat source, it is
**bridge-owned and marked external** (`external_sources={"schedule"}`): the
engine doesn't start it (the zero-arg registry factory can't know the schedule
list); the bridge builds it from `get_schedules()` in `start_triggers`, wires
`run_runnable=self._run_scheduled` for the direct path, feeds event-action fires
in via `engine.feed`, and stops it in `stop_triggers`. Adding/removing a schedule
restarts the engine so the change takes effect live. It is still
`register_source`'d so the Triggers UI lists "schedule" in the source dropdown,
and `list_triggers` advertises the event names schedules actually emit for the
trigger Event-field autocomplete.

**Known limitation (v1):** forward-only, no persistence — a fire that would land
while the console is closed is skipped (the default in-memory jobstore). A SQLite
jobstore is a clean later drop-in for missed-run catch-up.

---

## 5. Trigger contract (TOML, symmetric to a runnable's args)

Triggers are **console-side config** (they bind across discovered runnables and
event sources), stored as a `[[triggers]]` array alongside hosts and filters in
the console config — not in any package's `runspec.toml`.

```toml
[[triggers]]
name = "vip-outage-triage"
enabled = true
source = "outlook"          # EventSource.id
event  = "new_mail"         # one of source.event_types()
autonomy = "confirm"        # gate level for runnables this trigger drives (default: confirm)
post_to_chat = false        # (A2) also surface this trigger's run in the main chat tab
once_per_thread = true      # fire on the first message in a conversation, ignore replies
                            # (omit to inherit the [mailbox] default)

# Cheap declarative pre-filter on the payload (regex). No LLM, fully auditable.
# Works for any event type — especially useful for `reminder`, which fires for
# every upcoming item (A4); a couple of regexes keep it to what matters.
[triggers.match]
from    = ".*@bigcustomer\\.com"
subject = "(?i)outage|down|urgent"

# Routing: how the event becomes an action. Two modes.
mode = "agent"              # "agent" = LLM-in-the-loop | "rule" = direct runnable

# agent mode: the rules the LLM follows for THIS trigger (its system prompt).
rules = """
If the email reports an outage, identify the affected host and run the
diagnostics runnable against it, then draft (do not send) a reply summarising
findings. Never send mail or restart a service without confirmation.
"""

# agent mode (optional): scope the agent to an allowlist of runnables. Omit it and
# the agent sees every discovered runnable; set it and the LLM still extracts and
# decides, but only over these tools. Each entry matches a runnable's bare name or
# its qualified "host__name" (a bare name allows it on every host).
runnables = ["prod-web-01__service-status", "check-memory"]

# (A5) Suggested reply — off by default. When on, an agent-mode trigger may use
# the built-in `draft_reply` tool to save a short draft reply to Outlook Drafts
# for the user to review and send. The console never sends mail itself.
[triggers.reply]
draft = true               # enable the draft_reply tool for this trigger
style = "Concise, professional. Acknowledge, state next step, no commitments."

# ── rule mode alternative (deterministic, no LLM) ──
# mode = "rule"
# run  = "prod-web-01__service-status"     # host__runnable
# [triggers.map]                            # payload field → runnable arg (VALIDATED)
# service = "{{ subject }}"
```

- `match` is the cheap gate (regex per payload field, all must match). Keeps the
  LLM out of the 99% of events that aren't relevant. Applies to every event type,
  and is the recommended knob for taming a noisy `reminder` source (A4).
- `mode = "rule"` maps payload → args via the **existing validator** (the
  `map` table), then runs one runnable through the gate. Fully deterministic.
- `mode = "agent"` runs a headless agent turn seeded with `rules` + a rendered
  summary of the event, with `_runnables_to_tools()` as its toolset. The optional
  `runnables` allowlist is the **third point on the rule↔agent spectrum**: `rule`
  runs one fixed runnable (no LLM), `agent` lets the LLM pick from *every*
  runnable, and `agent` + `runnables` lets the LLM extract/decide but only over a
  curated set. Empty/omitted = every runnable; an allowlist that matches nothing
  logs a warning (a typo or a not-yet-discovered host) and the agent runs
  tool-less rather than silently picking from all. Invalid in `rule` mode (which
  already names its single runnable via `run`).
- `autonomy` sets the gate level for runnables this trigger drives. **Defaults to
  `confirm`** — safe by default, exactly like runspec everywhere else. The
  per-runnable / per-arg escalation in `_effective_tool_autonomy` still applies
  (most restrictive wins), so a `manual` runnable is still refused to the agent.
- `post_to_chat` (default `false`) mirrors the trigger's run into the main chat
  tab for visibility; otherwise the run lives only on its run card (§6, A2).
- `once_per_thread` suppresses replies: the trigger fires for the **first** matching
  event in a conversation, then skips later messages in the same thread — so an
  email back-and-forth doesn't re-run the rule on every reply. Keyed on the event's
  thread id (`conversation_id` / `thread_id`); **fail-open** when the payload carries
  no thread id (it fires, since we can't tell it's a reply). The dedup is **durable**
  (a small JSON state file), so a reply after a console restart is still recognised.
  It's **tri-state**, resolving like `autonomy`: a per-trigger `once_per_thread`
  overrides the mailbox-wide default (`[mailbox] once_per_thread`), which overrides
  the library default (`false`). Omit it on the trigger to inherit the mailbox
  default; set `true`/`false` to override for that one trigger.
- `[triggers.reply] draft` (default `false`) turns on the built-in `draft_reply`
  tool. The draft is **saved, never sent** — the human reviews and sends (A5).

### 5.1 Richer actions (0.111.0)

A trigger's action vocabulary grew, while staying under the §1 "no workflow
engine" cliff (one fire → one card → a flat action list, optionally one LLM
pre-turn; no branching, no step-result piping, no cross-restart state).

**Built-in agent action tools.** An agent-mode turn already saw every runnable
plus the file-transfer / memory / Outlook tools; it now also gets four built-in
**action** tools, all **confirm-gated** (`_ACTION_TOOL_NAMES` in `bridge.py`):

| Tool | Effect |
|---|---|
| `create_document` | Write markdown to a local file, and optionally upload it to a fleet host (reuses `start_transfer`). |
| `run_skill` | Render a saved runbook's steps **inline** and return them for the agent to follow with its other (gated) tools — *not* a spawned sub-agent. |
| `create_schedule` | Add a cron/interval schedule (validated via `parse_schedule_entry`, then `create_schedule`). |
| `emit_event` | Feed a matchable `Event` into the engine now (fan-out to `source="schedule"` triggers). Bounded: it does not wait on or branch over their results. |

**`action_allow`** (agent mode) scopes which built-in action / Outlook tools the
trigger may use — a parallel to the `runnables` allowlist, applied in
`_run_isolated_agent_turn`. Empty = all available.

**Declarative multi-runnable rule actions.** A `mode = "rule"` trigger may name
**several** runnables instead of one, via an `actions` array-of-tables and an
`action_mode` (`sequential` | `parallel`). Mutually exclusive with `run`; capped
at 10; `actions` is rejected in agent mode.

```toml
[triggers.nightly]
source = "schedule"
event  = "nightly_report"
mode   = "rule"
action_mode = "parallel"          # or "sequential"
pre_turn = "Find the affected host and severity."   # optional LLM extract step
[[triggers.nightly.actions]]
run = "web01__check-memory"
[triggers.nightly.actions.map]
threshold = "{{ _llm.level }}"    # {{ _llm.* }} = the pre-turn's output
[[triggers.nightly.actions]]
run = "db01__check-disk"
```

- **Sequential** runs each step in order through the normal per-runnable gate.
  Each step's args come only from the event payload (+ the optional pre-turn) —
  never a prior step's output. That boundary is what keeps it declarative.
- **Parallel** runs the batch behind a **single grouped confirm** (`run_group`):
  the group's autonomy is the most restrictive across the steps; a `manual` step
  refuses the whole group; on approval each step runs without re-gating.

**Optional LLM pre-turn** (`pre_turn`, rule mode): one headless model call with a
single `emit_params` output tool and **no runnable tools**, so it can only extract
— it cannot act, loop, or choose steps. Its `{param: value}` result is merged into
the payload under `_llm` for the action arg maps (`{{ _llm.<name> }}`).

**Editable confirmations (global).** At the autonomy gate the operator may edit a
call's args before approving; the edited input runs instead of the agent's
proposal. `resolve_tool_confirmation(request_id, approved, edited_input=None)` →
`_finish_confirm` returns `(approved, edited_input)`, threaded into the post-gate
execution in `_gated_run_tool_async` / `_gated_run_tool` / the reply gates /
`_run_rule_group`.

---

## 6. Headless agent turn (trigger-driven)

`mode = "agent"` reuses the `_agentic_chat_turn` machinery with three changes:

1. **No human message** — the "user" turn is a rendered event summary plus the
   trigger's `rules`. The rules go in the system prompt; the event summary is the
   first user message.
2. **Isolated context (default)** — each card gets its own ephemeral history,
   *not* the main chat's `_chat_history`, so a trigger firing can't pollute (or
   be polluted by) what the user is doing in the chat tab. Per-trigger
   `post_to_chat = true` (A2) opts a trigger into *also* mirroring its run into
   the main chat for visibility; the working context stays isolated either way.
3. **Card-scoped `chat_id`** — the turn runs under the card's id so its
   `runspec:tool_start` / `runspec:tool_confirm` / `runspec:run_end` events are
   attributable to the card in the run inbox. The autonomy gate's `request_id`
   mechanism is untouched; we just tag the card.

This is a small refactor: extract the body of `send_chat`'s `run()` into a
helper that takes `(history, system_prompt, chat_id)` so both the human path and
the trigger path call it.

---

## 7. Run inbox

The lifecycle statuses below drive the run inbox, and the human status **is** a
parked gate:

```
Triggered → Triage (agent thinking) → Awaiting human → Running → Done / Failed
```

- A run card is one `Event` instance.
- "Awaiting human" = a pending `_register_confirm` (`runspec:tool_confirm`). The
  user approving the card's gate is the existing `resolve_tool_confirmation`.
- New UI events on the existing `_dispatch` channel:
  - `runspec:trigger_card` — `{card_id, trigger, source, summary, column, detail}`
    (create / move / annotate)
  - `runspec:trigger_board` — initial snapshot on view mount
  - existing `runspec:tool_confirm` / `runspec:tool_start` / `runspec:run_end`
    reused, tagged with `card_id`.
- New `js_api` methods on the bridge: `list_triggers`, `save_trigger`,
  `set_trigger_enabled`, `get_trigger_board`, plus a card action to dismiss /
  retry. (Frontend run-inbox view is a separate step — see §9.)

---

## 8. Safety, and known limitations

**Safety.** "Give the LLM everything I can do" includes authenticated Outlook,
fleet SSH, and the local filesystem in one agent. The mitigations are the ones
already in the codebase, kept on by default:
- gate defaults to `confirm`; trigger `autonomy` cannot silently downgrade a
  `manual` runnable;
- per-trigger `enabled` flag is a kill switch;
- `match` keeps the LLM out of irrelevant events entirely;
- every trigger run flows through the existing runspec audit log.

I recommend keeping Outlook/notification-driven triggers at `confirm` (not
`autonomous`) until the loop is trusted in practice.

**Known limitations (v1, acceptable):**
- **No persistence across restarts.** Sources are live subscriptions; an event
  that arrives while the console is closed is missed. Cards are in-memory. (A
  durable queue is a later question — see §10.)
- **No debounce/rate-limit** beyond `match` in v1. A burst (e.g. a mailing list)
  could create many cards. Simple per-trigger debounce is a fast follow.
- **Windows only.** The `EventSource` abstraction is cross-platform-ready, but no
  Linux/macOS sources ship in v1.

### 8.1 Queued — tag trigger-sent mail by category (the loop guard)

**Idea.** Stamp every reply the console sends from a trigger with an Outlook
**category** (e.g. `runspec`), and have `OutlookSource` skip any item that carries
it. The `categories` field is already in the `new_mail` payload, so the source
already sees it.

**What it buys us.** Two things:
1. **A hard loop guard.** Today self-send loops are prevented only by the *default*
   folder layout (sent mail → Sent, you watch the Inbox) and, for a `once_per_thread`
   trigger, by the thread already being marked. The category skip closes the
   remaining gap *regardless of folder config* — even if a trigger watches the Sent
   folder, or an Outlook rule copies the reply back into the watched folder, the
   marker stops it re-firing. It's the protection for a deliberately
   **"Every message"** (`once_per_thread = false`) trigger, which by design reacts to
   each reply and so can't lean on thread-dedup.
2. **Provenance.** A durable "this mail was automated" tag is useful on its own —
   operators (and Outlook rules / searches) can tell a trigger-drafted reply from a
   hand-written one.

**Why it isn't in the `once_per_thread` change.** It's **Windows/Outlook-COM only**
(stamping the category on the reply object in `save_reply_draft()`/`send_draft()`,
and the skip in `OutlookSource`), so unlike `once_per_thread` it can't be exercised
in the cross-platform unit suite — only the `windows-smoke` CI job and a real Outlook
box can confirm the tag rides onto the *sent* item and that the skip sees it. Keeping
it out of the well-tested routing change preserves that confidence. Two details to
settle against real Outlook when we build it: the category **name** (and whether it's
configurable, to avoid colliding with a user's existing category taxonomy), and that
the tag **persists** onto the sent copy. Build it alongside the next Outlook-validated
change.

---

## 9. v1 implementation plan (bounded, in order)

1. **`events.py`** ✅ *done* — `Event`, `EventSource` protocol, registry
   (`register_source`/`create_source`/`available_sources`), and a `FakeSource`
   for tests. (Note the **forward-only** contract from §4.1 / A1.)
2. **`sources/outlook.py`** ✅ *done* — `OutlookSource`: COM pump thread,
   `NewMailEx` + `Reminder` → `emit(Event)`, **forward-only** (no startup backlog
   scan, A1). `available()` guards non-Windows / Outlook-absent. Pure payload
   extractors unit-tested with fakes. Also `save_reply_draft()` (the draft_reply
   COM writer).
3. **`triggers.py`** ✅ *done* — `Trigger` model (load from config, incl.
   `post_to_chat` and `[triggers.reply]`), `match` evaluation, payload→arg
   mapping, `TriggerEngine` (start/stop shared sources, route events to cards,
   **serialised per-trigger** via an injectable executor — A3), `TriggerCard`.
4. **Bridge wiring** ✅ *done* — `Bridge` owns a `TriggerEngine`; a **separate**
   isolated headless turn (`_run_isolated_agent_turn`, *not* a refactor of the
   shared `_agentic_chat_turn`); `js_api` methods + `runspec:trigger_*` dispatch;
   card-scoped `chat_id`; `post_to_chat` (A2); the built-in **`draft_reply`**
   tool (A5) handled inline in the isolated turn, confirm-gated, **save-only**
   (the human sends). Brains live in the tested `trigger_runner.py`.
5. **Config** ✅ *done* — `[[triggers]]` load/save in `config.py` (array-of-tables
   serialiser, tomllib round-trip tested), in `runspec_triggers.toml`.
6. **Tests** ✅ *done* — `match`, payload→arg mapping, engine routing +
   per-trigger serialisation with `FakeSource`, `post_to_chat`, `draft_reply`
   schema/gating, config round-trip. All platform-independent (COM mocked).
7. **console-ui run-inbox view** — *next*. The run inbox, the per-event behaviour
   tickboxes (`post_to_chat`, reply-draft), and an easy `match` editor for taming
   `reminder` (A2/A4). Backend contract is green and ready.

**Source #2 (Notification Listener)** ✅ *done* — `sources/notifications.py` +
the `winsdk` dep (`notifications` extra) + consent check + `register_source`. No
engine/gate/run-inbox changes were needed, exactly as predicted.

---

## 10. Open questions

1. **Persistence:** should cards / missed events survive a restart? (v1: no.
   If yes later, a small SQLite-backed queue at `config_path().parent`.)
2. **Agent context isolation:** confirmed isolated per-card (§6)? Or should a
   trigger be able to *post into* the main chat for visibility?
3. **Concurrency:** how many trigger agent turns may run at once? (Propose a
   small worker pool; serialise per-trigger to keep ordering sane.)
4. **Reminder source value:** `Reminder` fires for *every* upcoming item — is
   that too noisy without a `match` on organizer/subject? (Likely needs a default
   `match`.)
5. **Built-in tools:** do agent-mode triggers need a "draft email reply" custom
   tool (via Outlook COM) for the canonical scenario, or is reading + running
   fleet runnables enough for v1?

---

## 11. Resolved decisions (from the answers below)

1. **Persistence — no, and the design leans on that.** No durable queue in v1
   (or likely later). The apps keep their own notifications across restarts, so
   nothing is lost; the console just won't re-react to messages that arrived
   while it was closed. This makes **forward-only sources** a hard contract
   (§4.1): `start()` subscribes to future occurrences and never replays backlog.
2. **`post_to_chat` is configurable per trigger** (default `false`). Working
   context stays isolated; opting in mirrors the run into the main chat. The
   run-inbox UI will expose this (and reply-draft) as per-event tickboxes.
3. **Concurrency — small worker pool, serialised per trigger** (my recommended
   default), made configurable once there's usage experience.
4. **`reminder` noise — handled by `match`, not a forced default.** `match`
   applies to reminders like any event; the UI gets an easy `match` editor so
   narrowing a chatty reminder source is a couple of regexes.
5. **`draft_reply` is in for v1 — and it's a headline feature.** ✅ *implemented.*
   Off by default, enabled per trigger via `[triggers.reply] draft = true`. The
   agent writes a short reply and **saves it to Drafts; the console never sends.**
   The human reviews the suggested response and sends it — exactly the "check and
   send" loop from the answer. The tool is offered only when the trigger enables
   it *and* the event carries a message to reply to (an Outlook `entry_id`), and
   every draft save is confirm-gated.

## Answers
1. No not in version 1 and maybe not in later versions either, the notifications from the apps should remain visible betwee restarts, we don't clear them right? So they are not lost, we just won't react to old messages which seems like a good ide to me right now, usage may change my mind.
2. I would like to explore the ability to specify that in the toml settings. Default is no but the ability to configure a certain event to post will be great, maybe the ui also allows tick boxes to change event behaviour too.
3. Go with your recommendation here. Option to change it after some usage experience.
4. If the reminders are too much then the user could reduce his reminders maybe but open to a first version match, just needs to be easily configured maybe in a ui area so it's easy to match properly
5. Being able to draft a response to an event sounds exceptional, that really becomes a very powerful selling point, suggested responses could be a setting in the toml to turn on, give a short summary response I can check and send seems awesome!
