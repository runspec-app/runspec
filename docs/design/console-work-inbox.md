# Design: unified work inbox — one list of things needing you

**Status:** v1 built (`runspec-console` 0.48.0) — slices 1–3 of §11 done; the
optional backend aggregator (§4/§11.4) + notification consolidation (§10) and
in-tab focus-on-open remain. Picks up the "unified work inbox"
direction parked in `console-room-next-session.md` (the user's idea: *"one list
of items needing you"* spanning trigger cards + helpdesk threads). The 0.46.0
attention layer (tab badges, toasts, unread dots) and the 0.47.0 desktop
notifications were the first steps; this note designs the surface they were
leading toward.

**Scope:** A console-side *aggregating view* over attention signals that already
exist. It introduces **no new data model, no new store, and no new lifecycle** —
it indexes the items the Triggers, Rooms/helpdesk, and chat confirm-gate
surfaces already produce, and routes the user to them.

---

## 1. What this is, and the line it must not cross

Three places in the console can need a human *right now*:

- the **chat / agent confirm-gate** — a tool call parked on the autonomy gate
  (`runspec:tool_confirm`), waiting for Approve/Deny;
- a **Triggers** kanban card sitting in **Awaiting human** (a parked gate from a
  headless trigger turn);
- a **Rooms/helpdesk** conversation — an unclaimed thread, or an unread visitor
  message on a thread you're handling.

Today each lives on its own tab with its own badge. The user has to *poll three
tabs* to know what needs them. The unified work inbox is **one list** that
answers "what needs me?" across all three.

**The line (the same test the triggers doc applies to itself):** the inbox is a
**projection / index**, not an owner. It must not:

- become a second source of truth for thread state, card state, or gate state —
  each owning surface stays authoritative;
- **merge the data models.** A trigger card (an `Event` moving through kanban
  columns), a helpdesk conversation (a `room` with an `open→…→closed` lifecycle),
  and a confirm gate (a `request_id` + `threading.Event`) are genuinely different
  shapes. We decided in `console-room-next-session.md` **not** to merge the tabs;
  this note honours that — the inbox holds lightweight *references* with
  denormalised display fields, and clicking one routes to the owning tab;
- introduce persistence. Consistent with `console-plan.md` (*"No SQLite. No alert
  DB."*) and the triggers doc's forward-only / in-memory stance — the inbox is
  derived state, rebuilt from the live sources on mount.

If a request can only be satisfied by the inbox *owning* state (editing a thread,
re-sequencing a card's flow, persisting across restarts), it belongs in the
source surface, not here. The inbox aggregates; it does not govern.

---

## 2. The three sources and the signals they already emit

Everything the inbox needs is already on the `runspec:*` UI event channel
(`bridge._dispatch` → `window.dispatchEvent`). No source needs to change to
*populate* the inbox.

| Source | "Needs you" item | Enters on (event) | Leaves on | Cold-start snapshot |
|---|---|---|---|---|
| **Confirm-gate** (chat + trigger) | a parked tool call | `runspec:tool_confirm` `{id, request_id, tool_name, tool_input}` (`id` = the card/chat id) | `runspec:run_end {id}` / resolve / timeout | *(new)* `get_pending_confirms()` |
| **Triggers** | card in `awaiting_human` (or with a live confirm) | `runspec:trigger_card {card_id, column, summary, …}` | card moves to `running`/`done`/`failed` | `get_trigger_board()` (exists) |
| **Rooms/helpdesk** | unclaimed thread; unread message; assigned-to-me thread | `runspec:room_message` / `runspec:room_conversation {room, state, assignee}` | claimed + read / resolved / closed | `get_room_state()` + thread states (exists) |

The App already does three *partial* versions of this aggregation for the badges
(`App.tsx`): `triggerAwaiting` (lines ~212–246) folds trigger cards +
trigger-tagged confirms; `roomAttention` (lines ~201–206) reads
`runspec:room_attention`; the Console tab shows `inFlight`. The work inbox
**generalises and unifies** that badge logic into a first-class list — the badge
counts become "rows you can act on."

---

## 3. Core idea: a `WorkItem` projection

Define one normalised shape the three sources project *into*. Each owning surface
(or a thin adapter over its existing events) contributes `WorkItem`s; the inbox
renders the merged, ordered list and dispatches actions back to the owners.

```ts
type WorkKind = 'confirm' | 'trigger' | 'thread'

interface WorkItem {
  id: string            // stable, source-namespaced: `confirm:<request_id>`,
                        // `trigger:<card_id>`, `thread:<room>`
  kind: WorkKind
  title: string         // "Approval: restart-service" | "alice@corp · it-support" | …
  subtitle?: string     // arg summary / message preview / card summary
  urgency: 'blocking' | 'actionable' | 'fyi'   // §5
  ts: number            // for ordering within a tier
  origin: {             // how to deep-link to the owning surface
    tab: 'console' | 'triggers' | 'rooms'
    select?: string     // room / card_id / chat_id to focus on arrival
  }
  actions?: WorkAction[]   // inline quick actions where cheap (§6)
  state?: string        // owner's state, for display (column / lifecycle / gate)
}
```

The key property: **the inbox stores references + denormalised display fields,
never the authoritative record.** Resolving an item is always a call into the
owning bridge method; the item then leaves the inbox when the owner emits its
terminal event. The inbox never mutates source state directly.

---

## 4. Architecture: frontend-first aggregation

Because every signal already reaches the UI as an event, the **first cut is pure
frontend** — a `useWorkInbox()` store (or context) that:

1. On mount, pulls the three cold-start snapshots (`get_trigger_board`,
   `get_room_state` + thread map, and a **new** `get_pending_confirms`) and seeds
   the list.
2. Subscribes to the live events in §2 and upserts/removes `WorkItem`s.
3. Publishes a single `runspec:work_attention {count, blocking}` the way
   `runspec:room_attention` works today — so the **one** inbox badge replaces the
   per-tab badges (or sits beside them; §9).

The store must live at the **app root** (like the existing `triggerAwaiting` /
`roomAttention` effects in `App.tsx`), *not* inside `WorkInboxView` — the
Triggers and Rooms views are deliberately mounted-always (`display:hidden` when
inactive) so their listeners and pending prompts survive tab switches; the inbox
projection needs the same lifetime so it keeps accruing while you're on another
tab. The view is just a renderer over the root-level store.

The only backend addition the first cut needs is **`get_pending_confirms()`** —
a snapshot of the live `_pending_confirms` map (`bridge.py`), so confirms that
were already parked when the inbox mounts appear (the others have getters
already). This is a read-only projection of existing state; it adds no storage.

A later, optional **backend aggregator** (`get_work_inbox()` + a
`runspec:work_item` upsert/resolve channel) would centralise the projection so
non-UI consumers (e.g. the OS-notification path from 0.47.0, or a future tray
menu) read one list. Recommendation: ship frontend-first, lift to the backend
only if a second consumer appears — don't pre-build it.

---

## 5. Urgency tiers (ordering)

The list is grouped by urgency, then newest-first within a tier:

1. **blocking** — a confirm gate. It *times out into a deny* after
   `_CONFIRM_TIMEOUT_S`, so it is genuinely time-critical and sorts first. This
   includes both chat confirms and trigger `awaiting_human` cards (which *are*
   confirm gates — see §7).
2. **actionable** — a helpdesk thread that wants a decision: **unclaimed** in a
   queue you staff (claim or leave it), or **assigned to you** with an unread
   visitor message. A `request`-kind message (a self-service Form submission)
   ranks at the top of this tier — it's a concrete ask.
3. **fyi** — unread group-chat messages, a trigger card still in `triage`
   (agent thinking, no human needed yet), informational lifecycle changes. Shown
   but not counted toward the *blocking* badge.

The badge the inbox publishes counts tiers 1–2 (things that actually need an
action); tier 3 is visible in the list but doesn't nag.

---

## 6. Actions: inline where cheap, deep-link otherwise

Each item offers the smallest useful action without leaving the inbox, plus an
"Open" that routes to the owning tab (`setView(origin.tab)` + focus
`origin.select`):

| Kind | Inline action(s) | Bridge call | Deep-link |
|---|---|---|---|
| `confirm` | Approve / Deny | `resolve_tool_confirmation(request_id, ok)` | Console tab, focus the card's `chat_id` |
| `thread` | Claim | `claim_thread(room)` | Rooms tab, select the queue + thread |
| `trigger` | (none inline v1 — its action *is* its confirm, surfaced as a `confirm` item) | — | Triggers tab, focus `card_id` |

Replying to a thread and answering a multi-field gate stay in the owning view
(the composer / the card) — the inbox routes you there. Inline actions are
limited to the one-click, irreversible-safe decisions (Approve/Deny already gated;
Claim is reversible via the lifecycle). This keeps the inbox a *dispatcher*, not
a second editor.

---

## 7. The overlap: a trigger's "awaiting human" *is* a confirm

A trigger card in `awaiting_human` is parked on the **same** `_register_confirm`
mechanism as a chat confirm. The two are told apart by the confirm event's `id`:
for a trigger turn it equals the card's `chat_id` (= the `card_id`); for a chat
turn it's the chat's id. The App badge logic already exploits exactly this — it
folds confirms whose `id` matches a known trigger `card_id` into the trigger
count and *excludes* them from the chat count (`App.tsx` ~226–229,
`cardCols.has(id)`). The inbox must reuse that key so it doesn't double-count.

**Decision:** a confirm whose `id` matches a live trigger card surfaces as a
single **`confirm`** item whose `origin` points at the Triggers tab (title names
the trigger). A confirm with no matching card is a chat confirm, `origin` =
Console. No new bookkeeping — the same `id`-matching the badge already does. A
trigger card *not* in `awaiting_human` (triage / running) is a separate
`fyi`/progress item, not a blocking one.

---

## 8. UI placement

Two options:

- **(a) A new top-level "Inbox" tab** (a `ViewKey` + `navTabs` entry, likely
  first, with the unified badge). The per-source tabs keep their own badges or
  cede them to the inbox. Clear home for "what needs me?"; one more tab.
  **Recommended** — it matches how the user framed it ("one list").
- **(b) A header drawer / popover** off a single bell in the top bar (no tab),
  overlaying any view. Lighter, always reachable, but less room for the list and
  competes with the existing per-tab badges.

Recommend **(a)** as the canonical surface, with the unified badge on the Inbox
tab; keep the per-tab badges (they still answer "which surface") but they become
secondary. The inbox is the default landing only if the user opts in (don't steal
the Console tab's default).

---

## 9. What's reused vs new

**Reused unchanged:** every source's events and bridge methods
(`runspec:tool_confirm` / `trigger_card` / `room_message` / `room_conversation`,
`get_trigger_board`, `get_room_state`, `resolve_tool_confirmation`,
`claim_thread`), the `_dispatch` channel, the badge pattern (`runspec:*_attention`),
and the 0.47.0 desktop-notification path.

**New:**
1. `useWorkInbox()` frontend store — snapshot seed + event upserts → ordered
   `WorkItem[]` + `runspec:work_attention`.
2. `WorkInboxView` — the grouped list with inline actions + deep-link routing.
3. `get_pending_confirms()` bridge getter — the one missing cold-start snapshot.
4. *(optional, later)* a backend `get_work_inbox()` + `runspec:work_item` channel
   if a second (non-UI) consumer appears.

---

## 10. Relationship to OS notifications (0.47.0)

The desktop toast + taskbar flash shipped in 0.47.0 fire per-source today
(`_notify_confirm`, `_notify_room_message`). Once the inbox exists, the natural
consolidation is to let the **inbox aggregator be the single place that decides
"this is new and needs you, and the window isn't focused → toast."** That removes
the per-source duplication and gives one consistent rule. Out of scope for the
first cut (the per-source toasts already work) — noted as the clean follow-up
once the projection exists.

---

## 11. Slice plan (bounded, in order)

1. **`get_pending_confirms()`** ✅ *done* — read-only snapshot of a parallel
   `_pending_confirm_meta` map (the tuple in `_pending_confirms` doesn't hold the
   tool name/input). Returns the `runspec:tool_confirm` shape + `created_at`,
   oldest first. Tested against a seeded gate (snapshot + cleanup on finish).
2. **`useWorkInbox()` store** ✅ *done* — the pure projection lives in
   `bridge/workInbox.ts` (reducer + `buildWorkItems` + `inboxCounts`), unit-tested
   (`workInbox.test.ts`) against the §2 matrix: each source enters/leaves, the
   trigger/confirm de-dup holds, tiers order correctly. The thin
   `bridge/useWorkInbox.ts` hook seeds from the three snapshots and folds the live
   events. (No `runspec:work_attention` event in v1 — App owns the hook and reads
   the count directly; the event is only needed once a non-App consumer exists.)
3. **`WorkInboxView` + tab** ✅ *done* — the tiered list, inline Approve/Deny +
   Claim, "Open" deep-links to the owning tab. Inbox tab + count badge in
   `App.tsx`.
4. **(later)** backend aggregator + notification consolidation (§10), and
   focus-on-open within the target tab — only when a second consumer / the polish
   lands.

---

## 12. Open questions

1. **Badge consolidation** — does the Inbox badge *replace* the per-tab Triggers/
   Rooms badges, or sit alongside them? (Lean: keep both; the per-tab badge still
   says *which surface*, the inbox badge says *how much total*.)
2. **`fyi` noise** — do unread *group-chat* messages belong in the inbox at all,
   or only helpdesk/actionable items? (Lean: include as `fyi`, off the blocking
   count, with a filter toggle.)
3. **Cold-start completeness** — confirms and threads have snapshots; trigger
   cards in `triage` are transient — is the board snapshot enough, or do we need
   the engine to replay in-flight turns? (Lean: board snapshot is enough; v1
   triggers are in-memory and forward-only anyway.)
4. **Default landing** — should a user be able to set the Inbox as the startup
   view? (Lean: opt-in setting, default stays Console.)
5. **Multi-window / tray** — is a future system-tray "N items need you" menu a
   consumer that forces the backend aggregator early? (Defer until it exists.)

---

## 13. Non-goals (the cliff, restated)

- Not a workflow engine, not a router, not a queue — it *shows* work, it doesn't
  *orchestrate* it (same boundary the triggers doc draws in its §1).
- Not a new store — no persistence; derived from live sources on mount.
- Not a merge of the Triggers / Rooms / chat data models — a reference index with
  deep-links, deliberately preserving the three owning surfaces.
- Not a second editor — inline actions are one-click safe decisions only; real
  editing (replies, multi-field gates) happens in the owning view.
