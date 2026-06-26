# Design: Trigger actions UX — beyond email/reply

**Status:** ✅ Implemented (runspec-console 0.111.0) — and extended well beyond the
original UI-only scope (see "What shipped" below). **Scope:** the **Triggers**
settings form in `runspec-console` (`packages/console-ui/src/components/TriggersEditor.tsx`
— the editor was extracted out of `SettingsDrawer.tsx`). Goal: make the trigger
*action* fields read sensibly for **all** event sources — `outlook` (mail),
`schedule` (emitted events), and the `chat`/notification/reminder sources — not
just email replies.

---

## What shipped (0.111.0)

The UI generalisation below landed, plus a larger "richer actions" change:

- **Source-aware fields:** reply drafting + once-per-thread show only for threaded
  sources (`outlook`/`chat`/`email`); the draft toggle relabels to "Propose a
  reply" for `chat`. "Reply style" became the full-width **"Actions on results"**
  field (still `reply.style` on disk — option (a)).
- **Declarative multi-runnable rule actions:** a rule trigger can run several
  runnables `sequential` or `parallel` (one grouped confirm for parallel), with an
  optional **LLM pre-turn** (`pre_turn`) filling the action args via `{{ _llm.* }}`.
- **Built-in action tools** in agent mode: `create_document`, `run_runbook`,
  `create_schedule`, `emit_event`, and the Outlook COM tools — scoped per trigger
  by a new **action allowlist** (`action_allow`).
- **Editable confirmations (global):** the operator can edit a gated call's args
  before approving.

See `docs/design/console-triggers.md` §5 for the trigger contract and the cliff
guards that keep the multi-runnable list bounded. The rest of this doc is the
original problem statement / rationale.

See `docs/design/console-triggers.md` for the trigger model and engine. The
runtime behaviour referenced here lives in
`packages/python/runspec-console/runspec_console/trigger_runner.py`
(`make_handler`, `compose_system`) and `runspec_console/triggers.py` (`Trigger`).

---

## 1. The problem

A trigger binds an event (`source` + `event`, narrowed by a regex `match`) to an
**action**: `mode = "rule"` (run one fixed runnable) or `mode = "agent"` (a
headless LLM turn). The form's action fields, however, are written for the
original email use case and don't generalise:

| Current field (label) | Help / intent today | Why it misreads elsewhere |
|---|---|---|
| **Draft a reply** | `Offer a draft_reply tool (email triggers).` | A `schedule`/reminder event has nothing to reply to. |
| **Reply style (optional)** | placeholder `formal, concise…` | Only meaningful when drafting a reply; the input is also cramped (half-width grid cell). |
| **Replies** (`once_per_thread`) | `Fire on the first message… then ignore replies in that thread…` | "Thread" is an email/chat notion; a cron tick has no thread. |
| **Agent instructions** | `Triage the email and draft a reset link…` | Placeholder is email-flavoured. |

The user's framing: a scheduled **emitted event** should be able to use an LLM
turn to **run several runnables and act on their combined results**; a **rule**
trigger runs one runnable and acts on that one result. Replying is just *one*
optional thing an email agent might do — not the headline of the whole form.

---

## 2. The action model (what the backend already does)

This is already true in `make_handler`; the UI just doesn't present it this way.

- **Rule mode** (`trigger_runner.py` rule branch): renders `arg_map` from the
  event payload and calls `run_rule_tool(card.id, run, args)` — **one** runnable,
  through the autonomy gate. The "action on the result" today is implicit (the run
  output lands on the run card; `post_to_chat` mirrors it).
- **Agent mode**: calls `run_agent_turn(...)` — a headless LLM turn that may call
  **multiple** runnable tools, scoped by the optional `runnables` allowlist, and
  reason over their outputs. `reply.draft` merely *adds one more tool*
  (`draft_reply` for mail, `propose_reply` for chat) and a style note via
  `compose_system`.

So the rule↔agent spectrum is really: **rule** (one fixed runnable) → **agent +
allowlist** (LLM picks from a fixed set) → **agent** (LLM picks from all). Reply
drafting is an email/chat-only *add-on*, orthogonal to that spectrum.

---

## 3. Field applicability (source × mode)

The aim is to show a field only where it means something.

| Field | rule | agent | outlook | schedule | chat |
|---|---|---|---|---|---|
| `match` (payload regex) | ✓ | ✓ | ✓ | ✓ | ✓ |
| `run` + `arg_map` (one runnable) | ✓ | — | ✓ | ✓ | ✓ |
| `rules` (agent instructions) | — | ✓ | ✓ | ✓ | ✓ |
| `runnables` (allowlist) | — | ✓ | ✓ | ✓ | ✓ |
| Draft reply + style | — | ✓ | ✓ | — | ✓ (propose) |
| `once_per_thread` | ✓ | ✓ | ✓ | — | ✓ |

Email/chat-only fields (draft reply, reply style, once-per-thread) should be
**conditional on the selected source**, not always shown.

---

## 4. Proposed UX

1. **Reframe the section around "what happens", per mode** — keep the
   Agent/Rule segmented control as the primary switch, then show only that mode's
   fields (the form already does this for `run`/`map` vs `rules`/`runnables`).

2. **Generalise "Reply style (optional)".** Rename toward **"Actions on results"**
   — a full-width multiline input describing what the agent should *do* with what
   the runnables return (e.g. "summarise the failures and open a ticket if any are
   critical"; for mail, "draft a concise reply"). This subsumes reply-style for
   the email case and gives schedule/chat agents a natural home. Widen it from the
   current half-width grid cell to full width with `TextArea` autosize.

3. **Make reply-drafting an email/chat-only toggle.** Show "Draft a reply"
   (mail) / "Propose a reply" (chat) only when `source` is `outlook`/`chat`; hide
   it entirely for `schedule`. Its style hint folds into "Actions on results".

4. **Gate `once_per_thread` on threaded sources.** Show "Replies" only for
   `outlook`/`chat`; hide for `schedule` (a tick has no thread).

5. **Source-aware placeholders.** Swap the email-flavoured `rules`/`event`
   placeholders for source-appropriate examples (e.g. for `schedule`: event
   `nightly_report`, instructions "check each service and report anomalies").

> The Settings editors now open in a modal (see the modal-refactor change that
> ships alongside this doc), which gives this richer, taller form room to breathe.

---

## 5. Contract impact

Likely **UI-only** for a first pass: the backend already supports multi-runnable
agent turns and an allowlist, and `reply.style` can carry the "actions on results"
text unchanged (it's a free-form string fed into `compose_system`). Options to
decide when implementing:

- **(a) No model change** — reuse `reply.style` as the "actions on results" field
  and just relabel/condition the UI. Cheapest; slightly odd that the key is still
  `reply.style` on disk.
- **(b) Rename the field** — add a clearer key (e.g. `[triggers] instructions` or
  fold into `rules`) and migrate, keeping `reply.style` as a back-compat alias.

Recommend starting with (a) for the UI rework, and considering (b) only if the
on-disk naming becomes confusing.

---

## 6. Open questions

- Should "Actions on results" be distinct from "Agent instructions" (`rules`), or
  are they the same field? (Leaning: same field — `rules` *is* the instruction;
  "Actions on results" may just be a better label + placeholder for it, retiring
  the separate reply-style input.)
- For **rule** mode, is there an "act on the result" step to expose at all, or is
  the runnable's own effect the action (status quo)? A rule trigger that chains a
  second runnable would be a real model change — likely out of scope.
- Do reminders/notifications want their own tailored fields, or do they fit the
  generic schedule-like shape?

---

## 7. Implementation sketch (for the later session)

- `TriggersEditor` (`SettingsDrawer.tsx`): drive field visibility off the watched
  `source` (already watched) in addition to `mode`; relabel/relocate the
  reply-style input as a full-width "Actions on results" `TextArea`; hide
  draft/once-per-thread for non-threaded sources; source-aware placeholders.
- `triggerToForm`/`formToTrigger`: unchanged under option (a); under option (b),
  map the new key with a `reply.style` fallback.
- `docs/design/console-triggers.md` §5 (trigger contract) + the format docs:
  update field descriptions to match.
- Tests: extend the console-ui suite to assert the right fields render per
  source/mode.
