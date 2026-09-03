# Email folder-watch — multi mailbox/folder, no claim

> **Design.** Supersedes the email half of `helpdesk-channels.md` (shared-mailbox
> queue map + `mailbox_claim` markers / first-ack-wins fold) and realizes §4 of
> `runnables-portal.md` ("Channel 2 — email"). Status: **implemented in
> runspec-console 0.55.0** (the COM sink/poll is verified on Windows only; the
> pure pieces — config, payload tags, folder path-walk, dedupe — are unit-tested).
> The chat side already dropped claim (0.54.0); this drops it for email too and
> reshapes the source from one `NewMailEx` into a list of folder watches.

---

## 1. The reframe

Today the email channel is a **shared-mailbox helpdesk with claim**: one
`Application.NewMailEx` handler on the **primary inbox**, mail matched to a queue
by parsing its recipients, folded onto per-conversation cards, and coordinated
across consoles by self-addressed `X-Runspec-Claim` marker emails (first ack
wins). That claim machinery is the same fragile, authority-less coordination the
portal removed on the chat side — and on internal same-org mail the marker
round-trip is unreliable.

We replace it with a **folder watch**:

> runspec watches a **list of `mailbox + folder` combinations**. A new item in any
> watched folder fires a `new_mail` event **tagged with which mailbox and folder
> it came from**; the trigger engine decides what to do. **Outlook owns the
> routing** (rules, shared-mailbox delivery, manual moves) — runspec just reacts
> to what lands in a folder it's told to watch.

**The primary supported scenario is "my own mailbox."** A person points runspec at
their own Inbox (or a subfolder their Outlook rules sort into) and writes triggers
against it. Shared mailboxes still work, but **without claim** they carry a
duplication caveat (§6) we call out loudly.

### Why this shape
- **No claim, by construction.** The folder *is* the assignment — "it's in my
  watched folder" is the only ownership signal, and it's Outlook's to decide. No
  markers, no fold, no cross-console convergence window.
- **`NewMailEx` can't do it.** It only fires for the default account's primary
  Inbox — never a subfolder, never a shared/secondary mailbox. `Folder.Items.ItemAdd`
  fires for *any* folder, which is exactly what "watch these folders" needs.
- **Multi-watch is the unlock.** One console can watch several mailboxes/folders
  at once, and because each event is tagged with its origin, a trigger's `match`
  can scope to a specific mailbox or folder (§5). One config, many channels.

---

## 2. Decisions (settled)

1. **Config is a list of watches** — `[[mailboxes]]` as an array of tables, each
   `{ mailbox?, folder? }` (§4). Supersedes the old `[mailboxes]` address→queue
   table.
2. **Blank/missing → the Inbox.** `folder` omitted, blank, or the literal
   `"inbox"` (case-insensitive) means that mailbox's **Inbox**. `mailbox` omitted
   or blank means the **default account**. So the minimal watch — an empty
   `[[mailboxes]]` entry — means "my own Inbox."
3. **Events are tagged.** A `new_mail` payload gains `mailbox` and `folder` (the
   watch's normalized identity), so a trigger can match on origin (§5). These come
   from the *watch* (we know which folder's `ItemAdd` fired), not from parsing
   recipients.
4. **No claim, ever.** Shared-folder fan-out duplicates — documented as a loud
   warning, not solved in code (§6).
5. **Forward-only.** Only items that arrive *after* the source starts fire. No
   startup backlog scan (already removed in 0.52.0); the poll fallback only looks
   at items newer than its last tick.
6. **Only triggered mail acts.** A watched folder emits `new_mail` for every new
   item; the **trigger `match`** decides which warrants an action (the 0.52.0
   "only triggered mail" rule, now over folders). Non-matching mail is ignored —
   it's just mail.

---

## 3. The mechanism

### 3.1 Per-folder `ItemAdd` sink + poll fallback
For each resolved watch folder, the source binds a COM event sink to that
folder's `Items` collection and handles **`ItemAdd(item)`**:

```
for each watch:
    folder = resolve(watch)              # §3.2; skip + log on failure
    items  = folder.Items               # HOLD this reference (see gotcha)
    sink   = DispatchWithEvents(items, _FolderItemsSink)
    sink.on_add = lambda item: emit(new_mail tagged with watch)
```

Two well-known COM caveats shape this:

- **Hold the `Items` reference.** If the `Items` object is garbage-collected the
  `ItemAdd` event silently stops firing. The source keeps every watched `Items`
  (and its sink) alive for the lifetime of the pump, exactly as the current source
  holds `self._app`.
- **`ItemAdd` misses bulk moves.** When many items land at once (a rule moving a
  batch, an initial sync) Outlook may not raise `ItemAdd` for each. So every watch
  also gets a **periodic poll** (e.g. 30 s): `Items.Restrict("[ReceivedTime] > '<last-tick>'")`,
  emit anything not already seen (dedupe by `EntryID`), advance the high-water
  mark. The poll is the floor; `ItemAdd` is the low-latency fast path. Both feed
  the same `EntryID` dedupe so an item never double-fires.

All of this stays on the source's own STA pump thread (`CoInitialize` →
`PumpMessages`), and — as today — events are marshalled onto the dispatcher queue
so `emit` (and the calendar rota's COM read) never runs re-entrantly inside a COM
callback.

### 3.2 Resolving a watch to a folder
Pure-ish resolution (the lookups are COM, the path-walk is testable):

- **mailbox** blank/missing → the default account: `namespace.GetDefaultFolder(6)`
  (`olFolderInbox`) as the base.
- **mailbox** named → the other store: prefer `namespace.Folders[mailbox]` (a
  mounted store/shared mailbox shows as a top-level folder); fall back to
  `GetSharedDefaultFolder(CreateRecipient(mailbox), 6)` for a shared mailbox the
  user has delegate rights to but hasn't mounted.
- **folder** blank/missing/`"inbox"` → the base Inbox from above.
- **folder** a path → descend `Folders[seg]` for each `/`-separated segment from
  the mailbox's folder root (e.g. `Tickets/Open`).

A watch that can't resolve (mailbox not mounted, folder misspelled, no delegate
rights) logs **one actionable line** naming the watch and is **skipped** — it
never crashes the source or the other watches. (`available()` still reports the
platform/pywin32 gate as today.)

### 3.3 The slimmed payload
`_mail_payload` keeps the human/trigger fields and **drops the claim-fold
metadata**:

| field | keep? | note |
|---|---|---|
| `from`, `to`, `subject`, `body`, `received`, `categories` | ✅ | trigger-matchable, unchanged |
| `entry_id` | ✅ | reply threading (`save_reply_draft`) |
| `conversation_id` | ✅ | still handy for a trigger / threading; no longer a fold key |
| **`mailbox`**, **`folder`** | ➕ **new** | the watch's identity — the routing signal |
| `recipients`, `received_ts`, `message_id`, `claim_marker` | ❌ removed | only ever fed the claim fold |

With `recipients`/`received_ts`/`message_id` gone, the header-parsing helpers that
served only them (`_transport_headers`, `_recipients_from_headers`,
`_received_ts*`, `_header_field`, `_named_header`, `_PS_INTERNET_HEADERS`) go too.
The mailbox an email "belongs to" is no longer inferred from transport headers —
it's simply **the folder it landed in**.

---

## 4. Config

`[[mailboxes]]` — an **array of tables**, one per watch:

```toml
# 1) The simplest case: watch my own Inbox. Empty entry = default account, Inbox.
[[mailboxes]]

# 2) A subfolder my Outlook rules sort tickets into (still my own mailbox).
[[mailboxes]]
folder = "Tickets"

# 3) A shared mailbox I have rights to, a specific subfolder.
[[mailboxes]]
mailbox = "it-support@corp.com"
folder  = "Tickets/Open"

# 4) Explicit inbox of another account (equivalent to omitting `folder`).
[[mailboxes]]
mailbox = "alerts@corp.com"
folder  = "inbox"
```

Rules:
- `mailbox` — an account/store address. Blank/missing → the default account.
- `folder` — a folder path under that mailbox, `/`-separated. Blank, missing, or
  `"inbox"` (case-insensitive) → the Inbox.
- A watch's **identity** is the resolved `(mailbox, folder)`; exact duplicates
  collapse to one sink.
- Reading is surface-only (`config.read_mailbox_watches()` returns a normalized
  list); the `room_*`/transport keys are untouched.

> **Migration:** the old `[mailboxes]` *table* (`"addr" = "queue"`) is replaced by
> this *array of tables*. There is no queue key any more — the folder is the
> channel. (No back-compat shim; the email queue/claim feature had no external
> users and is being removed wholesale.)

---

## 5. Triggers know the mailbox and folder

Because the payload is tagged, a trigger's `match` (a field→regex map that
`search`es the payload) can scope by origin — no new trigger machinery:

```toml
# Only password-reset mail that landed in the it-support "Tickets" folder.
[[triggers]]
name   = "password-reset"
source = "outlook"
event  = "new_mail"
mode   = "agent"
[triggers.match]
mailbox = "it-support@corp\\.com"
folder  = "^Tickets$"
subject = "(?i)password reset"

# Anything new in *my* inbox from a specific sender → run a runnable.
[[triggers]]
name   = "oncall-page"
source = "outlook"
event  = "new_mail"
runnables = ["ack-page"]
[triggers.match]
folder = "(?i)^inbox$"
from   = "pagerduty@"
```

A trigger with no `mailbox`/`folder` clause fires for matching mail from **any**
watched folder — the broad case still works, and narrowing is opt-in.

---

## 6. ⚠️ Shared mailboxes — coordinate with an opt-in claim, or avoid fan-out

By default runspec does **not** coordinate across consoles. Each console reacts to
its own watched folders independently. Therefore:

> **If two or more consoles watch the *same* shared mailbox+folder, every one of
> them fires its triggers on every new mail** — duplicate agent turns, duplicate
> drafted replies, the same runnable run twice.

**The fix for a shared helpdesk mailbox is the opt-in, per-trigger claim** (see
`email-claim-move.md`): set `claim = true` + a `claim_folder` on the matching
trigger. Each matched mail parks behind a **Claim** button; the winning operator's
console **moves the item into the claim folder** (Exchange as the shared store, no
mail round-trip) and only that console runs the action — the other consoles' cards
flip to "claimed by X". This is a deliberate, reliable replacement for the removed
`X-Runspec-Claim` marker-email mechanism.

Without claim, this feature is built for **"watch my own mailbox."** For an
un-claimed shared mailbox, keep exactly **one** of these true:

- **One console per shared folder** — only one operator watches `support/Tickets`.
- **Per-operator folders** — let **Outlook rules** route each operator's share of
  the mail into a *personal* folder, and each console watches only its own. Outlook
  owns the routing; runspec watches the result. (This is the recommended shared
  pattern.)
- **Read-only / idempotent triggers** — if a trigger only *reads* (no reply, no
  state change) duplication is harmless noise, not a correctness bug.

Fan-out several consoles at one shared folder **without** one of the above and you
will get duplicate replies. The docs and the config UI surface this warning at the
point a shared `mailbox` is entered.

---

## 7. Migration map

**Removed**
- `mailbox_claim.py` + `test_mailbox_claim.py` (markers, fold, first-ack-wins).
- `outlook.py`: `send_claim_marker`, `_named_header`, `_PS_INTERNET_HEADERS`, and
  the header-parse helpers feeding the fold (`_transport_headers`,
  `_recipients_from_headers`, `_received_ts` / `_received_ts_from_headers`,
  `_header_field`); the `recipients`/`received_ts`/`message_id`/`claim_marker`
  payload fields; `OnNewMailEx` / the `NewMailEx` subscription.
- `bridge.py`: `_email_feed_gate`, `_email_queue_for`, `_email_conversation_key`
  and the conversation fold, `_email_threads`, the email queue cards driven by
  claim/lifecycle, the `[mailboxes]` address→queue map (`read_mailboxes` becomes
  `read_mailbox_watches`).

**Kept / changed**
- `_mail_payload` — slimmed (§3.3) and **tagged** with `mailbox`/`folder`.
- `save_reply_draft` / `send_draft` — unchanged (reply by `entry_id`; still
  human-sent). `from_mailbox` still optional for send-as.
- The trigger → runnable / trigger → agent-turn path — unchanged, now **ungated**
  (no claim gate): a matched `new_mail` becomes an ordinary trigger card + action.
- The calendar rota (`decide_calendar` / `read_calendar_now`) — unrelated, untouched.

**New machinery**
1. Per-folder `Items.ItemAdd` sink + the bulk-move **poll fallback**, with
   `EntryID` dedupe and a per-folder high-water mark.
2. **Folder resolution** (`mailbox`+`folder` → a MAPI folder; default/shared/
   subfolder), skip-and-log on failure.
3. `[[mailboxes]]` **watch-list** config + `read_mailbox_watches()`.
4. The `mailbox`/`folder` payload tags + a config-UI **shared-mailbox warning**.

---

## 8. Build plan (in order)

1. **Config: `read_mailbox_watches()`** — parse `[[mailboxes]]` into a normalized
   `[{mailbox, folder}]` list (blank/`"inbox"` → Inbox; default account;
   dedupe). Pure, fully unit-tested.
2. **Payload: slim + tag `_mail_payload`** — drop the fold fields, add
   `mailbox`/`folder` (passed in by the sink). Pure, unit-tested with fakes.
3. **Source: the folder-watch `OutlookSource`** — resolve each watch, bind an
   `ItemAdd` sink per `Items` (holding the ref), the poll fallback + dedupe,
   skip-and-log on unresolved watches. The pure pieces (path-walk, dedupe,
   high-water) are tested with fakes; the COM binding mirrors the existing pump.
4. **Bridge: remove the claim/queue email machinery**; route a matched `new_mail`
   as a plain trigger action (ungated). Update `start_triggers` to start the
   source when any watch is configured.
5. **Docs + config UI**: the `[[mailboxes]]` watch list, the trigger
   `mailbox`/`folder` match examples, and the **shared-mailbox no-claim warning**
   at the point a shared address is entered.

The COM sink/poll (step 3) and the Outlook-specific resolution can only be
exercised on Windows with Outlook; everything else (config, payload tagging,
dedupe, path-walk) is pure and tested on any platform — the same split the current
source already uses.
