# Help desk channels — one queue, many channels

> Status: **design / discussion captured** (not yet built). Builds on the
> queue-in-Triggers work in PR #217 (`console: fold helpdesk queues into the
> Triggers tab`). See also `helpdesk-rooms.md`, `console-room-server.md`,
> `console-triggers.md`.

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
| Audit / analytics records | ✓          | **must match**  |
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
