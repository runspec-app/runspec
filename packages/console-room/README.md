# runspec-room

The server behind the runspec-console **chat room** — a *dumb message pipe with
presence*. It holds, per room, the connected participants, their live presence,
and a durable transcript, and it does three things: accept connections, stamp +
persist each inbound message, and broadcast every message / presence change to
everyone else in the room. **All the intelligence stays in the consoles** — the
server never runs anything.

See `docs/design/console-room-server.md` for the full design and decisions.

## Status — complete server (S1–S3)

Two front doors, one process:

- **Console door** — an NDJSON listener on **localhost**, which each
  runspec-console reaches by opening a `direct-tcpip` channel over its existing
  pooled SSH connection. A console's right to be in the room *is* its SSH access —
  no new exposed port, no second credential.
- **Browser door** — a WebSocket endpoint (`/ws`) plus a small vanilla static
  frontend (transcript / composer / presence) with recent-history backscroll for
  people. Authenticated via **OIDC** (no passwords stored); identity comes from
  the signed-cookie session. TLS is self-terminated or left to a reverse proxy.

## Run it

```bash
pip install -e .

# Full server (browser + console doors):
console-room --port 8080 --ndjson-port 8765 --db ./console_room.db

# Console door only (no web deps needed):
console-room --ndjson-only --ndjson-port 8765 --db ./console_room.db
```

The console door stays on localhost — consoles tunnel in. Open the browser door
at the web port.

### Enabling login (OIDC) + TLS

Without OIDC config the browser door runs in **dev mode** — unauthenticated,
identity from a `?user=` join form (a warning is logged; don't expose it). Set the
OIDC env vars to require single sign-on:

```bash
export CONSOLE_ROOM_OIDC_ISSUER="https://login.microsoftonline.com/<tenant>/v2.0"
# or CONSOLE_ROOM_OIDC_METADATA_URL=<discovery doc URL>
export CONSOLE_ROOM_OIDC_CLIENT_ID="…"
export CONSOLE_ROOM_OIDC_CLIENT_SECRET="…"
export CONSOLE_ROOM_SESSION_SECRET="$(openssl rand -hex 32)"
export CONSOLE_ROOM_PUBLIC_URL="https://room.example.com"   # for the redirect_uri
# register  $CONSOLE_ROOM_PUBLIC_URL/auth/callback  as the redirect URI at your IdP

console-room --host 0.0.0.0 --port 443 \
  --tls-cert /etc/ssl/room.crt --tls-key /etc/ssl/room.key
```

uvicorn is proxy-header-aware (`X-Forwarded-Proto`), so you can drop `--tls-*` and
let nginx/Caddy terminate TLS instead.

## Run it as a service (no root)

A `systemd` **user** unit ships in [`deploy/console-room.service`](deploy/console-room.service)
— it runs as you, no root, surviving logout via `enable-linger`.

```bash
pip install --user runspec-room          # or into a venv (then fix ExecStart)

mkdir -p ~/.config/systemd/user
cp deploy/console-room.service ~/.config/systemd/user/

# config (optional) — the server reads CONSOLE_ROOM_* env:
mkdir -p ~/.config/console-room
cat > ~/.config/console-room/console-room.env <<'EOF'
CONSOLE_ROOM_NDJSON_PORT=8765
CONSOLE_ROOM_PORT=8090
# CONSOLE_ROOM_HOST=0.0.0.0          # expose the browser door (then set OIDC!)
# CONSOLE_ROOM_OIDC_ISSUER=...
EOF

systemctl --user daemon-reload
systemctl --user enable --now console-room
loginctl enable-linger "$USER"           # keep it running after you log out

systemctl --user status console-room
journalctl --user -u console-room -f     # logs
```

Edit `ExecStart` in the unit if `console-room` isn't at `~/.local/bin/console-room`
(e.g. a venv). To serve only the console NDJSON door, append `--ndjson-only` to
`ExecStart`.

## Wire protocol (NDJSON, one JSON object per line)

Console → server:

```json
{"type":"hello",   "room":"ops", "user":"alice@web01"}
{"type":"message", "room":"ops", "text":"rolling out"}
{"type":"presence","room":"ops", "status":"available"}
```

The **first** frame must be `hello` — that's how a console declares its identity
(the server can't see the OS user over the tunnel). The name is bound to the
connection and stamped onto every later message; per-frame `user` fields are
ignored, so a frame can't spoof a different sender.

Server → client:

```json
{"type":"message", "room":"ops", "sender":"alice@web01", "text":"rolling out", "id":"…", "ts":0.0}
{"type":"presence","room":"ops", "user":"alice@web01", "status":"available"}
```

The server echoes a sender's own message back (with the assigned `id`/`ts`); a
console drops its own echo so it never re-reacts to its own messages.

Browsers speak the **same `message` / `presence` schema** over the WebSocket — but
their identity comes from the **connection** (the `?user=&room=` query in dev, the
OIDC session in S3), so they send no `hello` frame.

## Develop

```bash
pip install -e ".[dev]"
pytest
```
