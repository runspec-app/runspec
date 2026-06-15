# Console + Room helpdesk — handover / current state of truth

> **Status: living handover, accurate as of console `0.72.0` / room `0.26.0`
> (2026‑06‑15).** This is the consolidated record of the console + runspec‑room
> helpdesk work. Where an older design doc disagrees, **this doc and
> `teams-and-email-monitoring.md` win** — see the doc‑reconciliation table (§7).

---

## 1. The model (one line you must not cross)

**One console family = one email = one runspec‑room instance = one helpdesk =
one queue.** A different email means a different room instance.

- Several **operator consoles** (a shared pool) connect to the one room.
  **L1/L2 is a skill tier within that pool** — about *who takes/escalates a
  ticket*, never a queue split.
- A **team is a property of the *visitor*** (their department, e.g. Team1/2/3),
  **not** a queue and **not** the host. Operators all support every team.
- The **queue identity is the room's mailbox** (`--mailbox`). The internal queue
  name is the constant `helpdesk` (`DEFAULT_QUEUE`), the same in both packages.
- **Auth is email‑verify only.** OIDC was removed wholesale. Dev mode (`?user=`)
  remains as the no‑IdP local/test affordance.

The dead ideas (do not reintroduce): *teams‑as‑queues*, *per‑mailbox queue*,
*`room_queue*` on the host entry*, *OIDC / single sign‑on*, *a queue field in the
UI or sign‑up*.

---

## 2. Released versions

| Package | Version | State |
|---|---|---|
| `runspec-console` | **0.72.0** | PR #248 open (reply suppression) — publish after merge |
| `runspec-room` | **0.26.0** | merged (PR #247), publish when ready |

Earlier merged milestones this stream: room 0.20.0 (OIDC removed) → 0.26.0;
console 0.63.0 (single mailbox) → 0.72.0.

---

## 3. What's done — end to end

### 3.1 Email monitoring → triggers (console)
- One `[mailbox]` table in `config.toml`: `address` (blank = default account),
  optional `folder` (blank/`inbox` → Inbox), optional `label`, and the
  reply‑suppression default `once_per_thread` (see §3.5). Read/written by
  `config.read_mailbox()` / `write_mailbox()`; bridge `get_mailbox` /
  `save_mailbox`. The mailbox address doubles as the **helpdesk identity /
  sign‑up verify‑from**.
- `sources/outlook.py` watches that one inbox (per‑folder `ItemAdd` sink + 30s
  poll fallback, deduped by `EntryID`), emitting `new_mail` events whose payload
  carries `from/to/subject/body/received/categories/entry_id/conversation_id` +
  `mailbox/folder`.
- The `TriggerEngine` (`triggers.py`) routes events to actions through the
  autonomy gate, one card per match, serialised per trigger.

### 3.2 Triggers editor (Settings → Triggers)
- Full `runspec_triggers.toml` editor in `SettingsDrawer.tsx` (`TriggersTab` =
  `MailboxesTab` + `TriggersEditor`). Bridge: `list_triggers` / `save_trigger` /
  `delete_trigger` / `set_trigger_enabled`, `_clean_trigger` normalises the UI
  dict to the on‑disk shape.
- Per trigger: `name`, `source`, `event`, `mode` (**agent** = LLM turn /
  **rule** = fixed runnable), `autonomy`, `match` (regex per payload field),
  agent `rules` + `runnables` allowlist, rule `run` + `map`, `reply.draft` +
  `style`, `enabled`, and `once_per_thread` (§3.5).

### 3.3 The room (runspec-room): two doors, one process
- **Console door** — NDJSON over the SSH tunnel (`ndjson_server.py`). A console's
  `hello` carries its `[mailbox]`; the room **refuses a console whose mailbox
  doesn't match its own `--mailbox`** (one email = one room).
- **Browser door** — Starlette/uvicorn web app (`web/app.py`), email‑verify gated
  (`EmailVerifyConfig`) or dev mode (ungated, `?user=`). `create_app(hub, *,
  ndjson_addr, email_verify)`.
- Stores: `EnrollmentStore` (SQLite — `teams`, `members`, `pending_signups`) and
  the transcript store. `Hub` owns the `mailbox` identity, the team list, the
  signup/verify/enrol logic, requests + assignment.

### 3.4 Single‑queue helpdesk + teams
- **Queue collapse:** `conversation.py` / `chat_transport.py` `DEFAULT_QUEUE =
  "helpdesk"`. A host entry with a `room_port` (and `room` / `room_via`) staffs
  the helpdesk — **no `room_queue` needed**; `host_rooms()` builds the spec from
  `room_port` alone.
- **Teams** live in the room DB (`teams` table, `Default`‑seeded + protected).
  Protocol frames `team_list` / `team_add` (name → slug via `make_team_slug`) /
  `team_remove` (slug) over NDJSON → `Hub.list_teams/add_team/remove_team`,
  answered with a `teams` frame. Console bridge `list_teams/add_team/remove_team`
  + a `runspec:teams` event; **Settings → Teams** tab manages them.
- **Sign‑up by team:** `/signup` (server‑rendered) asks for company email
  (domain‑gated by the mailbox domain) + full name + **team** (dropdown from
  `/teams`). `Hub.submit_signup` writes a pending row + dispatches the verify mail
  *via a connected console as the mailbox*. `/verify` burns the nonce, persists
  the member, sets the session (`user`/`name`/`team`).
- **Team on the operator card:** the visitor's `team` rides `Request` →
  `assignment` / `page` frames and the `BrowserConnection`.

### 3.5 Reply suppression — `once_per_thread` (console 0.72.0, PR #248)
- A trigger fires on the **first** message in a conversation and ignores later
  replies. **Tri‑state**, resolving like `autonomy`: per‑trigger override →
  `[mailbox] once_per_thread` default → library default (`false`).
- Keyed on the event thread id (`conversation_id` / `thread_id`); **fail‑open**
  when there's no thread id. **Durable** across restarts via
  `trigger_state.py::ThreadDedupStore` (`runspec_trigger_state.json`); an
  in‑memory default keeps the engine correct/testable. Suppressed replies show up
  as a `deduped` disposition in the event‑audit feed.
- UI: a "Suppress replies by default" switch on the mailbox + a **Mailbox default
  / Once per thread / Every message** selector per trigger.

### 3.6 Visitor browser frontend (room 0.26.0)
- `web/frontend/index.html` + `app.js`: **no queue field** (server names the one
  queue, read from `/me`), **no OIDC/SSO wording**, `/logout` removed.
- **Verified visitors auto‑join** — after the verify link the client reads
  `name`/`team`/`queue` from `/me` and enters the helpdesk with no second gate.
- **Dev mode** declares identity on the link: `?user=` (name) + `?team=` (team),
  or types them in the join form. The dev `?team=` rides onto the operator card;
  a verified server ignores the query `?team=` and uses the session's (can't be
  spoofed).

---

## 4. Config surfaces (where the knobs live)

| Surface | File | Keys |
|---|---|---|
| Mailbox | `config.toml` `[mailbox]` | `address`, `folder`, `label`, `once_per_thread` |
| Triggers | `runspec_triggers.toml` | `[triggers.<name>]` + `.match` / `.map` / `.reply` |
| Rota | `runspec_triggers.toml` `[rota]` | working‑hours gate |
| Helpdesk host | `runspec_hosts.toml` host entry | `room_port`, `room`, `room_via` (no `room_queue`) |
| Room identity | `--mailbox` / `CONSOLE_ROOM_MAILBOX` | the one email = the queue identity |
| Room auth | `CONSOLE_ROOM_AUTH` | `email-verify` (needs `CONSOLE_ROOM_PUBLIC_URL` + session secret) / `dev` |
| Trigger state | `runspec_trigger_state.json` | internal `once_per_thread` dedup (not user‑edited) |

---

## 5. Key modules (orientation map)

**Console (`packages/python/runspec-console/runspec_console/`)**
`config.py` (mailbox/triggers/state paths) · `sources/outlook.py` ·
`sources/chat.py` (ChatSource: hello+mailbox, advertise, teams frames) ·
`sources/chat_transport.py` (`DEFAULT_QUEUE`, `host_rooms`) · `triggers.py`
(Trigger + `once_per_thread`, TriggerEngine) · `trigger_state.py`
(ThreadDedupStore) · `trigger_runner.py` (rule/agent handler) · `bridge.py`
(mailbox, triggers, teams, `start_triggers`).
**Console UI (`packages/console-ui/src/`)** `components/SettingsDrawer.tsx`
(tabs: SSH, LLM, Jump Hosts, Filters, Credentials, **Triggers**, **Teams**) ·
`bridge/index.ts` + `bridge/mock.ts`.

**Room (`packages/console-room/console_room/`)** `conversation.py`
(`DEFAULT_QUEUE`, namespaces) · `signup.py` (`Team`, `make_team_slug`,
`EmailVerifyConfig`, `AUTH_MODES`) · `enrollment_store.py` (teams/members/pending
+ migrations) · `hub.py` (mailbox, teams, signup/verify, requests) · `models.py`
(`TEAM_*` frames, `teams_frame`, assignment/page carry `team`) · `ndjson_server.py`
(mailbox‑match refusal, team frames) · `web/app.py` (`create_app`, `/me`,
`ws_room`) · `web/auth.py` (`session_user/name/team`) · `web/signup_routes.py`
(`/teams` `/signup` `/verify`) · `web/frontend/` (`index.html`, `app.js`).

---

## 6. Invariants & gotchas (read before changing)

- **One email = one room.** Never reintroduce a per‑mailbox queue or a host
  `room_queue`. The room enforces the mailbox match on console connect.
- **`DEFAULT_QUEUE = "helpdesk"` must stay identical** in console
  `chat_transport.py` and room `conversation.py`.
- **OIDC is gone.** Email‑verify is the only authenticated door; dev mode is
  ungated by design (keeps tests + local dev runnable). Don't re‑add `/login`,
  `/logout`, or `AuthConfig`.
- **`once_per_thread` fails open** (fires) when an event has no thread id — that's
  deliberate; don't "fix" it into fail‑closed.
- **Dev‑mode team is untrusted by design;** a verified server ignores the query
  `?team=` and trusts the session. Keep that asymmetry.
- **The room never sends mail or executes runnables** — it's a message pipe with
  presence. The console sends the verify mail (as the mailbox) and runs tools.

---

## 7. Doc reconciliation — what's stale

| Doc | State | What's now true |
|---|---|---|
| `teams-and-email-monitoring.md` | **Authoritative plan** | The locked model; this handover is its completed‑state companion |
| `console-triggers.md` | **Current** | Includes `once_per_thread` (§5) + the queued loop guard (§8.1) |
| `helpdesk-rooms.md` | **Stale** | Drop the `room_queue*`‑on‑host + multi‑queue model; single queue + team‑as‑tag |
| `email-queue-signup.md` | **Stale** | Sign‑up picks a **team**, not a queue; email‑verify is the only door (no OIDC) |
| `runnables-portal.md` | **Partly stale** | §6.6 queue‑is‑host‑`room_queue` is wrong; queue identity = the mailbox; teams are visitor tags |
| `console-room-server.md` | **Partly stale** | OIDC auth mode removed; `email-verify` / `dev` only |

Reconciling `helpdesk-rooms.md` + `email-queue-signup.md` to the single‑queue /
email‑only / team‑as‑tag model is the **F** task still open in the plan doc.

---

## 8. Backlog / parked (with rationale)

- **Scenario‑1 prefilter** *(parked — needs real examples)* — deterministic
  cruft‑strip (quoted‑history / signature) + per‑trigger regex on the inbound
  payload, *before* the LLM, to cut tokens on huge mail. Deterministic on purpose
  (an LLM prefilter still pays to read the big message).
- **Category‑stamp loop guard** *(queued — `console-triggers.md` §8.1)* — tag
  trigger‑sent mail with a `runspec` Outlook category + skip it in the source. A
  hard self‑send loop guard regardless of folder config, plus automated‑mail
  provenance. Deferred because it's Windows/Outlook‑COM only (needs a real Outlook
  box to verify); `once_per_thread` already neutralises the common loop.
- **v2 helpdesk** — operator **reassigns a visitor's team** (fix a mis‑tag);
  **reassign/escalate a ticket** (L1→L2); per‑team catalogues / skill‑based
  routing. The user/request records already carry `team`/`assignee`.
- **Email‑channel team default** *(open question)* — a `new_mail`‑originated
  request has no signed‑up visitor team: default to `Default`, or stay untagged
  until an operator sets it? Unresolved.
- **Doc F** — fold `helpdesk-rooms.md` + `email-queue-signup.md` into the single
  queue model (or mark them superseded).

---

## 9. How to run it (smoke)

1. Room (email‑verify): set `CONSOLE_ROOM_AUTH=email-verify`,
   `CONSOLE_ROOM_PUBLIC_URL`, a session secret, and `--mailbox helpdesk@corp.com`;
   start it on the helpdesk host.
2. Console: configure `[mailbox]` (same address), add a helpdesk host entry with
   `room_port` (+ `room`/`room_via`), and connect — you should see the room
   connected with runnables advertised.
3. Visitor: open the room URL → `/signup` → pick a team + verify → land in the
   helpdesk (no queue field, no second gate). Dev check:
   `/?user=alice&team=sales`.
4. Trigger: add one in Settings → Triggers against `outlook:new_mail`; toggle
   reply suppression and confirm a thread re‑reply doesn't re‑fire.
