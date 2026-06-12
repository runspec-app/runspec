# Design: helpdesk rooms — private conversations + a runspec self-service catalog

**Status:** Design agreed (this note). No code yet. Supersedes the "helpdesk on
rooms" open idea in `console-room-next-session.md` §1 — the decisions below are
the resolution of that "discuss, don't build yet" item.
**Scope:** A second mode for `runspec-room` (`packages/console-room/`): a
corporate help desk built on the existing hub, where each signed-in user has a
**private** conversation with the helpdesk team — not a shared room. Pairs with
a small **spec change** (a `discoverable` keyword) that curates which runnables
a visitor may request.

This is a *layer*, not a pivot. The group-chat room (`console-chat-room-source.md`)
keeps working unchanged; helpdesk is a second routing mode over the same hub,
store, transport, and OIDC door.

---

## 1. Why private conversations (not a room)

The original room model broadcasts every message to everyone sharing a `room`.
For an on-call coordination channel that's correct. For a corporate help desk it
is wrong on three counts the user named, plus a fourth:

1. **Confusion / attribution** — in a shared room you can't cleanly tell who
   asked what. A per-user thread makes "who asked what" the *primary key*.
2. **Cross-department leakage** — Finance and HR must not see each other's
   requests. A shared room leaks by construction; private threads can't.
3. **Corporate sell** — "every employee gets a private support thread" passes a
   data-segregation review trivially; "everyone's in one chat room" does not.
4. **Cleaner primitive** — it's the *same hub* with the scoping key changed (§3),
   so we gain a product without new machinery.

---

## 2. The two front doors already encode the two roles

The decisive simplification: `runspec-room`'s existing two-door architecture
*already* separates visitor from agent. We do **not** need the "identity flip"
the earlier handoff worried about — the doors give it to us for free.

| Door (existing) | Was | Now (helpdesk mode) | Auth (unchanged) |
|---|---|---|---|
| **Browser (OIDC + TLS)** | people in a room | **Visitor** — one private conversation, their own | Corporate SSO session cookie |
| **Console (NDJSON over SSH tunnel / exec relay)** | a console in a room | **Agent** — team-scoped, sees every conversation in the queue | SSH access *is* the right to staff the desk |

**Agent side is consoles only** (decided). This keeps a hard cut between the two
populations: a browser user is *always* a visitor; an agent is *always* a
runspec-console reached over SSH. No human "browser agent inbox" in v1 — the
agent surface is the console's Rooms/inbox view, and the console's existing
**speak** (confirm-gated draft → human sends) and **act** (autonomy gate) gates
apply to every reply and every enacted runnable. (A browser agent inbox is a
later option; the cut makes it additive, not a rewrite.)

---

## 3. The model: a conversation *is* a room keyed by the visitor

The hub already scopes everything by a `room` string. Helpdesk keeps that
mechanism and changes only **what the key is** and **how agents subscribe**:

- A **conversation** is a room whose name is derived from the visitor's SSO
  identity and the queue, e.g. `helpdesk/it-support/alice@corp`.
- A **visitor** joins exactly one room — their own. The WebSocket is already
  gated on `session_user` (S3), so the server *constructs* the room name from the
  session and never honours a client-supplied one. A visitor therefore **cannot**
  address or subscribe to anyone else's conversation. Isolation is enforced by the
  auth layer you already shipped, not by convention.
- An **agent** (console) subscribes to a **queue namespace** (`helpdesk/it-support/*`),
  not a single conversation. The hub fans every conversation in that namespace to
  the agent — that's the agent's *inbox of threads*. This is the one genuinely
  new hub capability: a **namespace/wildcard participant**.

Fan-out becomes asymmetric, which is what makes it a help desk rather than a room:

- **Visitor → team.** A visitor message goes to every on-shift agent of the queue
  (plus the echo back to the visitor). It does **not** go to other visitors.
- **Agent → visitor (+ team).** An agent reply goes to that one visitor and to the
  other agents of the queue (so the team sees who is handling it). It never
  reaches a different visitor.

Backscroll stays as designed and now means something stronger: a **visitor**
reloading the page replays *their own* transcript only (browsers get backscroll);
an **agent** console is forward-only (no backscroll) — it reacts to live messages
and reads history from the store/inbox view, never re-reacting to stale ones.

### Why this is minimal

The store (`messages(id, room, sender, text, ts)`) already partitions by `room`,
so per-conversation transcripts and per-visitor privacy fall out of the existing
schema for free. The new pieces are bounded:

1. **Namespace participant** in the hub — an agent whose `room` is a prefix glob;
   `join`/`broadcast` match by prefix. (Today membership is exact-string.)
2. **Conversation lifecycle** state (§4), stored per room.
3. **Server-side room naming** from the OIDC session + chosen queue (visitors
   never name their own room).
4. The **self-service catalog** (§5) and the `discoverable` keyword (§6).

---

## 4. Conversation lifecycle

Each conversation carries a state. The "claim-to-react" efficiency idea
(deferred from the room design's §7) returns here as the **assignment** step —
broadcast presence/notice to the team, but defer any *agent LLM turn* until an
agent claims the thread, tying spend to a human claim.

```
        visitor opens / first message
                 │
                 ▼
            ┌─────────┐   agent claims   ┌──────────┐  agent marks resolved  ┌───────────┐
   new ───▶ │  open   │ ───────────────▶ │ assigned │ ─────────────────────▶ │ resolved  │
            └─────────┘                  └──────────┘                        └───────────┘
                 ▲                            │                                   │
                 │      visitor replies       │       visitor replies (reopen)    │
                 └────────────────────────────┴───────────────────────────────────┘
                                                                                   │ timeout / agent close
                                                                                   ▼
                                                                              ┌──────────┐
                                                                              │  closed  │
                                                                              └──────────┘
```

- **open** — visitor has an active thread; unassigned. Broadcast to the queue's
  on-shift agents (presence/notice only — no agent turn yet).
- **assigned** — an agent claimed it (`claim`). Now *this* agent's console may run
  its triage/draft turn (speak checkpoint). Other agents see it's taken.
- **resolved** — agent marked it done. Visitor can **reopen** by replying (→ open),
  so they're never stuck.
- **closed** — terminal (agent close, or auto-close after a resolved-idle window).
  Transcript is retained for audit; a new visitor message starts a fresh thread.

One **active** conversation per visitor per queue in v1 (keeps the model legible);
historical/closed threads accumulate in the store and the visitor's backscroll.

---

## 5. The catalog: "use runspec for arguments"

The other half of the user's brief — *make the help desk use runspec for
arguments* — is where this stops being "just chat" and becomes uniquely runspec.

A visitor's request can be backed by a **structured runnable with runspec-validated
args**, not free prose. The queue advertises a **self-service catalog**: the set
of runnables a visitor is allowed to request (e.g. *Reset my password*, *Request a
VM* with `size` / `region` / `justification`, *Install software*). The visitor
picks one, the console UI renders it as a **Form** (the console already has Forms),
runspec validates the args, and the submission lands in the visitor's conversation
as a structured request the agent console can enact through the **autonomy gate**.

So a help-desk interaction is:

> **private conversation** (chat for the human part) **+ a curated, runspec-validated
> catalog** (Forms for the structured part) **→ agent console enacts via speak +
> autonomy gates.**

The catalog is *curated*: not every installed runnable should be visitor-facing.
That curation is exactly what the new `discoverable` keyword provides (§6).

---

## 6. Spec change: the `discoverable` keyword

**Decided shape: a polymorphic, audience-scoped, runnable-level field.** It sits
beside `autonomy` / `output` in a runnable section.

```toml
[reset-password]
description  = "Reset the caller's AD password"
discoverable = ["operator", "self-service"]   # in the visitor catalog AND operator surfaces

[rotate-root-key]
description  = "Rotate the cluster root key"
discoverable = false                            # hidden everywhere

[list-tickets]
description  = "List open tickets"
# absent ≡ true ≡ ["operator"] — operator-visible, NOT in the visitor catalog
```

Semantics:

| Value | Meaning |
|---|---|
| absent / `true` | `["operator"]` — visible to operator surfaces (`runspec local` / `serve`, the console). **Not** visitor-facing. |
| `false` | `[]` — hidden from all discovery (an internal helper). |
| list of audiences | Visible to exactly the named audiences. Known audiences v1: `"operator"`, `"self-service"`. |

Two load-bearing defaults, both matching runspec's "safe by default, opt-in to
trust" posture:

1. **Operator-visible by default** (absent ≡ `true`) so the change is fully
   backward compatible — every existing package (linux/windows/jsm/snow/…) keeps
   surfacing in `runspec serve`/`local` with no edits.
2. **The visitor catalog is opt-in / default-deny** — a runnable reaches the
   `self-service` audience **only** by explicitly listing it. A package can never
   leak a runnable to visitors by accident; the bare `true` shorthand deliberately
   does *not* imply `self-service`.

This also fills a gap that exists independent of helpdesk: today discovery is
**all-or-nothing per package** (install a dist that requires `runspec` → *every*
runnable in it surfaces in `serve`/`local`). `discoverable = false` lets one
package hold both a visitor-facing "reset password" and an internal "rotate key"
and hide the latter — useful on its own, shippable before any helpdesk code.

### Spec-change mechanics (must move together — CLAUDE.md / `docs-check`)

Pure metadata, **no runtime dependency** added to the core; the parser just
surfaces it on the `RunSpec` and emitters carry it. The change touches, in one
commit, per the docs-drift guard:

1. `spec/SPEC.md` — document `discoverable` under *Runnable Definition* (beside
   `autonomy`), the audience semantics, and the two defaults.
2. `schema/runspec.schema.json` — add `discoverable` to `$defs/runnable`:
   `oneOf` boolean **or** `array` of an `audience` enum (`["operator","self-service"]`),
   `default: true`.
3. `docs/format.md` — add the row to the *Runnable definition* table (+ a short
   subsection like `### discoverable`).
4. **Python** (`models.py` field on `RunSpec`; `loader.py`/`inference.py` normalise
   bool→audience-list; surface in `cli.py local --format mcp` / emitters as an
   `x-discoverable` / audience hint), **Node** at parity (`models.ts`, `loader.ts`,
   `cli.ts`), shared compliance fixtures in `tests/integration/fixtures/`.
5. `CHANGELOG.md` entries for the `runspec` + `runspec-node` version bumps
   (`## [x]` / `## [node-x]`) — required or `docs-check` version-sync fails.

`runspec serve`/`local` filter to `operator` (unchanged behaviour, since that's
the default); the helpdesk catalog filters to `self-service`. Discovery still
reads `importlib.metadata` only — `discoverable` narrows *within* an installed
package; it does not re-introduce filesystem scanning (CLAUDE.md invariant intact).

---

## 7. Identity & privacy posture

- **Visitor identity is SSO (OIDC), not anonymous.** This strengthens the
  "track who asked what" goal — every message and request is attributable to a
  corporate identity — and keeps the room name server-derived and unspoofable.
- **Agent identity is SSH-gated**, as today (the console declares a name via the
  `hello` frame; staffing the desk *is* having SSH to the host).
- **Isolation** is enforced at the door: the WS gate builds the room from the
  session, so a visitor literally cannot read another visitor's thread. No shared
  room ⇒ no cross-department leakage path.
- **Audit** — the SQLite transcript already persists every message per room;
  per-visitor threads make the audit trail naturally per-person. Catalog
  submissions are runspec runs, so they additionally land in the runspec audit log
  (the run-summary record).
- **Trust** — inbound visitor text is untrusted LLM input on the agent side;
  backstopped by the console's `match` pre-filter, the speak checkpoint (human
  sends), and the autonomy gate (human approves enactment). Same posture as the
  existing chat/Outlook sources.

---

## 8. What's reused vs new

**Reused unchanged:** the hub broadcast core, the SQLite transcript store, the
SSH-tunnel + exec-relay console transport, the OIDC/TLS browser door, presence,
the console's speak + autonomy gates, and the Forms surface.

**New:**
1. `discoverable` spec keyword (§6) — independently useful; ship first.
2. Hub **namespace/prefix participant** (agent subscribes to a queue, not a room).
3. Server-side **conversation naming** from the OIDC session + queue choice.
4. **Lifecycle** state per conversation (§4) + the `claim` / `resolve` / `reopen` /
   `close` control frames.
5. Visitor **catalog** endpoint (the `self-service` runnables for the queue) and
   the **request → Form → structured message** flow.
6. UI: a visitor browser view (one thread + catalog/Forms) and the agent-console
   **inbox** view (many threads, claim/resolve).

---

## 9. Slice plan

- **H0 — `discoverable` keyword. ✅ done.** Spec + schema + format.md + Python/Node
  parity + fixtures + CHANGELOG; per-runnable visibility. *Foundation.* (`runspec`
  0.34.0 / `runspec-node` 0.30.0, PR #198.)
- **H1 — hub namespace participant + conversation naming. ✅ done.** `conversation.py`
  (pure naming/matching: `helpdesk/<queue>/<visitor>`, segment-boundary namespace
  match, token validation). Hub gains **agents** (subscribe to a queue namespace,
  fanned every room under it via a prefix-matched recipient merge) alongside exact
  **members** — group-chat unchanged; `join_agent` / `leave_agent` /
  `agent_message` (namespace-refused). Doors wired: the NDJSON `hello` gains an
  optional `namespace` (agent mode); the browser door derives a visitor's room
  server-side from `?queue=` + identity. Tested with fakes / StreamReaders /
  TestClient.
- **H2 — lifecycle + control frames. ✅ done.** `lifecycle.py` (pure state machine:
  open→assigned→resolved→closed, reopen; `ConversationRegistry` with a
  change-vs-no-op return contract). Hub drives it — a visitor message opens /
  reopens a thread; agent-only `claim`/`resolve`/`reopen`/`close` (namespace-scoped)
  move it on and broadcast a `conversation` state frame to the visitor + team;
  join/subscribe catch-up replays current state. NDJSON control frames wired
  (agents only). `claim` is the gate a console uses to release the deferred agent
  turn. Tested at the state-machine, hub, and wire levels.
- **H3 — visitor browser view + catalog/Forms.** The `self-service` catalog for a
  queue; pick → Form → structured request into the thread.
- **H4 — agent-console inbox view.** Many-threads inbox, claim/resolve, drafts via
  the existing speak checkpoint.

H0 shipped first (cleanest, most reusable, unblocks the catalog); H1 is the
server-side spine the rest hangs off; H2 gives each thread a lifecycle. **Next: H3**
— the visitor browser view + the `self-service` runspec catalog (Forms → structured
request into the thread), where `discoverable` (H0) and the conversation (H1/H2)
finally meet the UI.

---

## 10. Open questions (for later)

1. **Queue routing.** Single helpdesk queue vs multiple departmental queues, and
   whether a visitor's department auto-routes them or they pick a queue. The
   leakage concern leans multi-queue; H1's namespace design already allows it
   (`helpdesk/<queue>/<visitor>`), so ship single, design for many.
2. **Catalog source of truth.** Per-queue config listing the `self-service`
   runnables, vs every `self-service`-marked runnable installed on the agent hosts.
   Likely the former (explicit per-queue allowlist) for a tight blast radius.
3. **SLA / notifications.** Out of scope for the first cut; the lifecycle gives the
   hooks (e.g. resolved-idle auto-close, unassigned-too-long nudge) for later.
4. **Browser agent inbox.** Deferred by the "consoles only" cut; additive when wanted.
