# Email claim — move-based, Exchange-centralized (opt-in)

> **Design.** Reintroduces a shared-mailbox **claim** on top of the folder-watch
> model (`email-folder-watch.md`), but per-trigger and opt-in, and realized as a
> **Move of the shared item into a configured folder** rather than the removed
> `X-Runspec-Claim` marker-email round-trip. Status: implemented in runspec-console.

---

## 1. The problem it solves

The email channel is a **folder watch with no coordination**: if several consoles
watch the *same* shared mailbox+folder, every one fires its triggers on every new
mail — duplicate agent turns, duplicate replies (`email-folder-watch.md` §6). That
is fine for "watch my own mailbox" but not for a shared helpdesk mailbox staffed by
a team.

A claim system used to arbitrate "who handles this", but it was **removed in
0.55.0**: it self-addressed an `X-Runspec-Claim` marker email and folded the
conversation first-ack-wins. On internal same-org mail the marker **round-trip was
unreliable** (delivery latency + reordering), so the fold was racy — the reason it
was cut.

## 2. The mechanism — a move is the claim

We keep the "use Exchange as the shared store" idea but drop the mail round-trip:

> **A claim is `MailItem.Move` of the shared item into a folder the operator
> configured (which must already exist).**

Why a move is the right primitive:

- **Server-side + immediate.** No transport delivery, no latency — the item's
  location changes on Exchange the moment the winner moves it, visible to every
  console with mailbox access.
- **Near-atomic, with clean loser detection.** A `Move` re-keys the item's
  `EntryID`. A losing console's stored id (from *its* `new_mail` event) no longer
  resolves in the source folder, and a second console's `Move` of an
  already-moved item raises — either way the loser is detected without any
  cross-console protocol.
- **Owner is recorded.** The winner stamps a `Claimed: <operator>` category on the
  item before moving, so the poll (and a loser) can read *who* won and show
  "claimed by X".
- **Declutters.** The claimed mail leaves the shared Inbox for a work folder — a
  natural helpdesk workflow.

`claim_message(entry_id, owner, *, mailbox, source_folder, claim_folder)` in
`sources/outlook.py`:

1. Resolve the destination `claim_folder` first — a missing folder is a clear
   error (the operator must create it), not a half-claim.
2. Resolve the item; if its `EntryID` no longer resolves, or it has left
   `source_folder`, someone already won → `{claimed: False}`.
3. Stamp the `Claimed: <owner>` category and `Move` the item to `claim_folder`. A
   `Move` that raises (already moved) → `{claimed: False}`.
4. → `{claimed: True, owner, entry_id: <new id>}`.

`claim_owner(...)` reads the winner back off the claim folder (by conversation id,
else subject) for the loser/poll label; `item_in_folder(...)` is the poll's probe.

## 3. Per-trigger, opt-in

Claim is a property of the **trigger**, not the mailbox — one shared mailbox may
carry both claim-gated queues and ordinary read-only triggers.

```toml
[[triggers]]
name         = "helpdesk-claim"
source       = "outlook"
event        = "new_mail"
mode         = "agent"            # after a claim, runs as normal (agent or rule)
claim        = true               # opt in to the claim gate
claim_folder = "Tickets/Claimed"  # existing folder a claimed item moves into

[triggers.match]
folder = "^Tickets/Open$"         # required when claim = true — the scope
```

Validation (`Trigger.from_config`): `claim = true` requires `source = "outlook"`,
`event = "new_mail"`, a non-empty `match.folder`, and a non-empty `claim_folder`.
The folder-to-match reuses the existing `match` machinery (no new key).

## 4. The gate — park then resume

The gate is **engine-side**, so `trigger_runner.make_handler` is unchanged (the
handler just runs the action once resumed):

- `TriggerEngine._dispatch` — for a claim trigger not yet granted, it moves the
  card to a new `AWAITING_CLAIM` column, registers it in `_parked_claims`, and
  **does not** submit the run. The card sits parked.
- `claim_email(card_id)` (bridge, from the card's **Claim** button) attempts
  `claim_message`. On a win → `engine.resume_claim(card_id)` re-dispatches the run
  (the gate is skipped this time) and the action runs; on a loss →
  `engine.mark_claimed_elsewhere(card_id, owner)` resolves the card as done,
  "claimed by X"; on a config error (missing claim folder) the card stays parked
  with the error so the operator can fix it and retry.
- `fire_test` ("Trigger now") bypasses the gate — a synthetic event has no real
  message to move.

## 5. Cross-console visibility — poll while parked

A move on console A doesn't notify console B. A lightweight bridge daemon
(`_claim_poll_loop`, ~25 s) probes each parked card's source item with
`item_in_folder`; when it has moved away, the card flips to "claimed by X"
(`mark_claimed_elsewhere`) and its Claim button disappears. This is visibility
latency only — the claim's *correctness* is the move, which is immediate. The poll
is best-effort and self-guards on engine-None / Outlook-unavailable / no-parked.

## 6. Mailbox folders list

To let one shared mailbox feed several claim queues, `[mailbox]` / `[[watch]]` take
a **`folders` list** (empty ⇒ Inbox only); the source fans one entry into one
folder-watch per folder, each tagged with its folder so a claim trigger's
`match.folder` scopes to it. The legacy scalar `folder` is still read. See
`email-folder-watch.md` §4.

## 7. Why not categories / a marker item instead

- **Categories alone** aren't atomic (two consoles both add a claim category with
  no clean loser signal). We use a category only to *record the owner*; the move is
  the arbiter.
- **A marker email / post item** reintroduces the round-trip / extra-item noise the
  0.55.0 removal was about.

The move is the one primitive that is server-side, immediate, and self-arbitrating.
