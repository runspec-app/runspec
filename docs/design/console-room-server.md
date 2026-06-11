# Design: the room server (`packages/console-room/`)

**Status:** Server complete. Architecture locked (this doc). S1 (console door), S2
(browser door), and S3 (OIDC login + sessions + TLS) all built with tests. What
remains is the console-side integration (the `hello` frame, SSH-defaulted user,
and the Rooms view — §6).
**Scope:** The small web app the chat-room feature connects to — the *dumb
message pipe with presence* from `console-room-source.md` §1. All intelligence
stays in the consoles; the server never runs anything. It holds, per room, the
connected participants, their live presence, and a durable transcript, and it
does three jobs: accept connections, stamp + persist each inbound message, and
broadcast every message / presence change to everyone else in the room.

Pairs with `console-chat-room-source.md` (the console-side source) and
`console-chat-room-handoff.md` (phase notes). The console side through slice 3 is
already built; this is the other end of the wire.

---

## 1. Locked decisions

Settled in the resume session (one at a time — see the handoff). In short:

| # | Decision | Choice |
|---|---|---|
| 1 | Build a reference server? | **Yes** — it's the missing piece that makes the feature demoable |
| 2 | Home / stack | **Python**, `packages/console-room/` — deployment constraint: Python is available on the host, Node is not |
| 3 | Human access | **Public TLS + full login** (real web app, not SSH-only) |
| 4 | Identity backend | **OIDC / SSO** (Entra / Google / Okta …) — no passwords stored on the host |
| 5 | Console identity | **Client-declared, SSH-defaulted** — console sends its name; defaults to the SSH login (`alice@web01`) |
| 6 | Transcript | **SQLite** (stdlib) — durable; backscroll replays **to browsers only** |
| 7 | TLS | **uvicorn self-terminates** from a cert+key; `X-Forwarded-Proto`-aware for a future proxy |
| 8 | Fan-out | **Broadcast** to every on-shift console (v1) |
| 9 | Reply model | **Unchanged** — propose → confirm gate → composer → human sends |

**Stack (no Node anywhere, incl. CI):** Starlette + uvicorn (async websockets +
TLS), `authlib` (OIDC), `sqlite3` (stdlib), **vanilla static HTML/JS frontend with
no build step**. S1 (this slice) needs **zero runtime dependencies** — it's all
stdlib `asyncio` + `sqlite3` + `json`; the web deps land in S2/S3.

**Why a separate package can carry deps:** unlike the core `runspec` library
(stdlib-only), this is a side-car app, so Starlette/uvicorn/authlib are fine here
— same as `runspec-console` depending on pywebview.

---

## 2. Two front doors, one process

Two kinds of participant authenticate differently and frame differently, but meet
at the same hub and the same message schema:

| | Reaches the server by | Auth | Framing | Backscroll |
|---|---|---|---|---|
| **Console** | `direct-tcpip` over its pooled SSH connection → a **localhost** port on the host | **SSH access** (you can't open the tunnel without it) | **NDJSON** (one JSON object per line) | **No** — forward-only by design |
| **Browser (person)** | Public **wss + TLS** | **OIDC** session cookie | **WebSocket** JSON | **Yes** — recent history on load |

The asymmetry in the last column is load-bearing: a person loading the page wants
to see what they missed; a console must **never** receive messages that arrived
while it was away, or it would re-react to stale ones (the forward-only contract,
`console-triggers.md` §8/§11). So the server keeps a durable transcript, replays
the last *N* to **browsers on connect**, and sends **consoles the live stream
only**. Presence is purely live state (derived from open connections) and is
never persisted.

S1 built the **console (NDJSON-localhost) door**; S2 added the **browser door**
(both run in one process — the Starlette app's lifespan starts the NDJSON
listener). S3 adds OIDC + TLS around the browser door.

---

## 3. Wire protocol

Same schema both directions; the server is the authority on `id`/`ts`/`sender`.

### Console → server (NDJSON, one object per line)

```
{"type":"hello",   "room":"...", "user":"alice@web01"}   # FIRST frame, once, on connect
{"type":"message", "room":"...", "text":"..."}           # a human-sent message
{"type":"presence","room":"...", "status":"available|away"}  # connect / rota flip / stop
```

**The `hello` frame is how a console declares identity** (decision #5). Over
`direct-tcpip` the server sees only a localhost TCP connection — it cannot see the
OS user — so the console states it once, and the server **binds that name to the
connection** and stamps every later `message`/`presence` from it. Per-frame `user`
fields are ignored: identity is connection-scoped, not frame-scoped, so a frame
can't spoof a different sender. (Console-side: `ChatSource` must send `hello` on
connect — a small addition tracked as the first integration step in §6.)

### Server → client (NDJSON to consoles, WS JSON to browsers)

```
{"type":"message", "room":"...", "sender":"...", "text":"...", "id":"...", "ts":0.0}
{"type":"presence","room":"...", "user":"...",   "status":"available|away"}
```

The server **echoes** a sender's own message back to them (with the assigned
`id`/`ts`); a console drops its own echo by `sender == self_user`
(`ChatSource.parse_inbound`), so it never re-reacts to its own acks.

---

## 4. The hub (S1 core)

A transport-agnostic in-process broker. A **participant** is anything with
`.user`, `.room`, `.wants_backscroll`, and an async `send(frame)`; the NDJSON
console connection and (later) the WS browser connection both satisfy it, so the
hub has no transport knowledge.

- **join(p):** add to the room; send `p` the current presence of everyone already
  there; if `p.wants_backscroll`, replay the recent transcript to `p`
  (chronological); broadcast `p`'s `available` presence to the others.
- **leave(p):** remove; broadcast `p`'s `away` presence.
- **on_message(p, text):** stamp `id`+`ts`+`sender=p.user`, **persist**, broadcast
  to everyone in the room **including `p`** (the echo).
- **on_presence(p, status):** rebroadcast to the others.

A dead/slow connection can't break a broadcast — each `send` is guarded, and a
failed send just drops that recipient from the round.

**Transcript store** (`store.py`): a thin SQLite wrapper —
`messages(id PRIMARY KEY, room, sender, text, ts)` with a `(room, ts)` index;
`append(msg)` and `recent(room, limit)` (newest-N, returned chronological). Called
off the event loop via `asyncio.to_thread` so SQLite I/O never blocks the hub. The
hub depends on a tiny store protocol, so tests inject an in-memory fake and the
real SQLite store is tested on its own.

---

## 5. Layout

```
packages/console-room/
  pyproject.toml            # console-room script entry; web deps (lazy for --ndjson-only)
  README.md  CHANGELOG.md
  console_room/
    __init__.py             # version + public exports (Hub, TranscriptStore)
    models.py               # Message + frame builders / client-frame validation
    store.py                # SQLite transcript store (append / recent)
    hub.py                  # Hub + Participant protocol (transport-agnostic)
    ndjson_server.py        # asyncio NDJSON listener on localhost (console door)
    server.py               # wires store+hub+doors; CLI entry (main); TLS
    web/
      connection.py         # BrowserConnection (WebSocketLike seam, fake-testable)
      app.py                # Starlette app: /ws, OIDC routes, static, lifespan
      auth.py               # OIDC AuthConfig + identity helpers (authlib lazy)
      frontend/             # vanilla, no-build UI: index.html / app.js / style.css
  tests/
    test_models.py  test_store.py  test_hub.py  test_ndjson_server.py
    test_browser_connection.py  test_web_app.py  test_auth.py  test_auth_app.py
```

---

## 6. Slice plan

- **S1 — core hub (this slice).** Rooms, broadcast, NDJSON-localhost listener,
  SQLite transcript, live presence. No auth, no browser. Stdlib only. Tested with
  fake participants / a fed `StreamReader` (no real sockets, no SSH, no server
  process). **Makes consoles able to talk to the room over the tunnel.**
- **S2 — browser door ✅ *done*.** Starlette app (`web/app.py`): a `/ws` endpoint
  (the `BrowserConnection` participant, `wants_backscroll=True`) + the static
  vanilla frontend (transcript / composer / presence) + backscroll replay +
  `/healthz`. The app lifespan also runs the console NDJSON door, so one process
  serves both. Browser identity is a `?user=&room=` dev stand-in (replaced by the
  OIDC session in S3). Still no auth — dev/local until S3. Tested with a fake
  socket (`BrowserConnection`) and Starlette's `TestClient` (real ws round-trip),
  and verified by hand with a live cross-door round-trip (a browser and a console
  exchanging messages through the running server).
- **S3 — OIDC + sessions + TLS ✅ *done*.** `web/auth.py` (env-driven `AuthConfig`
  + pure identity helpers) + OIDC routes in `web/app.py` (`/login` → IdP via
  `authlib`, `/auth/callback` → signed-cookie session, `/logout`). The app shell
  and the WebSocket gate on `session_user`; a spoofed `?user=` is ignored. TLS via
  uvicorn `--tls-cert/--tls-key`, proxy-header-aware, `Secure` cookies on https.
  Falls back to dev mode (no gate) when OIDC isn't configured — which keeps local
  dev and the test-suite runnable without an IdP. Tested with the pure helpers and
  the full gate via `TestClient` with a stubbed token exchange (no live IdP).

**Integration steps (console side, when wiring end-to-end):**
1. ✅ *done* — `ChatSource` sends the `hello` frame on connect before any
   presence/message (`sources/chat.py._send_hello`); the server binds the declared
   name to the connection. Verified by a live console↔server interop round-trip.
2. ✅ *done* — `[chat] user` defaults to the SSH login when unset
   (`chat_transport.default_user_from_ssh` / `default_chat_user`, wired in the
   bridge's `_build_chat_source` where the `ssh_target` is known): a `user@host`
   target is used as-is, a bare host becomes `{os_user}@{host}`.
3. ✅ *done* — `console-ui` Rooms view (`views/RoomsView.tsx`) — transcript +
   presence + composer over `runspec:room_message` / `room_presence` /
   `room_draft`; composer send → `bridge.send_room_message`; connection/identity
   from `bridge.get_room_state`.

---

## 7. Out of scope / later

- Message editing, deletion, threading, reactions, typing indicators.
- Multiple server instances / horizontal scale (one process, one host for v1).
- Rate limiting and abuse controls beyond the SSH/OIDC gates.
- Claim-to-react fan-out (deferred; broadcast first — source design §7 q1).
