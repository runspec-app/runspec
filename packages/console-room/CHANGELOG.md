# Changelog

All notable changes to `runspec-room` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
semantic versioning.

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
