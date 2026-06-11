# Console chat room — phase handoff / resume notes

Working doc to resume the chat-room feature in a fresh session. Pairs with the
design in `console-chat-room-source.md`. Delete once the feature ships.

---

## Status snapshot

| Slice | What | State |
|---|---|---|
| 1 | `sources/chat.py` — `ChatSource` event source (+ `[chat]` config) | ✅ merged (#183) |
| 2 | `sources/chat_transport.py` — NDJSON-over-SSH-tunnel transport | ✅ merged (#183) |
| 3 | **Bridge wiring** — construct the source w/ a real transport, UI fan-out, `send_room_message`, draft-reply, rota-driven presence | ✅ done (branch `claude/console-chat-room-2apgjx`) |
| 4 | **console-ui Rooms view** — transcript + presence + composer | ✅ done (branch) |
| — | **Console integration** — `hello` frame + SSH-defaulted `user` | ✅ done (branch) |
| — | **Room server** (`packages/console-room/`, runspec-room) — console NDJSON door + OIDC browser door + TLS | ✅ done (branch) — see `console-room-server.md` |

**Not runnable end-to-end yet.** Slice 3 wires the console side (source built
with the pooled tunnel transport, events fanned to the UI, `send_room_message`,
the confirm-gated `propose_reply` speak-checkpoint, rota-driven presence), all
unit-tested with fakes — but there is still **no room server** to connect to and
**no Rooms view** to see it in. Until a server exists the tunnel transport simply
fails to connect; the wiring is correct and tested regardless. Nothing to
interact with by hand yet — this is expected.

### Slice 3 — what landed (Python, branch `claude/console-chat-room-2apgjx`)

- `bridge.py`: `_ChatUIFanout` (wraps `ChatSource`: fans `message`+`presence` to
  the UI via `_dispatch` → `runspec:room_message` / `runspec:room_presence`; only
  `message` reaches the engine, so presence never clutters the trigger audit
  feed); `_RotaPresenceWatcher` (polls the engine's `clock_ok`, pushes
  `set_presence` only on an on-shift flip); `_build_chat_source` (constructs
  `ChatSource(transport_factory=transport_factory_from_config(pool))` and is
  reached by the engine through a `create_source` hook in `start_triggers`, since
  the registry factory is zero-arg); `send_room_message(room, text)` js_api →
  `ChatSource.send_message`; `_gated_propose_reply` (the speak checkpoint:
  confirm-gated, dispatches `runspec:room_draft` to pre-fill the composer — never
  sends).
- `trigger_runner.py`: `propose_reply_schema()`; `compose_system(draft_kind=…)`
  branches the draft note (`"room"` → propose_reply / composer, `"mail"` →
  draft_reply / Drafts); `make_handler` binds `room` from a `chat` event's payload
  (and `reply_to` stays `None` — chat has no entry_id).
- Tests: `tests/test_chat_bridge.py` (fan-out, presence watcher, send guards,
  propose-reply guards) + new cases in `tests/test_trigger_runner.py`.
- Lifecycle: **always-on** (updated — the source was decoupled from the engine).
  A reconnecting supervisor (`start_chat` / `_chat_supervise`) joins whenever
  `[chat]` host/port/room are configured, independent of triggers, so you can be
  *in* the room (transcript / chat to humans / presence) with no chat trigger at
  all. The engine marks `chat` an `external_sources` id and receives its messages
  via `engine.feed`, so a chat trigger still fires.

---

## A. Try-out checklist (what you *can* verify now)

- [ ] Merge #183; confirm CI is green (console tests run via `PYTHONPATH`, not an
      editable install — see housekeeping).
- [ ] In a fresh clone, run the new tests:
      `cd packages/python/runspec-console && pip install pytest && PYTHONPATH=. python -m pytest tests/test_chat_source.py tests/test_chat_transport.py`
      (41 tests; no SSH/server needed — they use fakes).
- [ ] Skim `docs/design/console-chat-room-source.md` (the model + §6 staged plan)
      and confirm it still matches what you want.
- [ ] Eyeball the `[chat]` config shape you'll use (host / port / room / user) —
      see §B.

That's the extent of what's testable without slice 3 + a server.

---

## B. Report back to me (this unblocks the next phase)

The big one first:

1. **Room server.** There is no server yet. Do you want me to **build a minimal
   reference room server** next session (so the feature becomes demoable), or do
   you have/intend your own? Without it there's nothing for a console to connect
   to. If I build it: where should it live — a new `packages/console-room/`, the
   `node-webops` neighbourhood, or a Python package?

2. **Which remote runs the room?** The SSH host string exactly as the console
   knows it (matches your jump-host fleet), and a localhost port the server will
   bind on that host. These become `[chat] host` / `port`.

3. **Console identity (`user`).** What display name should a console show in the
   room? Over the tunnel the server can't see the OS user, so the console
   declares it. Is a client-declared name fine for v1 (SSH access is the real
   authN), or do you want identity derived from the SSH session?

4. **Reply model — confirm.** Agent proposes reply text → it pre-fills the room
   **composer** → *you* edit and send. The agent never sends. (This is the plan;
   just confirm it's what you want.)

5. **Fan-out for v1.** Broadcast to every available console (each drafts its own
   reaction), or defer the agent turn until a human "claims" the message? Design
   §7 q1. Recommend shipping broadcast first.

---

## C. Next-session notes (for Claude)

**Locked decisions:** SSH tunnel via `SSHConnectionPool.open_direct_tcpip`;
NDJSON framing on the console link (browsers use websockets to the server);
source stays `available()==False` until wired; **speak** = draft → human sends,
**act** = autonomy gate; room visible in-console as the coordination surface;
broadcast fan-out (pending §B.5 confirm).

**Read first in `bridge.py` (don't edit before reading):**
- How the `TriggerEngine` is built/started and where sources are created — grep
  for `create_source` / how `"outlook"` / `"notifications"` get constructed and
  `start()`ed. **Key wrinkle:** the registry factory is zero-arg, but `chat`
  needs the `pool` injected. So the bridge must build the chat source itself —
  `ChatSource(transport_factory=transport_factory_from_config(pool))` — and hand
  that instance to the engine, *not* go through `create_source("chat")`. Decide
  the cleanest hook for that.
- `_agentic_chat_turn` / `_run_isolated_agent_turn` (the agent loop reused for
  triggers).
- `_confirm_decision` / `_register_confirm` / `_effective_tool_autonomy` (gate).
- `_custom_agent_tools` / `_run_custom_tool` / `_CUSTOM_TOOL_NAMES` (the seam for
  the draft-reply action — model it on Outlook's `draft_reply`).
- `_dispatch` (→ `runspec:*` CustomEvents to the UI).
- The rota evaluation path (`config.read_rota`, `sources/outlook.calendar_clock_ok`)
  — where on-shift state is computed, so `set_presence` can be driven from it.

**Slice 3 tasks:**
1. Construct & register the chat source with the pooled transport (per wrinkle
   above); start/stop it with the other sources.
2. Fan **all** chat events (message *and* presence) to the UI via `_dispatch`:
   `runspec:room_message` / `runspec:room_presence`. (Message events already
   reach the engine for triggers; presence is UI-only and the engine ignores it.)
3. `send_room_message(room, text)` js_api method → `ChatSource.send_message`.
4. Draft-reply: per design §2 this is **draft-to-composer, not auto-send**. The
   agent-side action should surface proposed text to the composer (e.g. a
   `runspec:room_draft` dispatch / a `propose_reply` custom tool), and the human
   sends via `send_room_message`. Decide tool-vs-dispatch; keep it confirm-gated
   if it's a tool. Do **not** make the agent send directly.
5. Drive `ChatSource.set_presence(on_shift)` from rota state changes.

**Slice 4:** console-ui Rooms view — transcript + presence + composer — consuming
`runspec:room_message` / `room_presence` / `room_draft`; composer send calls
`send_room_message`. New view alongside Chat / Forms / Fleet.

**Housekeeping (don't forget):**
- Bump `runspec-console` version + add a `CHANGELOG.md` entry **when the feature
  becomes user-facing** (end of slice 4), not before — `docs-check` version-sync
  doesn't require it until then.
- The clean-clone **build-install fix** (commit `runspec_console/dist/.gitkeep`
  un-ignored + pin `hatchling>=1.18` + gate `pywin32` with
  `; sys_platform == "win32"`) is a **separate PR** the user will authorize — do
  *not* fold it into the chat feature branch.
