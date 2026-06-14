# Runnables portal — dropping the claim system

> **Decision record + design.** Origin: `claim_removal_proposak.md` (the rough
> proposal). This supersedes the **claim** machinery in `helpdesk-channels.md`
> (email self-addressed markers / first-ack-wins / the validated fold) and the
> **claim/lifecycle** in `helpdesk-rooms.md`. Status: **design agreed with the
> user (this note); no code yet.** Build in a fresh session.

---

## 1. The reframe (what we're actually building)

We are **not** building a multi-channel helpdesk with distributed claim
coordination. We are building a **self-service runnables portal**:

> A visitor requests a *service* — which is a **runnable** from a live catalogue —
> and the system **auto-routes** it to an available operator who **has** that
> runnable and enacts it through the autonomy gate.

Two front doors, same model:

- **UI portal** (browser, OIDC) — pick a runnable → it routes to an operator.
- **Email** (Outlook) — a second channel where **Outlook owns the routing**: mail
  lands in the operator's folder and runspec reacts to it. We don't reinvent email.

**There is no runspec claim system.** Assignment is automatic (UI) or Outlook's job
(email).

### Why drop claim

- It is the single **hardest, most fragile** subsystem we have: distributed
  claim with **no authority** — self-addressed `X-Runspec-Claim` markers, the
  first-ack-wins fold, the cross-console convergence window, read-after-write
  ownership. It *just* broke in live testing (the marker round-trip fails on
  internal same-org mail; the marker acks are visible plumbing in a shared box).
- It solves a problem we don't need to solve. **Outlook already routes mail**
  (rules, shared-mailbox assignment, folders). A **queue already knows who's on
  shift**. Claim was us re-implementing assignment without a server; auto-assign
  with the hub *as* the server is simpler and correct by construction.
- The differentiator was never "another chat app." It's **runnables** — a curated,
  shift-aware catalogue of things the fleet can safely run. Lead with that.

---

## 2. Decisions (settled)

1. **Minimal, per-request messaging — not freeform chat.** Every interaction
   **starts by selecting a runnable**; a small message thread hangs off the
   request for clarification / the result. We are not replacing instant messaging.
2. **No on-shift operator for a runnable →** the request shows **"no operator
   currently available"** (use IM / call / shout over). Plus a **page**: the
   visitor can click to **chime every *connected* console of the queue — on-shift
   *and* off-shift-but-connected — naming the runnable**, so someone can come on
   shift or respond out-of-band.
3. **Email = one watched folder per operator.** Config is `mailbox + folder`
   (folder ≈ the operator's name). **Outlook routes** mail into it (rules / manual
   / shared-mailbox assignment — not our concern). Runspec **watches that one
   folder** — and **triggers still apply** to it (a trigger's `match` decides which
   folder-mail warrants an agent turn).

---

## 3. Channel 1 — the UI portal

### 3.1 Sign-up → queue
A visitor (employee) authenticates via the existing **OIDC door** and is bound to
a **queue** (e.g. `it-support`) — by sign-up choice or directory mapping. The queue
determines which operators and which catalogue they see. (One queue per visitor in
v1; multi-queue is additive.)

### 3.2 The catalogue is the product
The catalogue a visitor sees is the **live union of the runnables published by the
on-shift operators of their queue**, with **per-runnable operator-affinity**:

- The hub already knows, per connected console (agent): its **namespace** (queue),
  its **published catalogue** (`set_catalog` / `build_catalog`, curated by the
  `discoverable` keyword), and its **presence** (on-shift `available` vs off-shift
  `away`, driven by the rota — the console stays *connected* either way).
- **Live catalogue** = union of runnables from agents who are **on-shift**.
- **Affinity** = which agents hold each runnable. A runnable only one operator has
  **must** route to them; it **vanishes** from the catalogue when that operator
  goes off-shift (and reappears when they're back).

### 3.3 Request → auto-assign → enact
1. Visitor **selects a runnable** → a **form** is generated from its `inputSchema`
   (the visitor-curated args via `discoverable` — this already exists), fills it,
   submits. The submission is a **request** = `{runnable, args, visitor, queue}`
   (the existing `kind="request"` / `data={tool,args}` message shape).
2. The hub **auto-assigns** the request to an available operator — **skill-first,
   then round-robin**:
   - candidates = **on-shift** agents who **hold** the runnable;
   - among them, **plain round-robin** (the next candidate in rotation). Single
     holder → always them.
3. The **assigned operator's console** receives the request in its inbox. The
   operator enacts the runnable **through the autonomy gate** (unchanged) — agent
   turn or direct run — and the **result** goes back to the visitor on the request
   thread. Audit / `run_summary` records as today.

**No claim step.** Assignment is immediate and automatic.

### 3.4 No operator available (the page/chime)
If **no on-shift operator holds the runnable** at submit time:
- the request surfaces **"no operator currently available"** to the visitor
  (with the out-of-band hint), and
- a **Page the team** action **chimes every connected console of the queue
  (on-shift and off-shift-but-connected)**, the chime naming the requested
  runnable. An off-shift operator who holds it can come on shift (or respond out of
  band); when an eligible operator is on shift, the pending request auto-assigns.

> Requires the hub to distinguish **connected** from **on-shift**: it already has
> both (the WS connection = connected; the rota-driven presence = on-shift). The
> chime is a new broadcast frame to *connected* agents, independent of presence.

### 3.5 Request lifecycle (claim-free)
`requested → assigned (auto) → in-progress → done` (or `failed`); `unassigned`
(no operator) is the pre-assign holding state that a page targets. **No
claim/resolve/reopen as claim actions.** "Done" is the operator closing out the
request (result delivered). An assigned operator who doesn't act within a
**timeout** → the request returns to `unassigned` and **auto-pages** (the team is
chimed), so a stuck assignment never strands a visitor.

---

## 4. Channel 2 — email (folder watch, Outlook-routed)

### 4.1 Config
A `[mailboxes]`-style entry becomes **`mailbox address + folder`** — one watched
folder per console, e.g. the operator's own folder inside the shared mailbox, or a
folder in their own mailbox. **Outlook puts mail there** (a server-side rule, a
shared-mailbox assignment, or a manual drag) — *that* is the "assignment", and it's
entirely Outlook's job.

### 4.2 Mechanism
- Monitor **that folder** via **`Folder.Items.ItemAdd`** (with a periodic poll
  fallback for stores where the event is unreliable). `NewMailEx` is *not* used for
  this — it only fires for the primary inbox, not a subfolder.
- A new item in the folder → a `new_mail`-shaped event → **the trigger engine**.
  **Triggers still apply** (the user's explicit requirement): a trigger's `match`
  gates which folder-mail warrants an agent turn (the 0.52.0 "only triggered mail"
  idea survives, now over the folder).
- The operator replies (drafts via `save_reply_draft` / sends; reply-as-the-mailbox
  optional). **No markers, no fold, no claim** — "in my watched folder" *is* the
  assignment.

### 4.3 What email keeps vs drops
- **Keeps:** the Outlook COM source (repurposed: watch a folder, not `NewMailEx`),
  `_mail_payload`, `save_reply_draft` / `send_draft`, the trigger/agent path,
  `_named_header` (still handy if any header is read).
- **Drops:** `mailbox_claim.py` (markers / fold / first-ack-wins), the marker
  send/read (`send_claim_marker`, `claim_marker` extraction), `_email_feed_gate`,
  `_feed_email_event`, the `email`/`claimed` event, the claim/resolve/reopen/close
  lifecycle controls, and (already gone) the backlog scan.

---

## 5. Migration map — removed / kept / new

**Removed**
- `runspec_console/mailbox_claim.py` + `tests/test_mailbox_claim.py` (the entire
  claim domain).
- Email: markers, `send_claim_marker`, `_email_feed_gate`, `_feed_email_event`, the
  `claimed` event, lifecycle controls, `_ingest_email`'s fold/claim parts.
- Chat: the **claim gate** (`_feed_engine_for`), hub `assignee`/`claim`/`resolve`/
  `reopen`/`close` *as claim actions*, the lifecycle conversation frames driven by
  claim.
- **PR #226** (chat history-on-claim) becomes **moot** — no claim ⇒ no
  history-on-claim. **Close it.** (The room's transcript replay could be repurposed
  as "show the request thread to the assignee", but that's now trivial.)

**Kept and made central**
- **Runnable discovery** (SSH+MCP, importlib.metadata) — unchanged.
- **The catalogue**: `build_catalog`, `set_catalog`, the `discoverable` keyword,
  the **visitor form from `inputSchema`** — moves from "add-on" to the spine.
- The **room hub** as the *server*: presence, the message pipe, namespace agents —
  repurposed for catalogue aggregation + routing + the chime.
- The **autonomy gate**, **agent turns**, **audit/`run_summary`**, OIDC + SSH doors.

**New machinery**
1. **Folder-level Outlook monitoring** (`ItemAdd` / poll) — the whole email source.
2. **Presence-aware live catalogue** in the hub: per-queue union of on-shift
   agents' runnables + per-runnable **affinity** (who holds it).
3. **Auto-assignment engine** in the hub: skill-first + least-load fair routing;
   the `unassigned` holding state; auto-assign a pending request when an eligible
   operator comes on shift.
4. **Page / chime**: a broadcast to *connected* agents of a queue (on- and
   off-shift), naming the runnable; a desktop chime on the console.
5. **Request lifecycle** (claim-free) + a **minimal per-request thread** (scoped
   reuse of the existing message pipe).

---

## 6. Resolved decisions

All settled with the user — no open questions remain for the build:

1. **Fairness = plain round-robin** among the on-shift candidates who hold the
   runnable (no load-tracking in v1). Single holder → always them.
2. **Assigned-operator AFK = timeout → `unassigned` + auto-page.** A stuck
   assignment re-enters the queue and re-chimes the team rather than stranding the
   visitor.
3. **Off-shift-but-connected: confirmed.** A console stays *connected* (WS up) when
   the rota flips it off-shift — presence goes `away` but the connection persists —
   so the page/chime reaches off-shift operators. (Re-verify at build time, but the
   design assumes it.)
4. **Catalogue-only.** A visitor can only request a **listed** runnable; there is
   **no** freeform / "something else" escape hatch. The **page** covers "I need X
   that isn't currently staffed."
5. **Email folder: any folder path.** Config carries `mailbox + folder`; don't
   constrain whether it's a shared-mailbox subfolder or the operator's own mailbox.
6. **Queue = the host's `room_queue`.** An operator console staffs a queue; the
   catalogue + routing key off that same namespace (reuse, no new concept).

---

## 7. Build plan (fresh session, in order)

1. **Hub: presence-aware live catalogue + affinity.** Aggregate connected agents'
   catalogues by namespace, filtered by on-shift presence; expose `{runnable →
   [agents]}`. (Pure-testable against fake agents.)
2. **Hub: auto-assignment + request lifecycle.** Route a `request` to a candidate;
   `unassigned` when none; auto-assign on the eligible-operator-comes-on event.
3. **Hub: page/chime broadcast** to connected agents of a queue.
4. **Console: portal request inbox** (assigned requests) + enact through the gate +
   result back on the thread. Repoint the Triggers/Rooms UI to the request model.
5. **Email: folder-watch source** (`ItemAdd`/poll) feeding the trigger engine;
   **remove** the claim machinery (`mailbox_claim.py`, markers, gates, lifecycle).
6. **Browser portal**: catalogue view → runnable form → submit → status/thread,
   with the "no operator / page" affordance.
7. **Close PR #226**, retire the claim docs (fold this note's outcome into
   `helpdesk-rooms.md` / `helpdesk-channels.md` as "superseded by the portal").

Removing claim is a **net deletion** — `mailbox_claim.py`, the marker COM, the
gates, the chat claim path — offset by the routing/catalogue machinery, most of
which is the existing catalogue code promoted to the centre. It's a **refocus, not
a rewrite.**
