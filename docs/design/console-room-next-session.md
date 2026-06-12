# Console Rooms — next-session handoff

Working notes for resuming the console chat-room work. Picks up after PR #194.
Not a design doc — delete or fold into `console-room-server.md` once consumed.

## Helpdesk pivot — decided + H0 shipped (resume here)

The helpdesk direction is agreed and written up in
**`docs/design/helpdesk-rooms.md`** (private per-user conversations + a curated,
runspec-validated self-service catalog; consoles-only agents; the two existing
front doors already split visitor/agent). Build order is H0..H4 in that note's §9.

**✅ H0 — the `discoverable` keyword — DONE** (branch `claude/runspec-room-dev-w0s5f0`):
audience-scoped runnable-level field (`true`/`false`/`["operator","self-service"]`,
operator-default, self-service opt-in), at parity in Python (`runspec` 0.34.0) and
Node (`runspec-node` 0.30.0). Spec + schema + format.md + compliance fixtures +
CHANGELOG all moved together; `runspec serve` now skips non-operator runnables and
schemas carry `x-discoverable`; `RunSpec.runspec_discoverable` exposes it.

**Next slices (not started):** H1 hub namespace/prefix participant + server-side
conversation naming → H2 lifecycle + control frames → H3 visitor browser view +
catalog/Forms → H4 agent-console inbox view. See `helpdesk-rooms.md` §9.

**Parked open questions (stored for next — from `helpdesk-rooms.md` §10):**
1. **Queue routing** — single helpdesk queue vs multiple departmental queues, and
   whether a visitor's department auto-routes or they pick. Leakage concern leans
   multi-queue; H1's `helpdesk/<queue>/<visitor>` naming already allows it — ship
   single, design for many.
2. **Catalog source of truth** — per-queue explicit allowlist of `self-service`
   runnables vs every `self-service`-marked runnable installed on the agent hosts.
   Leaning explicit per-queue allowlist (tight blast radius).
3. **SLA / notifications** — resolved-idle auto-close, unassigned-too-long nudge.
   Out of scope for the first cut; the lifecycle gives the hooks.
4. **Browser agent inbox** — deferred by the consoles-only cut; additive later.

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

### 1. Helpdesk-on-rooms — ✅ RESOLVED, design agreed → `docs/design/helpdesk-rooms.md`
The "discuss, don't build yet" item is settled. Decisions (see the design note):
**private per-user conversations**, not a shared room — each SSO visitor sees only
their own thread (kills cross-department leakage + makes attribution the primary
key + easy corporate sell). The "identity flip" worry is moot: the existing two
doors already split the roles — **browser door = visitor** (OIDC), **console door
= agent** (SSH-gated). **Agents are consoles only** (clean visitor/agent cut). A
help-desk interaction = private chat **+** a curated, runspec-validated
**self-service catalog** (Forms) enacted through the existing speak + autonomy
gates; the catalog is curated by a new **`discoverable`** spec keyword
(audience-scoped: `true`/`false`/`["operator","self-service"]`, default operator,
visitor-facing is opt-in). Build order: **H0 `discoverable` keyword first**
(self-contained, ships value alone), then hub namespace participant → lifecycle →
visitor view/catalog → agent inbox. Full state machine, routing, and identity
model are in the design note.

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
