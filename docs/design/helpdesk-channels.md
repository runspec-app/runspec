# Help desk channels — one queue, many channels

> Status: **slices 1–6.2 built** (email as a queue card; mailbox claim + lifecycle;
> the ownership-gated reply, sent as the shared mailbox; the **claim-gated agent
> turn** with audit/`run_summary` parity; the agent draft sent **as the queue**;
> and **draft on claim** — claiming feeds an `email`/`claimed` trigger event so a
> one-message ticket auto-drafts). Remaining: the email conversation-pane transcript
> (Q7), losing-ack hygiene (Q6), and a real-Exchange smoke test. Builds on the
> queue-in-Triggers work in PR #217 (`console: fold helpdesk queues into the
> Triggers tab`). See also `helpdesk-rooms.md`, `console-room-server.md`,
> `console-triggers.md`.

## Status & handover (read this first)

> This section is the single source of truth for picking the email channel back
> up — it lives here, not in a PR body, so a fresh session finds it. The
> per-slice "Implemented — slice N" sections below have the detail.

**Where it stands:** slices 1–5 shipped on branch `claude/email-queue-cards-godjdh`
(PR #221 against `main`); slice 6 (the claim-gated agent turn) is on branch
`claude/helpdesk-channels-design-otyxsj`. All tests green. The email channel only
activates when a `[mailboxes]` table is configured, so it's a no-op for existing
users until then; the agent turn additionally needs a configured Outlook
agent-mode trigger (`source = "outlook"`, `event = "new_mail"`, `reply.draft =
true`) — exactly as chat needs a configured chat trigger.

**Architecture map (where each piece lives):**

- `runspec_console/mailbox_claim.py` — **pure** claim domain (the keystone):
  `Marker`, `format_marker`/`parse_marker`, `marker_from_headers`,
  `fold_conversation(events) → {state, owner}` (first-ack-wins; lifecycle only
  from the owner; requester reply after resolve/close reopens). No COM.
- `runspec_console/sources/outlook.py` — all COM, platform-guarded. `_mail_payload`
  now carries `recipients`, `received_ts`, `message_id`, `claim_marker`. New:
  `send_claim_marker` (the ack), `read_mailbox_items` (backlog scan),
  `save_reply_draft(…, from_mailbox=)`. Pure header parsers
  (`_recipients_from_headers`, `_received_ts`, `_header_field`) are fake-tested.
- `runspec_console/bridge.py` — orchestration: `_ingest_email` (dedup → append a
  fold event → refresh the card's state/owner/`owned_by_me`; retains the latest
  requester `req_subject`/`req_body`; feeds a `claimed` event on the ownership
  flip unless `replay`), `claim/resolve/reopen/close_email_thread`,
  `reply_email_thread`, `_scan_email_backlog` (replay), `_agent_identity`,
  **`_email_feed_gate`** (the claim gate for agent turns — the email analog of
  `_feed_engine_for`), **`_feed_email_event`** (feeds the synthetic `email`
  lifecycle event). State: `_email_threads`, `_email_seen`. The engine gained
  `on_source_event`, `always_start`, and **`feed_gate`** (`triggers.py`);
  `"email"` is an external source.
- UI: `packages/console-ui/src/views/TriggersView.tsx` (email cards get the queue
  chip + lifecycle buttons via `controlEmail`; reply composer in the detail drawer
  when `owned_by_me`), `bridge/index.ts` + `bridge/mock.ts`.

**⚠️ Must verify on a real Exchange box (could not be tested off-Windows):**

1. **Cross-console ack delivery** — each operator's Outlook must fire `NewMailEx`
   for the shared mailbox (monitored as a secondary store). Backlog scan covers
   cold-start; the live race window is Exchange replication latency.
2. **The exact COM calls** — `Reply()` re-addressing for the marker,
   `PS_INTERNET_HEADERS` for the `X-Runspec-Claim` header round-trip, the ack
   threading on the same `ConversationID`, `SentOnBehalfOfName`,
   `GetSharedDefaultFolder`.

**Next steps, in priority order:**

1. **Real-Exchange smoke test** of the two caveats above — the main thing blocking
   confidence in the live multi-console path.
2. ~~**Agent draft as the shared mailbox**~~ — **done** (slice 6.1). The agent turn
   now threads `from_mailbox` through `run_agent_turn` → `_gated_draft_reply` →
   `save_reply_draft`, so a help-desk email draft replies as the queue identity
   (the trigger handler resolves it via `BridgeOps.queue_mailbox_for`); the card's
   Send button reopens that already-saved draft, so the send-as is preserved.
   Personal-mail triggers and chat `propose_reply` are unaffected.
3. **Single-message tickets → make *claim* a trigger event.** The claim gate
   mirrors chat: the agent turn fires on a *requester message that arrives after
   claim*. **Done (slice 6.2):** taking ownership is now a first-class **event** the
   existing trigger config binds to — not a bespoke `draft_on_claim` flag or a UI
   button. See "Implemented — slice 6.2" below.
   - **Related gap (own design note, not built):** the trigger config has no
     *guided agent flow* — `rule` mode defines runnable+args (no LLM), `agent` mode
     defines only `rules` (the LLM sees every runnable and picks). The `runnables`
     allowlist (slice 6.x) narrows the *toolset*; pre-filling specific args, or a
     "rule → agent narrate" two-step, is a broader change to the trigger model
     (touches `console-triggers.md`), tracked separately.
4. **Losing-ack hygiene (Q6)** — mark-read / file the internal marker acks to a
   subfolder so hot threads don't accumulate visible plumbing.
5. **Thread-transcript pane (Q7)** — retain requester-message bodies per
   conversation (payloads already flow through `_ingest_email`; no new COM) and
   render them in the drawer.

**Watch-outs:**

- `_email_seen` dedups on Message-ID then EntryID — distinct mail items must have
  distinct ids (real items do; test fakes were given unique ids).
- The fold order key is `(received_ts, message_id)`; `received_ts` prefers the
  store `ReceivedTime` so header-less internal mail still orders correctly.
- `scripts/check_docs.py` only scans top-level `docs/*.md`, **not** `docs/design/`,
  so the ` ```toml ` `[mailboxes]` example here is not schema-validated.

## Implemented — slice 6.2: draft on claim (claim is a trigger event)

Solves the single-message ticket: the slice-6 gate only routes a *follow-up*
requester message, so a one-message ticket (the common email case) never auto-
drafts. Rather than a bespoke `draft_on_claim` flag or a card button, **taking
ownership is now a first-class trigger event** the existing config binds to:

```toml
[triggers.draft-on-claim]
source = "email"
event  = "claimed"
mode   = "agent"          # or "rule" — run a defined runnable on claim
rules  = "Draft a concise reply to the requester's message."
[triggers.draft-on-claim.reply]
draft = true              # offers draft_reply → autonomy gate → saved draft
```

- **The event.** When `_ingest_email` folds a claim ack that flips ownership to
  *this* console (unowned-by-us → owned-by-us, computed from `owned_by_me`), the
  bridge feeds `Event.new("email", "claimed", {queue, conversation_id, from,
  subject, entry_id, recipients=[mailbox], body})` — the same external-source
  `engine.feed` path chat uses. `_feed_email_event` reads the engine under the lock
  and feeds after releasing it (no handler-on-lock re-entrancy).
- **The retained message.** The claim ack carries no requester content, so
  `_ingest_email` now retains the latest requester message (`req_subject` /
  `req_body` on the card detail) when it sees one; the claimed event replays it as
  the agent's user turn. (A step toward the Q7 transcript, which wants bodies too.)
- **`recipients = [mailbox]`** so `BridgeOps.queue_mailbox_for` resolves the
  send-as identity and the agent drafts *as the queue* (slice 6.1), and `entry_id`
  is the requester message the `draft_reply` tool replies to — the handler treats
  a `source = "email"` event as a mail draft (`is_chat` is false).
- **Routing.** `"email"` is registered as an external source (like chat) so the
  engine never tries to *start* it; the synthetic event flows through `_route` →
  `_on_engine_event` (ignores non-`outlook`) → `_email_feed_gate` (passes non-
  `outlook`) → trigger matching. No matching trigger ⇒ a harmless no-op + audit
  record. **Opt-in by config presence.**
- **No re-draft on restart.** The startup backlog scan calls `_ingest_email(…,
  replay=True)`, which reconstructs owner/state but suppresses the claimed event —
  so a restart that re-folds an already-claimed thread doesn't re-draft. The flip
  also fires *once* (only on the unowned→owned transition), and only when there's a
  requester `entry_id` to reply to.
- **Latent fix.** A card first created by a claim ack used to seed `entry_id` from
  the ack's id; card creation now leaves `entry_id` empty until a requester message
  sets it, so the reply target is never a marker.

## Implemented — slice 6: the claim-gated agent turn (audit parity)

The "audit parity" unlock. An email ticket now runs a **claim-gated agent turn**
that drafts a reply behind the autonomy gate — and because it runs the *same*
isolated agent loop as a chat trigger, email starts emitting the same agent
audit / `run_summary` records (the History/Analytics tabs no longer go half-dark
for email queues).

The turn itself was already wired: a configured Outlook agent-mode trigger
(`source = "outlook"`, `event = "new_mail"`, `mode = "agent"`, `reply.draft =
true`) runs `_run_isolated_agent_turn` with `reply_to = entry_id`, offering the
built-in `draft_reply` tool gated by `_gated_draft_reply` (save-to-Drafts, never
sent). The piece that was **missing — and is the whole of this slice — is the
claim gate**, so the turn doesn't run on every new mail regardless of ownership
(which would burn model spend on unclaimed threads and have *both* consoles draft
a reply, breaking first-ack-wins).

- **The gate (keystone).** `bridge._email_feed_gate(event)` is the email analog of
  `_feed_engine_for`: a shared-mailbox `new_mail` routes to the trigger engine —
  and so can start an agent turn — **only once this console owns the folded
  conversation** (`detail.owned_by_me`, set by the fold in `_ingest_email`). Two
  further suppressions: a claim/lifecycle **marker ack** is internal plumbing
  (`parse_marker` hit → never a turn), and **non-queue mail** (personal / other,
  `_email_queue_for is None`) routes **ungated** so a user's own Outlook trigger
  still works. Mirrors chat exactly: group-chat (no namespace) always feeds; a
  help-desk thread feeds only when claimed.
- **The engine seam.** `TriggerEngine` gained an optional `feed_gate:
  Callable[[Event], bool]`, checked in `_route` **after** the `on_source_event`
  hook (so the queue card is folded/surfaced first) and **before** trigger
  matching. A rejected event is dropped entirely — no card, no run, **no audit
  record** — exactly like an unclaimed chat message that never reaches `feed()`.
  Fail-open: a raising gate never blocks routing. This keeps the engine
  bridge-agnostic (the gate is injected, fake-tested with a plain predicate),
  parallel to `clock_ok` / `on_source_event`.
- **Why a gate and not handler-side filtering.** The card is dispatched *before*
  the handler runs, so gating in the handler would flash a trigger card (and an
  audit record) for every unclaimed email. Gating in `_route`, upstream of
  dispatch, is the only place that achieves true chat parity (unclaimed ⇒
  invisible to the trigger audit; the *queue* card still surfaces, because the
  fold runs in the source-event hook above the gate).
- **Audit parity falls out for free.** Once an owned-thread message routes, the
  agent turn drives the same `runspec:token` / `runspec:tool_start|end` /
  `runspec:run_end` dispatches and runs runnables through `_gated_run_tool_async`,
  whose executions log `run_summary` records on their hosts — the records the
  Analytics tab aggregates. No new audit plumbing; the gap was purely that email
  turns never *started*.

Drafting the agent reply *as the shared mailbox* landed as **slice 6.1** (the
`from_mailbox` thread-through; see Next steps). Still deferred: the single-message
ticket case — the gate fires on the next requester message, mirroring chat — to be
solved by making *claim* a first-class trigger event (Next steps §3).

## Implemented — slice 5: reply as the shared mailbox (Q5)

`reply_email_thread` now sends the reply **as the queue's shared mailbox** (sets
`SentOnBehalfOfName` on the draft via a new `from_mailbox` arg to
`save_reply_draft`), so the requester sees the queue identity, not the operator.
Whether it lands as send-as ("IT Support") or send-on-behalf ("X on behalf of IT
Support") is decided by the Exchange permission on the mailbox — the COM call is
the same, so it works whichever the org grants.

## Implemented — slice 4: the forward-only catch-up (startup backlog scan)

`NewMailEx` is forward-only — it misses mail (and claim/lifecycle acks) that
arrived while the console was down, and chat gets catch-up from the hub on
reconnect but email has none (Q1). The fix reuses the fold:

- **`read_mailbox_items(mailbox_address, limit)`** (COM) — resolves the shared
  mailbox via `GetSharedDefaultFolder` (the staffer is a member) and returns the
  most recent items as `new_mail` payloads. Best-effort: `[]` off-Windows / on any
  error.
- **`_scan_email_backlog`** — on startup, for each mapped mailbox, replays those
  items through `_ingest_email`; the fold reconstructs each in-flight conversation
  and its current owner/state from the thread's markers. Runs off-thread (the COM
  read can be slow) so it never blocks startup; the Triggers tab pulls the board
  on mount, so the recovered cards surface even before the webview is up.
- **Dedup** — `_ingest_email` folds each item once (by Message-ID / EntryID), so
  the scan and the live `NewMailEx` source overlapping on an item is harmless, and
  a re-scan doesn't pile up duplicate timeline entries.

## Implemented — slice 3: the ownership-gated reply

The owner replies to the requester. Building on the claim fold, the reply is
**gated on confirmed ownership** (rule 3): only the console whose claim ack is the
earliest in the fold may message the requester, so a losing claimant never does.

- **Bridge** — `_ingest_email` now also sets `detail.owned_by_me` (does the fold's
  owner match this console's identity?). `reply_email_thread(card_id, body)`
  refuses unless `owned_by_me`, then drafts a reply to the requester's latest
  message and sends it via the existing `save_reply_draft` + `send_draft`. It's
  human-initiated (the operator typed and clicked Send), so it goes straight
  through — the autonomy gate is for *agent*-proposed replies, a later step. The
  reply is logged on the card timeline.
- **UI** — the email card's detail drawer shows a reply composer **only when this
  console owns the conversation** (and the bridge enforces the same gate
  server-side); the drawer header shows the current owner.

(Slice 5 below removes the original send-from limitation: the reply now goes out
as the shared mailbox.)

## Implemented — slice 2: mailbox claim + lifecycle (first-ack-wins)

A shared mailbox has no hub, so claim coordination rides the mailbox itself.
Claiming (or changing lifecycle) sends a tiny **marker email addressed to the
queue address itself** — never the requester — threaded on the conversation; every
staffing console sees it land and folds it. **First ack wins.** What shipped:

- **The validated fold (keystone, pure)** — `mailbox_claim.py`: a `Marker`
  (kind/queue/conversationId/agent) with format/parse + `marker_from_headers`, and
  `fold_conversation` — replay a conversation's ordered events → `{state, owner}`:
  the earliest claim-ack wins ownership; `resolve`/`reopen`/`close` count only from
  the owner; a requester reply after resolve/close reopens. Same lifecycle the chat
  side gets from its hub, reconstructed locally from the thread. Fully unit-tested,
  no COM.
- **The marker carrier** — a custom `X-Runspec-Claim` internet header, set on the
  outbound ack via the PS_INTERNET_HEADERS named property and read back from the
  transport-header block. The note's recommended path for internal same-org mail
  (Q3); invisible to humans, unlike Outlook categories (which don't even transmit
  between mailboxes).
- **Ordering** — the fold sorts by `(received_ts, message_id)`. `received_ts`
  prefers the store's `ReceivedTime` (≈ server `receivedDateTime`, present on every
  received item, including header-less internal mail), falling back to the `Date`
  header; `message_id` is the same-instant tiebreak (Q's rule 1).
- **Bridge wiring** — `_ingest_email` records each new_mail (requester message or
  marker ack) into a per-conversation event list and refreshes the card's
  state/owner from the fold; a marker ack is internal plumbing (never a summary
  bump). `claim/resolve/reopen/close_email_thread(card_id)` send the marker email
  via `send_claim_marker` (COM: `Reply()` to stay threaded, re-addressed to the
  mailbox, header set, send). Ownership is **never** set optimistically — it
  changes only when the ack lands back and folds (read-after-write, rule 2). The
  console identity in markers is `[console] agent_id` or `<osuser>@<host>`.
- **UI** — the email card gets the same Claim/Resolve/Reopen/Close buttons as a
  chat queue card, routed by `card_id` to the email lifecycle methods.

Still deferred: the **customer-facing reply** (gated on confirmed ownership + the
autonomy gate, via `save_reply_draft`/`send_draft`), the **forward-only startup
backlog scan** + thread re-read to reconstruct state on launch (Q1), losing-ack
hygiene (Q6), and audit parity. The cross-console convergence window is Exchange's
replication latency to each member's store (Q2) — accepted.

## Implemented — slice 1: a shared mailbox as a queue card

The first slice surfaces a shared-mailbox email as a queue card in the Triggers
inbox, reusing the chat queue-card model — no claim mechanism yet (the card stays
`open`). What shipped:

- **Mapping config** — a top-level `[mailboxes]` table in `config.toml` maps a
  shared-mailbox address to its queue (the email analog of `room_queue`, but
  *not* host-bound: desktop Outlook is local, with no SSH host):
  ```toml
  [mailboxes]
  "it-support@corp.com" = "it-support"
  ```
  `config.read_mailboxes()` lowercases addresses and drops blanks. Mail not
  addressed to a mapped mailbox (personal/other mail) never becomes a card.
- **Recipient → queue matching** resolves the *real* SMTP recipients, not the
  `MailItem.To` display names. `_mail_payload` reads the raw transport header
  block (MAPI `PR_TRANSPORT_MESSAGE_HEADERS` via the COM `PropertyAccessor`) and a
  pure stdlib parser (`_recipients_from_headers`) pulls addresses from `To`/`Cc`
  plus the envelope-delivery headers (`Delivered-To` / `X-Original-To` / the
  `Received: … for <addr>` trace) — so a mailbox reached via an alias or
  distribution list is still recognised. The bridge matches those **exactly**
  against the map (a lookalike can't match), falling back to a substring match on
  the display `To` for internal MAPI mail that carries no header block. (The
  alias-only-no-header tail is the future store-based signal, Q1/Q4.)
- **The new_mail → queue card fold** — `_mail_payload` now carries the Outlook
  `ConversationID`; the trigger engine calls a new `on_source_event` observer for
  every raw event, and the bridge's `_on_engine_event` folds an `outlook:new_mail`
  whose recipient matches a mapped mailbox onto one card per conversation
  (`_touch_email_card`, keyed `queue:email:<conversationId>`): `source = "email"`,
  `trigger = "queue:<name>"`, visitor = From, summary = subject/snippet. The
  engine `always_start`s the Outlook source when a mailbox is mapped, so the
  channel works without any mail/reminder trigger.
- **UI** — `TriggersView` renders an `email` card with the lifecycle chip (`open`)
  like a chat queue card; the hub-backed claim/lifecycle controls + live
  conversation pane stay gated to chat (`source === 'chat'`) until the
  MailboxClaim fold lands.

Deferred to later slices (see Open questions): the MailboxClaim mechanism
(first-ack-wins), forward-only startup backlog scan, the email conversation pane
+ reply path, and audit parity.

## The idea

A **queue is a help desk that can be fed by more than one channel.** Today a
queue is fed by a chat room (the `runspec-room` hub). A *shared mailbox* is the
classic help-desk channel, and it fits the **same** model: a new email is a new
conversation, agents **claim** it, work it through a **lifecycle**
(open → assigned → resolved → closed), and reply — exactly what we already do for
chat. The email channel's queue key is the **recipient address**: mail to
`it-support@corp.com` belongs to queue `it-support`. Email and chat for the same
queue converge into **one inbox, one claim, one lifecycle** — the Triggers tab.

So the model generalises to:

```
queue (help desk)
 ├── channel: chat   (runspec-room hub)        — strong consistency
 ├── channel: email  (shared mailbox / Outlook) — log-folded, first-ack-wins
 └── channel: …      (ServiceNow / JSM, later)
shared: one card · claim · lifecycle · autonomy gate · audit · Triggers surface
```

## What is already in place

- **Queue card + claim + lifecycle + conversation pane** in the Triggers tab
  (PR #217). A queue message is a first-class trigger card; Approve/Deny reuses
  the trigger Drawer; claim gates model spend.
- **The email transport already exists**: `runspec_console/sources/outlook.py` is
  an `OutlookSource` that binds the **local Outlook COM object model**
  (`win32com.client.DispatchWithEvents` on `Outlook.Application`) and fires on
  **`NewMailEx`** — event-driven push, **no Graph, no polling**, riding the
  desktop client's existing SSO session (sidesteps the Conditional-Access wall
  that blocks MSAL device-code). It is **forward-only** (A1): it sees mail that
  arrives while the console is running; it never scans the inbox for backlog.
  It already reads **categories** (`_split_categories`) and already has
  `save_reply_draft(entry_id, body)` and `send_draft(entry_id)` (local COM draft
  + send). `pywin32` is already a console dependency.

This means the email help desk is largely **wiring an existing source into the
queue/claim model**, not new plumbing.

## The mailbox-claim mechanism (the interesting part)

Chat gets claim coordination for free: the room hub holds the `assignee` and
broadcasts it, so a claim is **strongly consistent** — authoritative the instant
it is set. A shared mailbox has **no hub**. The decision: make the **mailbox
itself the source of truth** and converge by reading it. "First ack wins."

### Claim = a self-addressed ack email; first-ack-wins

1. A new customer email arrives → fires `NewMailEx` in **every** group member's
   local Outlook → appears as an unclaimed queue card on each console.
2. An agent clicks **Claim** → the console sends a tiny **ack email addressed to
   the queue address itself** (`To: it-support@corp.com`, *not* the customer),
   threaded on the conversation, carrying a machine marker.
3. That ack lands back in the shared mailbox → fires `NewMailEx` on every
   member's console → they converge. **First ack wins.**

This is leader election over an append-only log (the conversation), where the
mail server gives a **total order** for free.

### Four rules that keep "first-ack-wins" consistent

1. **Tiebreak on the *server's* order, not local time.** Two agents can both
   click Claim before either sees the other's ack (events are async across
   clients), so both send. Every console then picks the same winner from the same
   data: sort claim-acks by **server `receivedDateTime` + `internetMessageId`**
   (sender breaks a same-instant tie), lowest wins. Same mailbox + same sort =
   same verdict, zero coordination.
2. **Read-after-write ownership.** "I clicked Claim" is *not* ownership; "I see my
   own ack is the earliest in the thread" is. This makes the mailbox the
   authority and tolerates a silent send failure (no ack in the thread ⇒ no
   claim ⇒ you never act thinking you own something you don't).
3. **The ack is internal, so a loser can't spam the customer.** Addressing the
   ack to the **queue** (not the customer) means every agent sees it but the
   customer never does. The customer-facing reply is a **separate step, gated on
   confirmed ownership** (rule 2) **and on the autonomy gate** (it's an
   outward-facing action — same Approve/Deny as a tool call, reusing
   `save_reply_draft` / `send_draft`).
4. **Markers make acks machine-distinguishable.** An `X-Runspec-Claim:
   <queue>/<conversationId>/<agent>` header (or an Outlook **category** — already
   read by the source — or a MAPI named property) tags the ack. Lifecycle rides
   the same scheme (`X-Runspec-Lifecycle: resolved`), so **the email thread *is*
   the event log** — card state is reconstructed by replaying the marker
   messages, exactly as the chat side reconstructs from the hub's conversation
   frames.

### Authorization comes from Exchange, free

**Only a member of the email group can send-as the group.** So a non-member's
ack is *rejected by the mail server* — only a group member can ever produce a
**valid** claim-ack. **Group membership is the staffer ACL**; we maintain no
separate trust roster. The From is the group (not the person), so the *claiming
member's* identity lives in the `X-Runspec-Claim` marker (or in a
send-on-behalf From, if that's chosen over send-as).

### State is a validated fold (this is the keystone)

Don't think "last writer wins." **Current state = replay the conversation's
messages in server order, folding with validity rules:**

- claim-acks → ownership (earliest valid ack, rule 1);
- lifecycle markers → only count **if from the current owner** (a non-owner
  "resolving" is ignored);
- a **customer reply after resolve/close → reopens** (matches chat semantics;
  the reply's later server timestamp makes the fold land on "reopened").

Two agents resolving at once is harmless (idempotent → resolved). First-ack-wins
is just the first instance of the one rule: *state is a validated fold of the
ordered conversation.* This single model unifies chat and email.

## Channel-capability matrix

| Capability                | Chat (hub) | Email (mailbox) |
|---------------------------|:----------:|:---------------:|
| Queue card                | ✓          | ✓               |
| Claim                     | ✓ (strong) | ✓ (log-folded)  |
| Lifecycle open→…→closed   | ✓          | ✓               |
| Autonomy gate on reply    | ✓          | ✓               |
| Claim-gated agent turn    | ✓          | ✓ (slice 6)     |
| Audit / analytics records | ✓          | ✓ (slice 6)     |
| Conversation pane         | ✓          | ✓ (email thread)|
| Self-service catalog/forms| ✓          | ✗ (freeform)    |
| Presence                  | ✓          | ✗               |

"Email is a channel" ⇒ shares card/claim/lifecycle/gate/audit; opts out of
catalog and presence. **Audit parity is a requirement**, not optional: email
claim/resolve/reply must emit the same `run_summary`/audit records as chat or the
History/Analytics tabs go half-dark for email queues.

## Proposed shape

Two claim backends behind one interface, both feeding the same queue card +
lifecycle in the Triggers tab:

- `HubClaim` — chat; strong (hub assignee, broadcast). *Built.*
- `MailboxClaim` — email; log-folded, first-ack-wins over the conversation.

## Open questions for the next session

1. **Forward-only gap.** `NewMailEx` is forward-only — it misses customer mail
   that arrived while the console was **down** (chat gets catch-up from the hub on
   reconnect; email does not). Do we add a **bounded inbox scan on startup** for
   unclaimed items in the shared mailbox? Also note: reconstructing a card's
   *current claim/lifecycle* requires **reading the thread's marker messages**
   (a local-store read), not just listening for events — needed on startup and
   when a card first appears.
2. **Cross-client convergence window.** With local events the race window is
   Exchange's **replication latency** to each member's local store (seconds), not
   a poll interval — quantify and accept.
3. **Marker mechanism.** `X-` header (via COM `PropertyAccessor` /
   `PR_TRANSPORT_MESSAGE_HEADERS`) vs **Outlook category** (already read by the
   source — simplest, but visible to humans) vs MAPI named property. Headers are
   reliable for internal same-org sends (which is all the self-addressed ack
   ever is); pick one and a fallback.
4. **Thread / visitor identity.** visitor = From; thread = Outlook
   `ConversationID`. New-email-vs-reply ⇒ new-card-vs-update mapping. Fallback to
   normalized subject + participants when clients mangle threading.
5. **send-as vs send-on-behalf** for the customer reply (and the ack), and the
   exact local-COM send path that preserves threading (reply endpoint, not a
   fresh message).
6. **Losing-ack hygiene.** Internal, marker-tagged acks accumulate on hot
   threads — mark-read / file to a subfolder, hide from the marker-aware view.
7. **Conversation pane for email** — render the thread transcript from the local
   Outlook store; the composer's Send routes through `save_reply_draft` /
   `send_draft` reply-as-group, behind the autonomy gate.
8. **Resolve/reopen race walkthrough** — confirm the validated-fold rules cover
   two agents resolving, and a customer reply landing exactly as someone closes.

## Reuse summary (why this is tractable)

- PR #217 gives: queue card, claim/lifecycle UI, conversation pane,
  Approve/Deny Drawer, claim-gated turns.
- `sources/outlook.py` gives: local `NewMailEx` push events, category reads,
  `save_reply_draft` / `send_draft`.
- The autonomy gate already covers the outward-facing reply.
- The email thread itself is the claim/lifecycle log — no new store.

The genuinely new code is: recipient→queue mapping, the `MailboxClaim` fold
(ack send + marker read + validated fold), the startup thread-read for current
state, and making the conversation pane + audit channel-aware.
