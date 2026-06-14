# Changelog

All notable changes to `runspec-room` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
semantic versioning.

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
