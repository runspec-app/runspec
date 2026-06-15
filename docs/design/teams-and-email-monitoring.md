# Email helpdesk — single queue, email sign-up only (v1)

> **Status: design locked; v1 build essentially complete.** Supersedes the
> multi-queue / teams-as-queues idea, the `room_queue*`-on-host model
> (`helpdesk-rooms.md` / `email-queue-signup.md`), and the OIDC auth path.
> **For the completed state of truth (modules, versions, invariants, backlog)
> see `console-room-handover.md`** — this doc is the locked plan it was built to.

---

## 1. The shape — one helpdesk per room

One console family = **one email = one runspec-room instance = one helpdesk = one
queue.** A different email means a different room instance. Several **operator
consoles** (a shared pool) connect to the one room; **L1/L2 is a skill tier within
that pool**, relevant only to *who takes/escalates a ticket* — not a queue split.

The earlier "teams = queues" idea is dropped: operators all support every customer
team, so team never partitions operators or the catalogue. **A team is a property
of the *visitor*** (their department), not a queue.

---

## 2. Email monitoring — single mailbox (**done**)

One `[mailbox]` the Outlook source monitors — `address` (blank = default account),
optional `folder`, optional `label`. A new item → a `new_mail` event → the trigger
engine. The mailbox address doubles as the **helpdesk identity / sign-up
verify-from**.

```toml
[mailbox]
address = "helpdesk@corp.com"   # blank/absent → the default account
folder  = "Tickets/Open"        # blank/absent/"inbox" → the Inbox
label   = "Helpdesk"            # optional display label
```

Implemented: `config.read_mailbox()` / `write_mailbox()`, bridge `get_mailbox` /
`save_mailbox`, the Settings → Triggers tab, Outlook source wired to the one mailbox.

---

## 3. Teams = a visitor attribute (v1-with-tag)

- A **team** is the customer group/department a visitor belongs to (e.g. Team1,
  Team2, Team3). **Knowing a visitor's team is mandatory** — it rides on every
  operator view (card, inbox, thread).
- The **team list** lives in the runspec-room DB (so `/signup` can serve it),
  managed from the operator consoles, **seeded with a `Default` team** so the list
  is never empty.
- A team is just `{ slug, name }` — **no per-team catalogue, no per-team operators**
  in v1. (Per-team catalogues / routing are a *maybe later*.)
- **v2:** an operator can **reassign a visitor's team** (fix a mis-tag). The user
  record already carries `team`, so reassign just edits it.

---

## 4. Sign-up — email-verify only

- **runspec-room is email-verify only. OIDC is removed** (see §6). Email-verify is
  *the* authenticated door; the unauthenticated **dev mode** (`?user=`) stays only
  as the no-IdP local/test affordance.
- Flow: `/signup` → company email (domain-gated by the mailbox's domain) + full
  name + **team** (dropdown from the DB list) → verify link → joins the one queue.
- **Operator approves each sign-up** (per-request Accept). The domain allowlist
  gates *who may ask*; the operator Accept *authorizes each one*. v1 is "open to all
  on the domain" — the operator is the gate.

---

## 5. Catalogue + assignment

- **One shared catalogue** for the helpdesk (the union of on-shift operators'
  self-service runnables — the existing `discoverable`/LiveCatalog over the single
  namespace). Not per-team in v1.
- **Auto-assign = round-robin** across on-shift operators. v1 includes
  **return-to-queue** (an operator releases a ticket back to the pool → re-page) as
  a crude escalation.
- **v2:** reassign/escalate a ticket to a specific operator or tier (the request
  already carries an `assignee`); skill-based routing later still.

---

## 6. Email-only room — remove OIDC

Delete the OIDC path wholesale (no users yet; parity isn't worth the maintenance /
security-review cost — a future OIDC need is built separately):

- `web/auth.py`: drop `AuthConfig`, `auth_from_env`, `build_oauth`, `pick_identity`
  (OIDC client). **Keep** the shared session helpers (`session_user`,
  `verified_name`, cookie/session plumbing) — both email-verify and dev use them.
- `web/app.py`: drop the `auth` param to `create_app`, the `oauth` registry, and
  the `/login` + `/auth/callback` routes. Modes collapse to **email-verify** (gated)
  or **dev** (ungated).
- `server.py` / `signup.py`: `read_auth_mode` returns `email-verify` / `dev` only;
  drop the `oidc` branch + inference.
- Remove OIDC env docs (`CONSOLE_ROOM_OIDC_*`), tests, and references.

---

## 7. Build plan (v1)

- **A. Plan doc** — this. ✅
- **B. Single-mailbox monitoring** — ✅ (§2; console 0.63.0).
- **C. Email-only room** — ✅ OIDC removed (§6; room 0.20.0).
- **D. Single-queue helpdesk + team tag** — ✅ (mostly)
  - ✅ Room DB `teams` table (slug, name) + CRUD, `Default`-seeded (room 0.22.0).
  - ✅ Mailbox-driven connection: a host's `room_port` staffs the helpdesk; queue
    defaults to `helpdesk`; no `room_queue` needed (console 0.66.0).
  - ✅ Sign-up by team: `/teams` + team dropdown; domain/verify-from from the room's
    `--mailbox`; the visitor's `team` rides sign-up → member → session (room 0.23.0 /
    console 0.67.0).
  - ✅ Team on the operator card: `Request`/`assignment`/`page` carry it (room
    0.24.0 / console 0.68.0).
  - ⬜ *Deferred:* operator **Accept** per sign-up is still the existing flow;
    truly dropping `QueueDirectory`/advertise (it still works single-queue).
- **E. Settings panel**
  - ✅ Triggers editor (Settings → Triggers): mailbox config + full
    `runspec_triggers.toml` editor — `save_trigger`/`delete_trigger`/`_clean_trigger`
    (console 0.69.0) + the editor form (console 0.70.0).
  - ✅ **Teams-management UI** (room 0.25.0 protocol / console 0.71.0 bridge + tab):
    console→room `team_list`/`team_add`/`team_remove` frames → `hub.*`, answered
    with a `teams` frame; bridge `list_teams/add_team/remove_team` + `runspec:teams`
    event; **Settings → Teams** tab (add name→slug / remove, Default protected).
- **F. Docs** — ⬜ reconcile `helpdesk-rooms.md` + `email-queue-signup.md` to single
  queue + email-only + team-as-tag (still open). The visitor frontend was caught up
  to single-queue + teams + verified auto-join (room 0.26.0).
- **G. Reply suppression** — ✅ per-trigger `once_per_thread` over a `[mailbox]`
  default, durable across restarts (console 0.72.0; see `console-triggers.md` §5).

**v2 backlog:** reassign a visitor's team; reassign/escalate a ticket (L1→L2);
per-team catalogues / skill-based routing.

---

## 8. Open questions (for step D)

1. **Team list management UI** — where in the console (a small Settings list?), and
   what authorizes a team add/remove frame over the room transport (any connected
   operator, gated by the SSH-tunnel trust)?
2. **Where the room learns its single-queue identity** — *resolved:* the room takes
   a `--mailbox` / `CONSOLE_ROOM_MAILBOX` and only accepts consoles whose `[mailbox]`
   address matches (the `hello` handshake carries it). One email = one room. (room
   0.21.0 / console 0.64.0.)
3. **Team on the email channel** — a `new_mail`-originated request has no signed-up
   visitor team; does it default to `Default`, or stay untagged until an operator
   sets it?
