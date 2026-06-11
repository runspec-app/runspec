# Console Rooms — next-session handoff

Working notes for resuming the console chat-room work. Picks up after PR #194.
Not a design doc — delete or fold into `console-room-server.md` once consumed.

## Where things stand (shipped)

The chat-room feature is built and live-tested on a locked-down corporate host:

- **runspec-console 0.43.2** — multi-room, always-on (independent of triggers),
  rooms are a property of a fleet host (`room` / `room_port` on the host entry).
  Two transports: SSH `direct-tcpip` tunnel (default) and an **exec-channel**
  relay for networks that block port-forwarding (`room_via = "exec"`).
  - PR #194 (latest): per-host `room_exec_command` verbatim override + Hosts-editor
    **Connect via** / **Relay command** fields. Merged; user publishing 0.43.2.
  - Key code: `runspec_console/sources/chat_transport.py` (`host_rooms`,
    `make_host_room_factory`, `make_host_room_exec_factory`, `default_room_user`),
    `bridge.py` (`start_chat` / `_room_specs` / `_chat_supervise` / multi-room
    `_chat_sources`, `_MANAGED_HOST_KEYS`), `ssh_pool.py` (`open_exec`).
  - UI: `console-ui/src/views/RoomsView.tsx`, `components/SettingsDrawer.tsx`.
- **runspec-room 0.4.x** — the server ("dumb message pipe with presence"). Two
  front doors: consoles via NDJSON over the SSH tunnel; people via an
  OIDC-authenticated browser web app (Starlette + uvicorn, TLS, SQLite transcript).
  `console-room connect` is the exec-channel stdio↔localhost relay.
  PR #193 (0.4.1) added: dev-mode `session_user` AssertionError swallow, `/favicon.ico`
  204, `[hidden]{display:none}` CSS fix, and the **systemd user-service deploy kit**
  (`deploy/console-room.service`, `WantedBy=default.target`, `systemctl --user`
  + `loginctl enable-linger`, no root). Confirm #193 merge/publish status.

### Live-test state (to resume testing)
- Exec transport via `nc` **drops the 2nd message** (nc's flaky half-close); `socat`
  or `console-room connect` fixes it. User is getting the **runspec-room 0.4.1 wheel
  onto web01** to switch the relay to `console-room connect`. Resume testing there.
- Browser front door works (user joined, saw own message). Cross-network browser
  test was pending a 2nd machine.

## Open ideas for next session

### 1. Helpdesk-on-rooms (user's pivot idea — DISCUSS, don't build yet)
Reframe: instead of multi-party rooms, a **support/help-widget** model — many
visitors, a few agents "on shift", 1:1 sessions that close on "resolved", plus an
embeddable website chat widget. My recommendation (agreed to revisit while testing):
**don't pivot the core — layer on it.** A support session = a 2-seat room with a
lifecycle; the widget = a third front door on the same pipe. What it *adds* (not
removes): session lifecycle (open→assigned→resolved→closed), routing/queue, and an
anonymous/lightly-authed visitor identity. **The one real design point is the
identity flip:** today "a console's SSH access *is* its right to be in the room";
in helpdesk the agent is the SSH/console side and the visitor is an anonymous web
user. If we proceed, write a short design note covering: session state machine,
routing (next-on-shift), widget front door, and that identity model.

### 2. Docs housekeeping (deferred from the 3-item polish task)
Was paused because the helpdesk decision may rewrite this content. When settled:
- Update `docs/design/console-room-server.md` + `console-chat-room-source.md` for
  host-associated rooms + the exec transport (currently describe the older model).
- Delete `docs/design/console-chat-room-handoff.md` (shipped) — and this file too
  once consumed.

## ⚠️ Hatchling issue to fix (found while inspecting the release)

`packages/python/runspec-console/pyproject.toml` force-includes the gitignored
Vite output **only under the wheel target**:

```toml
[tool.hatch.build.targets.wheel.force-include]
"runspec_console/dist" = "runspec_console/dist"
```

There is **no `[tool.hatch.build.targets.sdist]` config**, so the **published sdist
excludes `runspec_console/dist/`** (hatchling's default VCS file-selection drops
gitignored paths). The release workflow (`.github/workflows/console-release.yml`)
builds *and uploads both* wheel and sdist, and the **smoke test only checks the
wheel** — so a UI-less sdist ships unnoticed. Any install that builds from the
sdist (`pip install --no-binary`, a downstream rebuild, or a future env with no
matching wheel) gets a console with **no UI** (`dist/index.html` missing →
runtime failure).

The wheel itself is fine (UI built first, force-included, smoke-tested), so normal
`pip install runspec-console` is unaffected today. Fix options:
- **(a) Wheel-only publish** — drop the sdist build/upload from the release. Simplest;
  the wheel is pure-`py3-none-any` so it always applies. Recommended.
- **(b) Mirror force-include into an sdist target** + add an sdist smoke test. Keeps
  an sdist but means shipping a built artifact inside it (debatable for source dists).

Note the **same pattern affects runspec-node? no** — this is console-specific
(Python pack bundling a JS build). Worth a quick scan of other packages that
force-include generated assets, but none currently do.

## Constraints (standing)
- Feature branches; commit + push; model identifier must NOT appear in
  commits/PRs/code/CHANGELOG.
- runspec-room stays **out of runspec** (no `runspec` dep) so it's invisible to
  `runspec local` / `serve` discovery — same precedent as `runspec-linux-core`.
- Releasing is decoupled: version bump on main → `auto-tag` writes `console-v*`;
  publishing is a manual `workflow_dispatch` on that tag (see `docs/releasing.md`).
  Agents stop at the version bump + CHANGELOG; never push a `{prefix}v*` tag.
