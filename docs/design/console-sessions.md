# Console sessions — concurrent conversations, one agent-turn engine

## Why

Until 0.275.0 the Console tab was one rolling conversation. The backend kept
its state as singletons on the Bridge (`_chat_history`, `_history_turn_ids`,
`_excluded_turns`, `_conversation_id`, `_chat_lock`), and the interactive turn
held `_chat_lock` for its whole duration. Trigger-fired agent turns were already
isolated (own thread, own history, own one-off adapter), but the UI mirrored
every fire into the same transcript as a live block, and the composer decided
"a turn is in flight" by finding the first unfinished chat block. What the
operator hit:

- A running (or confirm-parked) trigger locked the composer with "Agent is
  working — press Stop to interrupt", and Stop targeted the trigger.
- "New chat" wiped every block immediately (a streaming one too) while the
  backend `clear_chat` blocked on the lock until the in-flight turn ended.
- Trigger cards showed no tokens / cache / cost, wrote no `run_summary` line
  and no turn metric — the headless loop never emitted any of it.
- A rule-mode trigger run spilled large output into the operator's chat
  workspace; scheduled agent turns streamed nowhere visible; `stop_triggers`
  never aborted a scheduled agent turn.
- Two ~500-line agent loops (`_agentic_chat_turn`, `_run_isolated_agent_turn`)
  had drifted — the headless one lacked auto-continuation, the rich
  `tool_end`, `run_end.stopped`, and a `finally` (a raise stranded the spinner).

## The model

**Every conversation is a session** (`runspec_console/session.py`,
`ChatSession`):

| | Interactive (`kind = "chat"`) | Automation (`kind = "trigger" | "schedule"`) |
|---|---|---|
| Opened by | the operator (New chat, first send, History resume) | the first sighting of a run card (`_sync_card_session`) |
| Id | **== its saved-conversation id** (so `workspace_dir(id)` and the transcript file share it) | **== the card id == the headless turn's `chat_id`** |
| Workspace | `{id}` | `trigger-{id}` / `schedule-{id}` (the prefix drives the sweep's retention rule) |
| History | rolling, trimmed, persisted per turn | one turn, never saved |
| Sends | yes (`send_chat(…, session_id)`) | **read-only** — refused with a visible error |
| Ends | `close_chat_session` (Stop + drop) | the card's terminal column; the newest 12 finished are kept, older evicted |

Sessions run concurrently: each owns the lock its turn holds, so a turn on one
never blocks a send, a clear or a compact on another. The registry is
in-memory (`Bridge._sessions`, `_chat_session_id` = the interactive session
sends / operator runs / the workspace fallback target, `_turn_sessions` =
live turn → session), guarded by `Bridge._lock`, never a session's own lock.
**Nothing survives a restart** — saved conversations stay browsable via History
and resume into a new session.

Every trigger and schedule fire is a session, agent-mode or not: rule-mode
trigger runs and scheduled runnable / saved-run / eval fires get one too (their
card is the session; a scheduled eval now gets a card so it shows). The
`event` schedule action emits an event and has no session of its own — the
trigger it fires does.

## One engine

`bridge_turn.py` — `TurnMixin._run_agent_turn(TurnSpec) -> TurnResult`. The
callers build a `TurnSpec` and differ only there:

| | chat (`_spawn_chat_turn`) | headless (`_trigger_run_agent_turn`) |
|---|---|---|
| `session` | the target interactive session | the card's automation session |
| `tools` | `_runnables_to_tools()` | allowlist + `inline_builtins` + `_filter_headless_tools` + draft schemas |
| `interactive` | True → persist, trim, reply cue, errors swallowed after reporting | False → `record`/`stage` card hooks, summary string, errors re-raised so the card fails |
| `headless_confirm` | False (chat timeout) | True (the unattended timeout) — explicit, no longer inferred from `stage` |
| `inline_tools` | — | `draft_reply` / `propose_reply` closures |
| hints | `say "continue"` | `re-run to continue` |

What every kind of turn now gets, from the shared `_finish_turn`: the OTel turn
span + `record_turn` metric, the History/Analytics `run_summary` line
(`_audit_chat_turn`, exit 130 stopped / 1 error), `runspec:run_end {…, session,
stopped}`, `runspec:chat_usage {…, session}`, `runspec:context_estimate {…,
session}`, and the `runspec:session` lifecycle event. Headless turns also gained
output-ceiling auto-continuation and the `bool(tool_calls)` rule (they used to
end on a literal `stop_reason != "tool_use"`).

## Events + JS API

- `runspec:session` — `{id, kind, label, interactive, created_at, state, turn,
  turns}` on create / turn start (`running`, `turn` = chat id) / turn end
  (`idle` chat, `done` automation) / close; an eviction arrives as
  `{id, state: "closed"}`.
- `new_chat_session()`, `list_chat_sessions()`, `select_chat_session(id)`
  (interactive only), `close_chat_session(id)`; `send_chat` / `clear_chat` /
  `compact_chat` / `run_runbook` take an optional trailing `session_id`;
  `resume_conversation(id)` returns `session` (and `already_open` when that
  conversation is already a live session — the UI just selects it).
- `clear_chat` replaces the session with a fresh one (stopping an in-flight
  turn first, never waiting on it); `compact_chat` refuses while a turn is in
  flight instead of blocking.

## The UI

A **chip row** beside *New chat* (`SessionStrip`, left slot of the Console
toolbar) shows every session: kind icon, label, running spinner, an amber bell
when a confirm is waiting for the operator, an unread dot for activity in a
session you are not looking at, and a dismiss `×` on finished ones. The
transcript shows the selected session's blocks only (`blocks.filter(sessionId)`),
so the activity indicator, the pending confirm gates and the composer's
Stop/Send are per session by construction. *New chat* opens an additional
session; the previous one keeps running. Selecting an automation session
shows its transcript read-only ("Automation session — switch to a chat to
talk"). The context gauge tracks the selected session.

## Deliberately not in this change

- **Continuing an automation session** (typing a follow-up into a trigger's
  conversation under its rules/allowlist) — sessions are read-only for now.
- **Restoring sessions across a restart** — by design; History + resume covers
  the interactive case.
- **A scheduled `event` action as a session** — it only emits an event.

## Files

`runspec_console/session.py`, `bridge_turn.py` (engine + registry + public
methods), `bridge_invoke.py` (chat builder, clear/compact/resume/persist per
session), `bridge_triggers.py` (headless builder, `_sync_card_session`,
`stop_triggers`), `bridge_schedules.py` (eval card), `bridge_autonomy.py` /
`bridge_agent_tools.py` (workspace via session), `tests/session_stub.py`,
`tests/test_session.py`, `tests/test_sessions_bridge.py`;
`console-ui/src/sessions.ts`, `useSessions.ts`, `components/SessionStrip.tsx`,
`views/ConsoleView.tsx`, `App.tsx`, `components/CommandInput.tsx`.
