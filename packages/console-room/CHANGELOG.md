# Changelog

All notable changes to `runspec-room` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
semantic versioning.

## [0.26.0]

### Changed
- **Visitor browser frontend caught up to the single-queue + teams model.** The
  join page no longer asks for a help desk queue (one room = one helpdesk = one
  queue — the server names it) and no longer mentions OIDC / single sign-on (the
  email-verify door is the only auth mode). It now asks for the visitor's **team**
  alongside their name, mirroring the verified sign-up form. `/me` returns the
  signed-in `name` + `team` (verified mode) and the single `queue` so the client
  needs no queue input. Removed the stale `/logout` link (route gone with OIDC).
- **Verified visitors auto-join — no second gate.** After the verify link the
  client reads the confirmed name/team/queue from `/me` and enters the help desk
  straight away; the join form is for dev mode only (where the visitor declares
  their own name + team).
- **Dev mode looks like the verified experience.** The browser declares both the
  name (`?user=`) and the team (`?team=`) on the link (or types them); the team
  rides onto the operator card the same way the verified session's team does (a
  verified server ignores the query `?team=` and uses the session's, so it can't
  be spoofed).

## [0.25.0]

### Added
- **Teams-management protocol (Teams UI, step 1).** New agent→server frames
  `team_list` / `team_add` (by display name → slug) / `team_remove` (by slug),
  handled in `NdjsonConsoleConnection` and answered with a `teams` frame (the
  current list). Backed by `Hub.add_team` / `remove_team` (Default stays
  protected). Authorized by the SSH-tunnel door (any connected agent console may
  manage teams in v1). The console bridge + Teams settings tab follow.

## [0.24.0]

### Added
- **Visitor's team on the operator card (D2, slice 2c).** The team a visitor picks
  at sign-up now rides onto the request: stored on the `Request`, included in the
  `assignment` + `page` frames (`team`), and carried from the verified session onto
  the `BrowserConnection`. The operator's console shows it beside the visitor's
  name/email. New `session_team()` helper; `assignment_fields()` gains the team as
  its trailing field.

### Fixed
- Package description no longer references the removed OIDC door (email-verify).

## [0.23.0]

### Changed
- **Sign-up picks a team, not a queue (D2, slice 2).** One room = one helpdesk =
  one queue (`DEFAULT_QUEUE`, identity = the room's `--mailbox`), so `/signup` now
  shows a **team** (department) dropdown from the DB teams table (Default-seeded)
  instead of a queue list, and the domain allowlist / verify-from comes from the
  room's `--mailbox` (no longer per-advertised-queue). `/queues` → `/teams`. The
  visitor's chosen **team** rides signup → pending → verified member (new `team`
  column on `pending_signups` + `members`, migrated in place) and onto the session.
  `Hub.submit_signup(email, name, team)` enrols into the single queue;
  `Hub.list_teams()` backs the dropdown.

## [0.22.0]

### Added
- **Teams (customer departments) in the enrollment DB.** New `teams` table
  (`slug`, `name`) + CRUD on `EnrollmentStore` (`list_teams` / `get_team` /
  `add_team` upsert / `remove_team`), **seeded with a protected `Default`** so the
  sign-up team list is never empty (Default can't be removed). A `Team` dataclass
  and a pure `make_team_slug()` helper land in `signup.py`. This is the foundation
  for the single-queue collapse: a visitor picks a **team** (not a queue) at
  sign-up, and the team rides onto the operator's view. Not yet wired into the
  sign-up flow — that's the next slice. See docs/design/teams-and-email-monitoring.md.

## [0.21.0]

### Added
- **Mailbox tie — one email = one room.** New `--mailbox` arg (and
  `CONSOLE_ROOM_MAILBOX`) names the helpdesk mailbox a room serves. When set, the
  console NDJSON door **only accepts consoles configured for the same mailbox**: the
  `hello` handshake now carries the console's `[mailbox]` address (`Hello.mailbox`),
  and a connection whose mailbox doesn't match (or that sent none) is refused.
  Matching is case-insensitive; an empty room mailbox keeps the door open (dev).

## [0.20.0]

### Removed
- **OIDC auth — removed entirely.** Email sign-up (`CONSOLE_ROOM_AUTH=email-verify`)
  is now the **only** authenticated browser door; `dev` mode (`?user=`) remains as
  the unauthenticated local/test fallback. Dropped `web/auth.py`'s `AuthConfig` /
  `auth_from_env` / `identity_from_userinfo` / `build_oauth`, the `/login` +
  `/auth/callback` + `/logout` routes and the `auth=` parameter to `create_app`,
  the `oidc` auth mode, and the `authlib` dependency. The shared session helpers
  (`session_user` / `session_name`) stay. `read_auth_mode()` now **defaults to
  email-verify** when a session secret is set (else `dev`) — no OIDC inference. A
  future OIDC need is a separate build, not carried as parity here.

### Added
- **Email-verify outbox: periodic expiry sweep.** The web app now runs a background
  task (`Hub.run_signup_sweeper`, started by the app lifespan in email-verify mode,
  cancelled on shutdown) that every 5 minutes drops pending signups older than the
  verify-token TTL — so an abandoned or never-delivered signup can no longer
  accumulate in the SQLite outbox. `sent` rows are kept as the audit trail. No-op
  outside email-verify mode. (The `sweep_expired` store method existed since slice 1
  but was never driven.)

## [0.18.0]

### Added
- **Email-verified queue signup — visitor name on the operator card (slice 5 of
  5, final).** A request now carries the requester's verified **display name**
  alongside the email, so the operator sees `Alice Smith <alice@corp.com>` instead
  of a bare id (`docs/design/email-queue-signup.md` §5). The email-verify door
  already stored the self-asserted name in the session at verify time; this threads
  it through: `session_name()` → `BrowserConnection(display_name=…)` →
  `Request.visitor_name` → the `assignment` **and** `page` frames (new optional
  `visitor_name` field). Display-only — the verified email stays the trust anchor;
  blank (and the card falls back to the id) in OIDC / dev mode and for console
  participants.

## [0.17.0]

### Added
- **Email-verified queue signup — the room-side wire + outbox dispatch (slice 3 of
  5).** The hub now actually *delivers* verify mail: it dispatches each pending
  signup to a connected console of the queue (which sends it as the queue mailbox —
  the Outlook send is slice 4) and verifies the clicked link, with the durable
  outbox surviving a no-console gap. (`docs/design/email-queue-signup.md` §3.3/§7.)
  - **Three new console↔room frames** (`models.py`): `advertise` (console → room: a
    queue it staffs — mailbox + label), `send_verify` (room → console: "send this
    verify mail as the queue mailbox"), and `verify_sent` (console → room: ack). The
    NDJSON door dispatches the two inbound ones for agent connections.
  - **The outbox + verify now live on the `Hub`** (built with `enrollment=` +
    `email_verify=`): `submit_signup` writes a pending row and dispatches it;
    `on_advertise` registers a queue **and flushes its backlog** to the newly
    connected console (one console claims each send, so two can't double-fire);
    `on_verify_sent` flips the row to `sent` (or back to `pending` to retry);
    `verify_token` validates the signed single-use token and enrols the member. A
    console disconnecting **releases** its claimed-but-unacked sends and re-flushes
    them to a remaining console (`EnrollmentStore.release_console_claims`).
  - **The web door is now thin** — `/signup` and `/verify` delegate to the hub
    (`create_app` no longer takes `enrollment` / `verify_sender`; the slice-2
    placeholder seam is replaced by real dispatch).
- **Verify URL is now required in email-verify mode.** `CONSOLE_ROOM_PUBLIC_URL`
  (new `--public-url` flag) must be set — the verify link is emailed and clicked
  later, so it can't be derived from a request. The server **hard-fails at boot** if
  it's missing, and `create_app` rejects an email-verify app without it. (Used as the
  OIDC redirect base too.)

## [0.16.0]

### Added
- **Email-verified queue signup — the web sign-up door (slice 2 of 5).** The
  browser half of the `email-verify` auth mode (`docs/design/email-queue-signup.md`
  §3): an internal employee picks an advertised queue, gives their company email +
  full name, verifies the address, and is enrolled. Built on slice 1's storage +
  token helpers; platform-agnostic and exercised end-to-end by the TestClient.
  - **`email-verify` is now a real mode in `create_app`.** New
    `email_verify=` / `enrollment=` / `verify_sender=` parameters; OIDC and
    email-verify are mutually exclusive. Both authenticated modes now share **one**
    `session_user` gate (the app shell, `/ws`, and `/catalog` are gated whenever
    *either* is configured) and one `SessionMiddleware`, so a verified email-verify
    session unlocks the portal exactly as an OIDC login does. An unauthenticated
    visitor is redirected to `/signup` (vs `/login` for OIDC).
  - **Routes `GET /queues`, `GET|POST /signup`, `GET /verify`** (`web/signup_routes.py`).
    `/queues` returns the live advertised-queue list; `/signup` is a server-rendered,
    JS-free, self-contained HTML form (so it works without a build step and is
    testable); `POST` enforces the **domain allowlist** (email domain must equal the
    chosen queue's mailbox domain), writes the durable **outbox** row, and dispatches
    the verify mail via the `verify_sender` seam (the real Outlook send lands in
    slice 4 — until then signups queue in the outbox). `/verify` validates the signed,
    single-use token, burns the nonce, persists the `Member`, and sets the signed
    session cookie (email as identity, name carried for slice 5).
  - **Queue directory** (`queues.py` — `QueueDescriptor` / `QueueDirectory`): the
    live union of the queues connected consoles advertise (deduplicated, usable-domain
    only), wrapped by new `Hub.advertise_queue()` / `withdraw_queues()` /
    `advertised_queues()` / `queue_descriptor()`. The wire frame that feeds it from
    consoles arrives in slice 3; the web door reads it now.
  - **Server.** `run_web` selects the mode via `CONSOLE_ROOM_AUTH` (inferred for
    back-compat), constructing the `EnrollmentStore` (a sibling `…​.enrollment.db`,
    overridable with `CONSOLE_ROOM_ENROLL_DB`) for email-verify mode.

## [0.15.0]

### Added
- **Email-verified queue signup — storage + token helpers (slice 1 of 5).** The
  pure, platform-agnostic foundation of the new `email-verify` auth mode (see
  `docs/design/email-queue-signup.md`): an internal employee signs up for a queue
  with their company email + name, proves the address by clicking a verify link,
  and is enrolled — no IdP. This release lands the I/O-free pieces only; the web
  routes, wire frames, and Outlook send come in later slices.
  - `console_room/signup.py` — the pure half: `EmailVerifyConfig` +
    `read_auth_mode()` / `email_verify_from_env()` (the third `CONSOLE_ROOM_AUTH =
    email-verify | oidc | dev` startup mode, inferred for back-compat when unset);
    the automatic domain allowlist (`email_domain` / `domain_allowed` — a signup
    may only ever verify a *same-domain* address as the queue's own mailbox); and
    the `itsdangerous`-signed verify token (`sign_verify_token` /
    `read_verify_token`, TTL-checked, single-use via a burned nonce) plus the
    `PendingSignup` / `Member` dataclasses.
  - `console_room/enrollment_store.py` — `EnrollmentStore`, a SQLite store
    mirroring `TranscriptStore` (one connection, lock, `to_thread`-safe) with two
    tables: `members` (verified enrollments, upsert on `(email, queue)`) and
    `pending_signups` (the durable verify **outbox** + single-use nonce). Outbox
    lifecycle — `add_pending` → `claim_pending_sends(queue, console)` (atomic
    flush-on-connect, exclusive between consoles) → `mark_sent` / `release_claim`;
    `consume_pending` burns the nonce on verify; `sweep_expired` drops stale
    pendings (keeping `sent` rows as the audit trail).

## [0.14.0]

### Added
- **`--assignment-timeout` / `CONSOLE_ROOM_ASSIGNMENT_TIMEOUT`** on the server —
  the AFK release timeout is now configurable (was a hard-coded default). Seconds
  an assigned request may sit before a console picks it up (sends `request_start`)
  before it's auto-released to the queue + re-paged; **0 disables**. Default 300.
  Passed through to `Hub(assignment_timeout=…)` in both web and `--ndjson-only`
  modes.

## [0.13.0]

### Added
- **AFK release timer.** An assigned request whose console never sends
  `request_start` within `Hub(assignment_timeout=…)` seconds (default **300**) is
  auto-released back to the queue: re-paged, and re-assigned to a **different**
  holder (the AFK console is excluded so a stranded request isn't handed straight
  back). Cancelled the moment the request starts / completes / is released. Set
  `assignment_timeout=0` to disable. (Runnables portal §3.5 / §6.2.)

### Changed
- **Browser portal layout: catalogue cards on top, thread below.** The
  self-service catalogue is now a responsive **card grid across the top** of the
  page (≤ half the height, scrolls if long), like the console Forms tab, with the
  request thread filling the space beneath and the on-shift operators shown as a
  compact list in the header — replacing the cramped 180px side rail. The request
  form still opens as a centered modal (now overlaying the grid without a reflow).
- **Browser portal is help-desk-only by default.** The join form's group-chat
  **Room** field is hidden — a bare visit shows just the Help desk queue. Group
  chat isn't removed, only hidden: a legacy `?room=` deep-link still works (and
  re-reveals the Room field for that visit), so the change is reversible.

## [0.12.0]

### Changed
- **Browser portal: the visitor sees each request's claim-free lifecycle**
  (runnables portal, step 6). The web frontend drops the removed `conversation`
  lifecycle chip and instead renders, inline under each request message, its
  `request_status` (assigned · operator → in progress → done ✓ / failed). When a
  request is `unassigned` (no operator on shift) it shows **"no operator
  available"** plus a **Page the team** button that sends a `page` frame
  (re-chiming every connected console). The catalogue view + schema-generated
  request form are unchanged. See `docs/design/runnables-portal.md` §3.

## [0.11.0]

### Removed
- **The conversation claim lifecycle is retired** (runnables portal, step 7 — hub
  half). The portal's request lifecycle is claim-free, so the parallel
  `open → assigned → resolved → closed` thread machinery is gone:
  - Deleted `console_room/lifecycle.py` (`Conversation` / `ConversationRegistry`)
    and the `conversation` wire frame; removed `Hub.claim` / `resolve` / `reopen` /
    `close` / `conversation_state`, the `_convs` registry, and the conversation
    catch-up in `join` / `join_agent` and the open/reopen in `on_message`.
  - The NDJSON door no longer accepts `claim` / `resolve` / `reopen` / `close`
    control frames.
  - `on_message` in a helpdesk room is now just the clarification thread that hangs
    off a request; the request's own state travels in `request_status`.
  - The room-naming module (`conversation.py` — `helpdesk/<queue>/<visitor>` +
    namespace matching) is unchanged; only the claim *state machine* was removed.

## [0.10.0]

### Added
- **Inbound portal request-control frames (runnables portal, step 4 — transport).**
  The operator console can now drive a request's claim-free lifecycle over the
  NDJSON door: a `request_start` frame (`{id}`) calls `Hub.start_request`
  (→ `in_progress`) and a `request_complete` frame (`{id, error?}`) calls
  `Hub.complete_request` (→ `done` / `failed`). Both are agent-only and scoped to
  the assignee. This completes the wire contract the console drives end to end
  (assignment in → start/complete out).

## [0.9.0]

### Changed
- **The portal is now the single visitor path** (runnables portal cleanup). With
  no users of the previous design to preserve, the parallel legacy catalogue cache
  is gone and the browser door drives the portal directly:
  - Removed `Hub.set_catalog` / `Hub.catalog_for` (the namespace-level
    last-writer-wins cache) and `Hub.visitor_request`. The presence-aware live
    catalogue is the only catalogue; `submit_request` is the only request path.
  - The NDJSON `catalog` frame feeds the live catalogue only; the web `/catalog`
    endpoint serves `Hub.live_catalog` (the on-shift union) — same `{queue, tools}`
    shape.
  - The browser `request` frame now calls `submit_request` (validate → auto-assign
    or hold + page); a new inbound `page` frame calls `page_request` (the visitor's
    "Page the team" action).

### Note
- The free-chat **conversation claim lifecycle** (`claim` / `resolve` / `reopen` /
  `close` + the `conversation` frames) still exists — it governs free chat threads,
  not the portal request flow, and its removal is the dedicated claim-retirement
  step (§7 steps 5/7). The served frontend ignores the new `request_status` /
  `page` frames until the browser portal is rebuilt (step 6).

## [0.8.0]

### Added
- **Page / chime broadcast (runnables portal, step 3).** When a request can't be
  auto-assigned — no holder is on shift — the hub now **pages the team**: a `page`
  frame chimes *every connected console of the queue* (on shift and off), naming
  the runnable and carrying the thread room, so a holder can come on shift to pick
  it up (the pending request then auto-assigns) or respond out of band (see
  `docs/design/runnables-portal.md` §3.4). Pages fire automatically when a request
  becomes `unassigned` — on submit with no on-shift holder, and on the AFK
  `release` when it can't be re-assigned — and the visitor can re-chime explicitly
  via `Hub.page_request` ("Page the team"), which is refused once the request is
  assigned/done (only something actually waiting can be paged).
  - New `page` wire frame (`console_room/models.py`).
  - `Hub._page` (broadcast to the queue's connected consoles), `_hold_unassigned`
    (status + page), and `page_request` (the visitor action).

## [0.7.0]

### Added
- **Auto-assignment + claim-free request lifecycle (runnables portal, step 2).**
  A visitor's portal request is now **routed to an operator by the hub**, not
  claimed by a human (see `docs/design/runnables-portal.md` §3.3 / §3.5). On
  submit the hub validates the runnable against the live catalogue, then
  auto-assigns it to an on-shift holder by **plain round-robin** (a single holder
  always gets it); when no holder is on shift it accepts the request as
  `unassigned` and drains it the moment an eligible operator comes on shift — no
  re-submit. The request walks a small claim-free state machine
  (`unassigned → assigned → in_progress → done` / `failed`, with `release` back to
  `unassigned` for the AFK timeout).
  - New pure `console_room/requests.py` (`Request` + `RequestRegistry`): the
    transition rules and the per-namespace round-robin cursor.
  - Hub methods: `submit_request` (validate + persist the thread message +
    auto-assign or hold), `start_request`, `complete_request`, `release_request`,
    `request_state`; `set_agent_shift` is now async and drains the backlog when an
    operator comes on shift.
  - New wire frames (`console_room/models.py`): `assignment` (server → the
    assigned operator's inbox, carrying the runnable + args + thread room) and
    `request_status` (server → thread, the lifecycle state + assignee).
  - Validation tolerates the off-shift race: a runnable some connected console
    holds is accepted (→ `unassigned` + pageable) even if every holder is
    momentarily off shift; one no connected console holds is refused.

## [0.6.0]

### Added
- **Presence-aware live catalogue + affinity (runnables portal, step 1).** The
  hub now tracks the self-service catalogue **per connected console** and whether
  that console is **on shift**, instead of one last-writer-wins list per queue.
  This is the spine of the runnables portal (see
  `docs/design/runnables-portal.md` §3.2): the catalogue a visitor browses is the
  **live union** of the on-shift consoles' published runnables, and every runnable
  carries **affinity** — which consoles hold it — so a request can be routed to a
  console that can actually serve it. A runnable only one on-shift console holds
  routes to that console and **vanishes** the moment it goes off shift (or
  disconnects), reappearing when it returns.
  - New pure `console_room/catalog.py` (`LiveCatalog`): per-namespace, per-agent
    publish / presence / withdraw, answering `live(namespace)`,
    `holders(namespace, tool)` (on-shift routing candidates), and
    `connected_holders(...)` (the wider on-and-off-shift set a page will target).
  - Hub methods: `publish_catalog`, `set_agent_shift`, `live_catalog`,
    `runnable_holders`, `connected_holders`; `join_agent` marks a console on shift
    by default and `leave_agent` withdraws it.
  - The NDJSON `catalog` frame now feeds the live catalogue alongside the legacy
    `set_catalog` cache (the latter still backs the web `/catalog` endpoint and is
    subsumed in a later step); an agent `presence` frame flips its on-shift state
    without dropping the connection (off-shift-but-connected).

## [0.5.3]

### Changed
- **WebSocket rejects now log an actionable diagnostic.** A `/ws` handshake that
  fails an auth/identity check closes *before* `accept()`, which the ASGI server
  turns into a bare **HTTP 403** at the browser — the custom close code (4401/
  4000) never reaches it, so the browser can't say *why*. Each reject branch now
  logs one line naming the cause and a handshake summary (`client`, `origin`,
  `cookie-header`, `session-cookie`). The cookie fields are the tell for the
  common "login works over HTTP but the WebSocket 403s" case: a reverse proxy
  that forwards `Cookie` on normal requests but drops it on the `Upgrade` request
  leaves the WS with no session (`cookie-header=no`). No behaviour change — the
  same connections are accepted/rejected as before, just traceable now.

## [0.5.2]

### Fixed
- **Catalog request form fits a many-arg runnable.** The visitor form rendered
  inside the 180px catalog rail, so a runnable with lots of arguments overflowed
  (no horizontal scroll) and was unusable. When opened, the form now lifts into a
  centered modal (dimmed backdrop) and lays its fields out in responsive columns,
  so a wide form has room. CSS-only — the open/close is still the existing
  `reqForm.hidden` toggle.

## [0.5.1]

### Changed
- **Visitor join form adapts to the link.** Arriving via a help-desk link
  (`?queue=it-support`) now shows only the queue — the **Room** field (group
  chat) is hidden, so a visitor isn't asked for both. A `?room=` link shows only
  Room; a bare URL shows both with a one-line "queue *or* room, not both" hint.
  (They were always alternatives — the queue wins when both are set — but the
  form didn't say so.)

## [0.5.0]

### Added — helpdesk mode (private conversations + self-service catalog)

The room server gains a second routing mode built on the same hub, store,
transports and OIDC door: a corporate **help desk** where each signed-in
visitor has a **private conversation** with a team of agent consoles. Group
chat is unchanged throughout — a room with no agents behaves exactly as
before. See `docs/design/helpdesk-rooms.md`.

- **Private conversations.** A conversation is a room named **server-side** as
  `helpdesk/<queue>/<visitor>` from the OIDC session — a visitor can never name
  (and so never read) another visitor's thread. Browser door: `?queue=` joins
  your own thread; `?room=` group chat is untouched.
- **Namespace agents.** A console's `hello` may declare a `namespace`
  (`helpdesk/<queue>`) instead of a `room`: the hub fans every conversation
  under the queue to it (the agent's inbox), present and future. Agent posts
  name their target conversation and are refused outside the namespace.
- **Conversation lifecycle.** `open → assigned → resolved → closed` (+ reopen
  by visitor reply), driven by agent-only `claim` / `resolve` / `reopen` /
  `close` control frames; every change broadcasts a `conversation` state frame
  to the visitor + team, and join/subscribe catch-up replays current state.
- **Self-service catalog.** Agent consoles publish a `catalog` frame (their
  `self-service`-marked runnable schemas); the room caches per queue
  (last-writer-wins, `[]` clears) and serves visitors via `GET /catalog?queue=`
  (auth-gated). **The room never executes anything**: a visitor form submit
  becomes a `request`-kind message (human text + `{tool, args}` payload),
  validated against the published catalog + size caps — enactment happens only
  on an agent console behind its speak/autonomy gates.
- **Visitor web UI.** A "Help desk queue" join option + `?queue=` deep links,
  an always-browsable catalog panel with forms generated from each tool's
  `inputSchema`, a lifecycle chip, and request cards in the transcript.

### Changed
- `Message` gains `kind` (`chat`|`request`) + `data` — additive on the wire;
  the SQLite store adds the two columns and migrates a pre-helpdesk transcript
  in place.

## [0.4.1]

### Fixed
- **Browser door 500'd on `/me` in dev mode.** With no OIDC configured there's no
  `SessionMiddleware`, and Starlette's `request.session` *asserts* it's installed
  (raising `AssertionError`, which the `getattr` guard didn't catch). `session_user`
  now guards the access, so the dev-mode web UI loads.
- Added a `/favicon.ico` route (204 No Content) to silence the browser's 404.
- **Browser UI showed the join form and the chat panel at the same time.** `.join`
  and `#app` set `display: flex`, which overrode the `hidden` attribute the JS
  toggles — so neither ever hid. Added `[hidden] { display: none !important }`.

### Added
- `deploy/console-room.service` — a `systemd` **user** unit (no root) + README
  steps to run the server as a managed daemon (`systemctl --user` + `enable-linger`).

## [0.4.0]

### Added
- **`console-room connect` — the exec-channel relay.** A subcommand that connects
  to the room server's local NDJSON port and pipes it over stdin/stdout. It lets a
  console join a room **without SSH port-forwarding**: instead of opening a
  `direct-tcpip` tunnel, the console runs `console-room connect --port 8765` on the
  host over a normal SSH *exec* channel (which works even when `AllowTcpForwarding`
  is off or a network appliance resets tunnels). Stdout carries only the relayed
  bytes; diagnostics go to stderr. See the console's `room_via = "exec"` host
  setting.

## [0.3.0]

**S3: OIDC login + sessions + TLS.** The browser door is now authenticated.

### Added
- `console_room.web.auth` — OIDC config (`auth_from_env`, env-driven, secrets
  never on the CLI) and pure identity helpers (`identity_from_userinfo`,
  `session_user`, `AuthConfig.redirect_uri` / `.secure_cookies`).
- OIDC login on the browser door (authlib): `/login` → your IdP, `/auth/callback`
  → a signed-cookie session, `/logout`. The app shell (`/`) and the WebSocket are
  gated on the session identity, so the browser no longer declares who it is —
  and a spoofed `?user=` is ignored. A `/me` endpoint reports the logged-in user;
  the frontend locks the name field and shows a sign-out link in auth mode.
- TLS: `--tls-cert` / `--tls-key` (or `CONSOLE_ROOM_TLS_*`) make uvicorn serve
  https/wss directly. uvicorn runs proxy-header-aware (`X-Forwarded-Proto`), so it
  also works behind a TLS-terminating reverse proxy; session cookies are marked
  `Secure` when the public URL is https.

### Changed
- Adds runtime dependencies `authlib` + `itsdangerous`. **Dev mode unchanged:**
  with no `CONSOLE_ROOM_OIDC_*` config the browser door runs unauthenticated
  (query-param identity), logging a warning — handy for local use, not for public
  exposure. `--ndjson-only` still needs none of the web deps.
- Static assets moved to `/static/`; the app shell is served from a gated `/`
  route (was a root static mount).

## [0.2.0]

**S2: the browser front door.**

### Added
- `console_room.web.connection.BrowserConnection` — the browser counterpart of the
  NDJSON console connection, satisfying the hub's `Participant` protocol with
  `wants_backscroll = True` (people see recent history; consoles never do). Speaks
  to a tiny `WebSocketLike` seam, so it's unit-tested with a fake socket.
- `console_room.web.app.create_app` — the Starlette app: a `/ws` WebSocket door, a
  `/healthz` check, and the static frontend served at `/`. When given an
  `ndjson_addr`, its lifespan also runs the console NDJSON door on the same loop,
  so one process serves both front doors.
- A vanilla, no-build static frontend (`web/frontend/`): transcript + presence +
  composer, with auto-reconnect. Browser identity comes from a `?user=&room=` join
  form (a dev stand-in, replaced by the OIDC session in S3).
- `server.py` now runs the **web server** by default (uvicorn + the app);
  `--ndjson-only` keeps the stdlib console-door path for a minimal deploy.

### Changed
- Adds runtime dependencies `starlette` + `uvicorn[standard]` (the package is now a
  web server). The `--ndjson-only` path imports neither — they're lazy — so a
  console-only deploy can still run without them.

## [0.1.0]

Initial release — **S1: the console front door**.

### Added
- `console_room.hub.Hub` — the transport-agnostic room broker: join/leave with
  live presence, message stamping (server-assigned `id`/`ts`/`sender`), persist,
  and broadcast (sender included, so all views converge). Browser-only backscroll
  asymmetry baked in via the participant's `wants_backscroll`.
- `console_room.store.TranscriptStore` — durable SQLite transcript (`append` /
  `recent`), stdlib-only, idempotent on message id.
- `console_room.ndjson_server` — the NDJSON listener on localhost (the console
  door): a `hello` handshake binds identity to the connection; `message` /
  `presence` frames drive the hub; blank/malformed lines are skipped.
- `console_room.models` — the wire schema: `Message`, frame builders, and
  `hello` parsing/validation.
- `console-room` CLI entry point (`--host` / `--port` / `--db`, env-overridable).

Stdlib-only at this stage (no runtime dependencies). The browser door (S2) and
OIDC + TLS (S3) follow. See `docs/design/console-room-server.md`.
