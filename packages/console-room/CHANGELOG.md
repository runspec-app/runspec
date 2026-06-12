# Changelog

All notable changes to `runspec-room` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
semantic versioning.

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
