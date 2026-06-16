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

### Enabling login (email link) + TLS

Without a session secret the browser door runs in **dev mode** — unauthenticated,
identity from a `?user=` join form (a warning is logged; don't expose it). With one,
the door runs **email-verify login**: an internal employee verifies a company email
(the link is sent by a connected console as the queue mailbox), and the signed
session that grants access **expires** after `session_ttl` (default 24h) — they log
back in the same way (the email is remembered in a cookie, so a return visit is one
click). See [`docs/design/email-queue-signup.md`](../../docs/design/email-queue-signup.md).

```bash
export CONSOLE_ROOM_SESSION_SECRET="$(openssl rand -hex 32)"  # signs cookie + token
export CONSOLE_ROOM_PUBLIC_URL="https://room.example.com"     # login links point here
export CONSOLE_ROOM_MAILBOX="helpdesk@corp.com"               # one email = one room
# export CONSOLE_ROOM_SESSION_TTL=86400   # access lifetime before re-login (s)
# export CONSOLE_ROOM_VERIFY_TTL=1800     # login-link lifetime (s)

console-room --host 0.0.0.0 --port 443 \
  --tls-cert /etc/ssl/room.crt --tls-key /etc/ssl/room.key
```

uvicorn is proxy-header-aware (`X-Forwarded-Proto`), so you can drop `--tls-*` and
let nginx/Caddy terminate TLS instead.

### Config file (deploy from one file)

Everything above can live in a TOML file instead — handy for Ansible. Point at it
with `--config` (or `CONSOLE_ROOM_CONFIG`, or drop a `console_room.toml` in the
working dir). Precedence is **defaults < config file < env var < CLI flag**, so a
flag or env var always overrides the file. The session secret can be a **command**
(its stdout is the secret) so it never sits in the committed file, and `[[teams]]`
is the **authoritative** team list, synced into the room on every boot — a fleet of
rooms deploying the same file converges on the same teams.

```toml
[server]
host = "0.0.0.0"
port = 443
ndjson_host = "127.0.0.1"
ndjson_port = 8765
public_url = "https://room.example.com"   # required for email login
mailbox = "helpdesk@corp.com"             # one email = one room
db = "/var/lib/runspec-room/console_room.db"
tls_cert = "/etc/ssl/room/fullchain.pem"  # omit if a proxy terminates TLS
tls_key  = "/etc/ssl/room/privkey.pem"
assignment_timeout = 300

[auth]
# Either a literal secret, or a command whose stdout is the secret (preferred):
session_secret_command = "cat /run/secrets/room_session_secret"
# session_secret = "…"
verify_ttl  = 1800     # login-link lifetime (s)
session_ttl = 86400    # access lifetime before re-login (s) — 24h

[[teams]]
name = "Engineering"
[[teams]]
name = "Finance"
```

```bash
console-room --config /etc/runspec-room/console_room.toml
```

## Run it as a service (no root)

A `systemd` **user** unit ships in [`deploy/console-room.service`](deploy/console-room.service)
— it runs as you, no root, surviving logout via `enable-linger`.

```bash
pip install --user runspec-room          # or into a venv (then fix ExecStart)

mkdir -p ~/.config/systemd/user
cp deploy/console-room.service ~/.config/systemd/user/

# config (optional) — the server reads CONSOLE_ROOM_* env (or a --config TOML):
mkdir -p ~/.config/console-room
cat > ~/.config/console-room/console-room.env <<'EOF'
CONSOLE_ROOM_NDJSON_PORT=8765
CONSOLE_ROOM_PORT=8090
# CONSOLE_ROOM_HOST=0.0.0.0          # expose the browser door (then set a session secret!)
# CONSOLE_ROOM_SESSION_SECRET=...    # enables email login
# CONSOLE_ROOM_CONFIG=/etc/runspec-room/console_room.toml   # or a config file
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
