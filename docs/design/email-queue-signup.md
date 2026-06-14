# Email-verified queue signup — internal self-service enrollment

> **Design.** Settled with the user (this note); no code yet. A new **auth mode**
> for `runspec-room`: instead of OIDC, an internal employee signs up for a queue
> with their email + name, proves the email by clicking a verify link, and is
> then a member of that queue. The verify email is sent **by a connected console,
> as the queue's own mailbox** — the room never sends mail. Pairs with the
> runnables portal (`docs/design/runnables-portal.md`): enrollment gets you *into*
> a queue to ask; the per-request operator **Accept** is still the authorization.

---

## 1. The reframe

`runspec-room` is **internal-only** — it runs on the company network and is a
portal for *other teams* to request actions they're **trained to ask for but
lack the permission to run themselves**. The canonical example: a Docker instance
many teams share — no single team may restart the whole instance, but they can
**request** a restart, and the on-shift operator **Accepts and runs it when
suitable**. The requester knows *what* to ask for; the operator holds the
*permission*.

For that to work, the operator must know **who** is asking. Today identity comes
from OIDC. This adds a lighter, IdP-free alternative built on email:

> Sign up with your **company email + full name**, **verify the email** (a link
> sent *as the queue mailbox* by a connected console), and you're a member of that
> queue. Your email + name ride **every request** so the operator sees exactly who
> they're accepting for.

### Two clean, orthogonal trust layers
- **Email-verify = *who*.** You own a real, internal address (anti-impersonation
  — you can't just type `ceo@corp.com`).
- **Operator Accept per request = *what's authorized right now*.** Already built
  (the autonomy-gate Approve on the portal card). Enrollment does **not** grant
  the action; it grants the right to *see the catalogue and ask*.

Because every action is already operator-gated, enrollment only has to establish
identity — it does not need to authorize anything.

### Why email is enough here (the abuse surface)
On a public service, a form that emails arbitrary addresses is a spam vector.
**Internal-only removes that:** the room is reachable only from inside, and the
**allowlist is the queue mailbox's own domain** — a signup can only ever send a
verify mail to a *same-company* address. The residual risk (an insider pestering
colleagues) is small, accountable, and auditable. So the allowlist is **automatic
and zero-config** — derived from `it-support@corp.com` → `corp.com`.

---

## 2. Decisions (settled)

1. **A third auth *mode*, chosen at startup** — `CONSOLE_ROOM_AUTH = email-verify |
   oidc | dev`. OIDC and dev are untouched. The *mode* is a boot-time arg; the
   *queues* (their emails, domains, display names) arrive dynamically from what
   consoles advertise — so the sign-up dropdown populates at runtime.
2. **The console sends the verify email**, as the **queue mailbox** (Outlook
   send-as). The room never sends mail (no SMTP dependency; the mail comes from the
   real queue address, which reads as legitimate internally).
3. **Durable outbox.** If no console for the queue is connected at signup time, the
   pending send is **persisted in the room's SQLite** and **flushed to the first
   console that connects** for that queue (off-shift is fine). One console claims
   each send so it can't double-fire. The user just sees "we'll email you shortly."
4. **Domain allowlist = the queue mailbox's domain** — automatic, no config.
5. **No enrollment approval in v1.** The per-request operator Accept is the
   authorization. A per-queue **"gated membership"** flag (an operator approves
   *who joins*, for sensitive queues) is a **v2** opt-in.
6. **Full name is display-only** (self-asserted, unverified). The **email** is the
   verified trust anchor the operator keys off.

---

## 3. The flow

```
visitor browser                 room (web + hub)                 a connected console
───────────────                 ────────────────                 ───────────────────
GET /signup  ───────────────▶  list queues (from advertisements)
pick queue + email + name ──▶  validate domain == queue domain
                               write pending_signup row (DB)
                               ── send-verify request ─────────▶  Outlook: send mail AS
                                  (or queue it until a console      the queue mailbox, with
                                   for this queue connects)         a link to /verify?token=…
                               ◀───────── ack (sent) ────────────
click /verify?token=… ──────▶  validate signed token (single-use,
                               expiring) → mark verified, persist
                               member, set signed session cookie
                               302 → /?queue=<q>
… normal portal …              every request carries {email, name} → operator card
```

### 3.1 Sign-up
A real Starlette `/signup` page (today the join form is client-side only). It
fetches the advertised queues (`GET /queues`), shows a **required dropdown** plus
**email** and **full name** fields, and POSTs. The room:
- rejects an email whose domain ≠ the chosen queue's mailbox domain;
- writes a `pending_signup` row (`email, name, queue, token, created_at,
  send_state`);
- dispatches a **send-verify** request to a connected console for the queue, or
  leaves it `send_state = "pending"` for the outbox (§3.3).

### 3.2 Verify
The link is `<public_url>/verify?token=<signed>` where the token is an
`itsdangerous`-signed payload `{email, name, queue, nonce}` with a short TTL,
**single-use** (the nonce is burned on first use). On a valid click the room marks
the member **verified**, persists `(email, name, queue)`, sets a **signed session
cookie** (the same mechanism OIDC mode uses), and redirects into the portal.
Thereafter `session_user(request)` returns the verified email, so the existing
`/ws` and `/catalog` gates work unchanged.

### 3.3 Durable outbox (queue the send)
A send is a row, not a fire-and-forget call:
- On signup, if a console for the queue is connected → send the **send-verify**
  frame now and mark `send_state = "sent"` on the console's ack.
- If none connected → `send_state = "pending"`; when **any** console for that
  queue *connects* (`join_agent`), the room **flushes** its pending sends to that
  console. One console claims each (a `claimed_by` / `sent` transition) so two
  consoles connecting together don't both send.
- Expiry: a pending send older than the token TTL is dropped (the user re-signs).

---

## 4. Queue advertisement

The console already publishes its catalogue (the `catalog` frame). It additionally
advertises, per queue it staffs, a small descriptor:

```
queue        = "it-support"          # the namespace (room_queue), today
queue_email  = "it-support@corp.com" # the queue's mailbox — verify sender + domain
display_name = "IT Support"          # friendly label for the dropdown
```

`queue_email` is the **single source of truth**: the verify-from address **and**
the allowlist domain. It is configured on the host entry (the natural home,
alongside `room_queue`); the design assumes a `room_queue_email` host field (the
console's `[[mailboxes]]` watch address is the obvious default). The room
aggregates advertisements from all connected consoles → `GET /queues` →
the dropdown. A queue with no connected console (so no advertisement) simply isn't
offered.

---

## 5. Identity on every request

`submit_request` already carries the visitor. With email-verify the visitor *is*
the verified email; the request payload also carries the **name**, so the
operator's portal card shows `Jane Doe <jane@corp.com>` instead of a raw id. The
operator uses the **verified email/domain** to decide whether the requester's team
is allowed to ask for this — exactly the Docker-restart judgement. (The name is a
convenience; the email is the anchor.)

---

## 6. Storage (room SQLite)

The room already owns a SQLite database (the transcript). Add two tables:

- **`members`** — `(email, name, queue, verified_at)`: a verified enrollment. A
  returning user with a valid session skips re-verify; an expired session re-signs
  (no new email needed if already a member — the room can re-issue a session for a
  known member, TBD at build time).
- **`pending_signups`** — `(token, email, name, queue, created_at, send_state,
  claimed_by)`: the durable outbox + the single-use verify nonce. Burned on verify;
  swept on expiry.

(Session state stays in the signed cookie, as OIDC mode does — not a table.)

---

## 7. Wire frames (console ↔ room, over the NDJSON door)

- **advertise** (console → room): the queue descriptor (§4) — folded into the
  existing `hello`/`catalog` flow rather than a wholly new channel.
- **send-verify** (room → console): `{to, name, queue, queue_email, verify_url}`
  — "send this verify email as the queue mailbox." Dispatched to one connected
  console; the durable outbox replays it on connect.
- **verify-sent** (console → room): ack `{token, ok | error}` → flips the outbox
  row to `sent` (or back to `pending` to retry elsewhere on failure).

The console sends the mail with a new Outlook helper (a sibling of the existing
`save_reply_draft` / `send_draft` send-as path): compose a fresh `MailItem`, set
`To` / `Subject` / `Body` / `SentOnBehalfOfName = queue_email`, `Send()`. Windows /
Outlook only, like the rest of the email channel.

---

## 8. Security posture

- **Internal-only** — not public-facing; reachable on the company network.
- **Allowlist = queue mailbox domain** — automatic; a signup can only email a
  same-company address.
- **Verify token** — `itsdangerous`-signed, short TTL, **single-use** (nonce
  burned). The link only *confirms* an address; it grants nothing by itself.
- **Session** — signed cookie (existing mechanism); over TLS in production (the
  room already supports TLS / a TLS-terminating proxy).
- **No SMTP in the room**, no IdP, no inbound secrets — the room stays a pipe; the
  one privileged action (sending mail) happens on the operator's own Outlook.
- **Authorization is still per-request** — enrollment never authorizes an action;
  the operator Accept does.

---

## 9. Not in v1

- **Gated membership** (operator approves *who joins* a sensitive queue) — a
  per-queue opt-in, v2. The room↔console handshake mirrors the verify outbox.
- **The `/run` natural-language path** — separate track; `/run` is already logged
  as a `chat`/`run` event (runspec-console 0.58.0), awaiting the LLM derivation.
- **Non-Outlook email send** — the console verify-send is Outlook-only, matching
  the email folder-watch channel.

---

## 10. Build plan (slices, in order)

1. **Room: storage + auth mode.** `members` + `pending_signups` tables; the
   `CONSOLE_ROOM_AUTH = email-verify` mode + `itsdangerous` token helpers
   (sign/verify, TTL, single-use nonce). Pure-testable.
2. **Room web: `/signup`, `/queues`, `/verify`.** The dropdown (from
   advertisements), the domain check, the signed-token issue/verify, the session —
   plugged into the existing `session_user` gate so `/ws` + `/catalog` need no
   change. The durable-outbox row lifecycle.
3. **Wire frames + advertisement.** `room_queue_email` host field → advertise
   `queue_email`/`display_name`; the `send-verify` / `verify-sent` frames; the
   room's flush-on-connect.
4. **Console: send the verify email** via Outlook send-as the queue mailbox (new
   helper) + handle the `send-verify` frame + ack. (COM — Windows-verified.)
5. **Identity on requests.** Carry `name` alongside the verified email into the
   request payload → render on the operator card.

The room-side pieces (1, 2, the outbox state machine, the token helpers, the
domain check, the advertisement aggregation) are pure and unit-tested on any
platform; only the **Outlook send** (slice 4) needs Windows to verify — the same
split the email folder-watch already uses.
