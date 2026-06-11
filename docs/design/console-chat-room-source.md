# Design: chat-room event source for runspec-console

**Status:** In progress. Source module + tests landed; SSH-tunnel transport,
Rooms view, and bridge wiring are staged below.
**Scope:** A third trigger event source (`chat`), symmetric to Outlook and the
notification listener. Cross-platform (no OS API — it's a network client).

This is the messaging-portal idea, deliberately reduced to its essence: **a tiny
chat room, and nothing else.** No runnables exposed over the web, no args, no
curation, no scheduler. The web app is a dumb message pipe with presence; all
intelligence stays in runspec-console.

---

## 1. The model, and why the ephemeral console becomes a *feature*

A team chat room runs as a small web app on a remote host the consoles already
reach by SSH. Participants are of two kinds:

- **People** — log into the web app in a browser and type messages.
- **Consoles** — each running runspec-console appears as a participant. A console
  is **available** when it is running *and* on-shift (the existing rota), and
  **away** otherwise. Presence is visible to everyone in the room.

When a message is sent, every **available** console in that room receives it as a
`chat` / `message` event and reacts through the normal trigger machinery
(`match` → rule/agent → autonomy gate). A console that is closed or off-shift is
simply *away* and never sees it.

That last sentence is the whole point. The trigger system is **forward-only with
no persistence** by design (see `console-triggers.md` §8/§11) — an event that
arrives while the console is closed is missed. For the earlier "request portal"
idea that was a limitation to engineer around. For a *chat room* it is exactly
correct: an away participant doesn't answer messages, and the web app keeps its
own transcript so nothing is lost — the console just doesn't re-react to messages
that arrived while it was away. The model that fought us before now fits with
zero new machinery.

---

## 2. Speak vs. act — two human checkpoints

Nothing the console does in the room is automatic. The design splits cleanly into
two independently human-gated actions:

| | What it is | Who commits it |
|---|---|---|
| **Speak** | A drafted reply / ack. The agent proposes text; it lands in the room **composer**, never on the wire by itself. | The human edits and clicks send (`send_room_message`). Mirrors Outlook `draft_reply` — except the destination is the room composer, and *sending* is a human click. |
| **Act** | Enacting a runnable the reaction proposes. | The existing **autonomy gate** (`confirm` by default). |

Drafts stay **local** to each console until sent — the room only ever shows
*sent* messages plus presence. The race between several available consoles is
resolved **socially, not with a lock**: everyone sees the same room, the quick
"ack, looking" messages collide visibly, and the humans coordinate who enacts —
exactly like a real on-call channel. This is why the room must be **visible
inside the console** (§5): a first-responder can only know they're first by
seeing the room and the others' acks.

---

## 3. What we reuse (almost everything)

Identical to the reuse table in `console-triggers.md` §3 — the engine, the
`match` pre-filter, rule/agent routing, the autonomy gate, the kanban surface,
and the `_dispatch` → `runspec:*` CustomEvent channel to the frontend. The only
genuinely new pieces are:

1. **`sources/chat.py`** — the `ChatSource` (this slice). A network client that
   emits `message` / `presence` events and relays outbound sends.
2. **An SSH-tunnel transport** for that source (next slice).
3. **A "Rooms" view** in `console-ui` (next slice).
4. **`send_room_message` + a "post to room" agent tool** on the bridge — the
   speak checkpoint (next slice).

---

## 4. Transport — a JSON message stream over the existing SSH tunnel

Decided: **SSH tunnel.** The web app binds **localhost on the remote**; the
console (which already pools an SSH connection to that host) opens a
`direct-tcpip` channel to it (`SSHConnectionPool.open_direct_tcpip`) and speaks a
message stream over the tunnel. Consequences:

- **No new exposed port** on the remote, and **no second credential system for
  consoles** — a console's right to be in the room *is* its SSH access to that
  host. (Browser people still authenticate to the web app with the login system;
  the two participant classes authenticate differently — §1.)
- **Rooms scope to the host/fleet** the tunnel reaches.

**Framing — NDJSON, not websocket, on the console link.** A paramiko `Channel`
is a byte stream, not a full socket (no `setsockopt`), so running a websocket
client straight over it needs a fragile, untestable local-port forwarder. The
console instead speaks **newline-delimited JSON** over the channel: simpler,
dependency-free, and unit-testable with a fake channel. Browsers still speak
websockets to the room server — same message schema, two front doors; the room
server (slice 2 / separate artifact) trivially exposes both. The source is
written against an injected `ChatTransport` (`send` / `recv` / `close`) so the
room/presence/parse logic is tested with a fake, and the tunnel transport
(`ChannelChatTransport` + `make_tunnel_transport_factory`) drops in behind the
same interface — the split that lets `outlook.py` test its extractors without COM.

### Wire protocol (minimal)

Server → console:
```
{"type": "message",  "room": "...", "sender": "...", "text": "...", "id": "...", "ts": 0.0}
{"type": "presence", "room": "...", "user": "...",   "status": "available|away"}
```
Console → server:
```
{"type": "message",  "room": "...", "text": "..."}                      # human-sent (send_room_message)
{"type": "presence", "room": "...", "status": "available|away"}         # set on connect, on rota flip, on stop
```

`message` events fire triggers (`match` binds to `sender`/`text`). `presence`
events feed the Rooms view; no trigger needs to bind to them, and the engine
harmlessly ignores them. A console **never re-emits its own messages** (the
server echo is dropped by `sender == self_user`) so it can't react to its own
acks — the chat analogue of the forward-only contract.

---

## 5. Rooms view (next slice) — load-bearing, not cosmetic

A new `console-ui` view alongside Chat / Forms / Fleet: live transcript +
presence list + composer. Fed by the same tunnel websocket, fanned out to the
frontend as `runspec:room_message` / `runspec:room_presence` over the existing
`_dispatch` channel. The composer's send is the one outbound path
(`send_room_message`); the agent's drafted reply merely pre-fills it. The view
doubles as a human-to-human coordination surface — a console user can just talk
to the requester ("on it, 5 min"), which *is* the "who enacts" negotiation.

---

## 6. Staged build plan

1. **`sources/chat.py` + tests** ✅ *this slice* — `ChatSource` satisfying the
   `EventSource` protocol; pure `message_payload` / `presence_payload` /
   `parse_inbound` (incl. self-echo skip) tested with a fake transport;
   `set_presence` / `send_message` relays; `[chat]` config read. Registered, but
   reports `available() -> (False, …)` until a real transport is wired, so the
   engine never starts a half-built source in production.
2. **SSH-tunnel transport** ✅ *done* — `sources/chat_transport.py`:
   `ChannelChatTransport` (NDJSON over a pooled `direct-tcpip` channel — line
   framing/buffering tested with a fake channel, no new dependency) +
   `make_tunnel_transport_factory` / `transport_factory_from_config`. Wired
   end-to-end into `ChatSource` in tests.
3. **Bridge wiring** — construct `ChatSource` with the tunnel transport; fan
   `message`/`presence` to the Rooms view via `_dispatch`; `send_room_message`;
   the confirm-gated "post to room" agent tool (the speak checkpoint); drive
   `set_presence` from the rota state.
4. **`console-ui` Rooms view** — transcript + presence + composer.

---

## 7. Open questions

1. **Claim-to-react (cost).** v1 broadcasts to every available console, so each
   fires its own agent turn (N× tokens, N private drafts) for a message one
   person handles. Alternative: deliver the message + presence to all (so they
   can claim), but defer the *agent* turn until a human claims it in the room
   ("I'll take this"). Ties LLM spend to the claim and matches "first responder"
   precisely. v1 ships broadcast; revisit with usage.
2. **Web app home.** The room server itself is a separate tiny artifact (auth +
   rooms + presence + message fan-out). Where does it live — a new package, or
   `node-webops`' neighbourhood? Out of scope for the source slice.
3. **Trust.** Inbound chat is untrusted text from other room members; in agent
   mode it is LLM input, so standard prompt-injection exposure applies, backstopped
   by `match` + the confirm gate — same posture as the toast/Outlook sources.
