# Console Rooms — next-session handoff

Working notes for resuming the console chat-room work. Picks up after PR #194.
Not a design doc — delete or fold into `console-room-server.md` once consumed.

## ⏩ RESUME HERE — helpdesk MVP shipped, live-tested, now polishing

**The whole helpdesk (H0–H4) is built, merged, and published**, and the
end-to-end loop has been **live-tested on a locked-down corporate host**:
visitor browser → private conversation → agent inbox → **Claim** → reply → the
visitor sees it (and a reply after Resolve reopens the thread). It works.

**Published versions:** `runspec` 0.34.0 · `runspec-node` 0.30.0 ·
`runspec-room` 0.5.1 · `runspec-console` 0.48.0 (all on main + published). Design
notes: `docs/design/helpdesk-rooms.md` (the full design + the H0–H4 markers
below); `docs/design/console-work-inbox.md` (the work inbox).

**🔶 Next live test — the self-service catalog / Forms (the one unexercised
path).** Everything else in the loop is proven; the catalog has never been seen
working live. Full step-by-step in the **"0.5.1 live-test checklist"** section
just below. Status blocker was Nexus holding the `runspec-room` 0.5.1 wheel off
the work host — pick up once it lands. **A TCP-forwarding-enabled host has now
been found**, so the default `direct-tcpip` tunnel transport can be used (no more
`room_via = "exec"` relay), which sidesteps most of the earlier deploy papercuts.

## ✅ 0.5.1 live-test checklist (next session — start here)

Folded in from the session that built console 0.47.0/0.48.0. Drives the whole
helpdesk loop to the one still-unexercised feature (self-service catalog/Forms),
on the newly-found **TCP-forwarding-enabled** host (default tunnel transport).

**Prereqs — the version gates that bit us before:**
- [ ] **runspec-room ≥ 0.5.1** is the *running process* on the host, not just the
  installed wheel. An old process still serving gives `invalid hello; closing` →
  console reconnect-loop. (≥ 0.5.0 is the hard floor for the namespace hello.)
- [ ] **runspec-console ≥ 0.48.0** on the desktop (Hosts-editor queue fields +
  room desktop notifications + the work Inbox).
- [ ] For the catalog: the **agent host's runspec ≥ 0.34.0** (understands
  `discoverable`). Older hosts surface nothing — safe, but the catalog stays empty.

**Staff the queue — now via the UI (0.48.0, no hand-edited TOML):**
- [ ] Settings → Jump Hosts → the TCP-forward host: set **Helpdesk queue** =
  `it-support`, **Room port** = `8765`.
- [ ] **Leave "Connect via" = Tunnel** (the default) — the win from the
  forwarding-enabled host; skip `room_via = exec` / Relay command entirely.
- [ ] Optionally set **Catalog allowlist** to scope self-service runnables (empty
  = all marked).
- [ ] Hit the in-app **Test** button to confirm tunnel + room handshake. *Don't*
  trust a local `ssh host` test (OpenSSH ControlMaster/ProxyCommand ≠ the console's
  pure-paramiko path).

**The unexercised path — self-service catalog / Forms:**
- [ ] On a host running runspec ≥ 0.34.0, mark one runnable
  `discoverable = ["operator", "self-service"]`, **reinstall** so it surfaces.
- [ ] Confirm the console **publishes the catalog** (on subscribe + each discovery
  refresh).
- [ ] Visitor browser at **`?queue=it-support`** (0.5.1 adapts the join form to
  show only the queue) → **Catalog panel lights up** → submit a Form.
- [ ] Submission lands as a **`⚙ request` card** in the inbox; enacting it runs
  through the **autonomy gate**. ← last helpdesk feature never seen working live.

**New 0.48.0 surfaces to watch while testing:**
- [ ] **Inbox tab** (first tab): unclaimed thread → **Action needed → Claim**; the
  catalog request's gate → **Needs approval → Approve/Deny**; **Open** deep-links
  to the owning tab. Good single pane to drive the loop from.
- [ ] **Desktop notification**: a visitor message while the console isn't focused
  raises an OS toast + taskbar flash (Windows, `[notifications]` extra). Opt-out
  `[console] room_notifications = false`.

**Gotchas to remember:**
- **Claim-gates spend** — an unclaimed thread is visible but burns no model tokens
  until Claim.
- **Rota toggles restart chat**, but the inbox is rota-independent.
- Empty catalog ≈ agent host runspec < 0.34.0, or the runnable wasn't reinstalled
  after marking `discoverable`.

**Live-test learnings (corporate network — save future deploys hours):**
- The host has **`AllowTcpForwarding no`** → the direct-tcpip tunnel is refused
  (`Administratively prohibited`). Must use `room_via = "exec"`. (0.45.0 now logs
  one actionable line + the Test button names it.)
- SSH egress is via a **single HTTP CONNECT proxy** (`[ssh] proxy` /
  `use_ssh_config` + a `ProxyCommand`), the same one discovery/transfers use. A
  room-connect *timeout* there is transient startup concurrency, not a config
  error (the Test button says so now).
- A local `ssh host …` test is **misleading** — it exercises OpenSSH
  ControlMaster/ProxyCommand the console (pure paramiko) never uses. Use the
  in-app **Test** button instead.
- The relay is pluggable: **`nc` works** (`room_exec_command = "nc 127.0.0.1 8765"`,
  or `room_command = "nc 127.0.0.1 8765"` once 0.46.0 lands). `console-room connect`
  needs the **full venv path** (exec PATH is bare).
- The **room server must be ≥ 0.5.0** — an older one rejects the agent's
  `namespace` hello (`invalid hello; closing`) → the console reconnect-loops.
  (Watch for a 0.5.0-installed-but-old-process-running mismatch.)
- **Rota toggles restart chat** (`save_rota → restart_chat`); the inbox itself is
  rota-independent (UI dispatch happens before the rota/engine gate).

**▶️ What's left to actually try (the one unexercised path):** the **self-service
catalog / Forms**. The test queue still has **0 tools** because nothing is marked
`discoverable = ["operator","self-service"]` yet. Mark one runnable on a host
running runspec ≥ 0.34.0, reinstall, and the visitor's Catalog panel lights up →
a form submit lands as a `⚙ request` card in the inbox (enacted via the autonomy
gate). That's the last helpdesk feature not yet seen working live.

**Next directions (not started):**
- ~~**Unified "work inbox"** (the user's idea)~~ — **v1 BUILT**
  (`runspec-console` 0.48.0, same branch). Design + decisions:
  `docs/design/console-work-inbox.md`. A `WorkItem` *projection/index* (not a
  merge, not a store) aggregating confirm gates + trigger cards + helpdesk threads
  into one **Inbox** tab, urgency-tiered (blocking → actionable → fyi), inline
  Approve/Deny/Claim + Open-to-owning-tab. Pure logic in `bridge/workInbox.ts`
  (tested), thin `useWorkInbox.ts` hook at the App root, new
  `get_pending_confirms()` getter for cold-start. **Left for later:** in-tab
  focus-on-open, group-chat unread as `fyi` (design §12 Q2), the optional backend
  aggregator + notification consolidation (design §10).
- ~~**Hosts-editor fields** for `room_queue` / `room_catalog`~~ — **DONE**
  (`runspec-console` 0.47.0, branch `claude/helpdesk-console-room-next-j2e42j`):
  the Jump Hosts editor now manages both keys (queue field + catalog-allowlist
  tag box), riding the existing Room port + transport; export emits them; queue
  validated (no slash/space) and dropped without a port. Staffing a queue no
  longer needs hand-edited TOML.
- ~~**OS/desktop notifications** for room messages~~ — **DONE**
  (`runspec-console` 0.47.0, same branch): inbound room/helpdesk messages raise
  an OS toast + taskbar flash (click → focus) at parity with the confirm-gate
  notification, suppressed when the console is already foregrounded (the in-app
  toast covers that). `notify.build_room_message` (pure, tested) names the
  visitor + queue; gated by `[console] room_notifications` (default on),
  Windows-only via the `[notifications]` extra. Fired from `_on_chat_event`
  (`bridge._notify_room_message`).
- §10 parked questions below (multi-queue routing, SLA/auto-close, browser agent
  inbox).

---

## Helpdesk pivot — decided + H0 shipped (build history)

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

**✅ H1 — hub namespace participant + conversation naming — DONE** (same branch):
`console_room/conversation.py` (pure: `helpdesk/<queue>/<visitor>` naming,
segment-boundary `namespace_matches`, `valid_token`); hub gains namespace
**agents** alongside exact **members** (`join_agent`/`leave_agent`/`agent_message`,
prefix-matched fan-out, group-chat untouched); doors wired (NDJSON `hello`
`namespace` = agent mode; browser `?queue=` derives the visitor room server-side).
82 tests pass. No runspec-room version bump yet — version + CHANGELOG land when the
helpdesk is user-facing (end of H3/H4), per the console precedent.

**✅ H2 — conversation lifecycle + control frames — DONE** (same branch):
`console_room/lifecycle.py` (pure state machine open→assigned→resolved→closed +
reopen; `ConversationRegistry`, change-vs-no-op return contract). Hub drives it:
a visitor message opens/reopens; agent-only `claim`/`resolve`/`reopen`/`close`
(namespace-scoped) broadcast a `conversation` state frame to visitor + team;
join/subscribe catch-up replays state. NDJSON control frames wired. `claim` is the
gate for the deferred agent turn. 97 tests pass; still no runspec-room version bump
(lands when user-facing, end of H3/H4).

**✅ H3 — visitor view + self-service catalog — DONE** (branch
`claude/runspec-room-h3-w0s5f0`, stacked on the H1+H2 PR #199). **Catalog
decisions (user-confirmed, resolves parked question 2):** consoles publish the
catalog (agent pushes a `catalog` frame of its `self-service`-marked tool schemas
over NDJSON; room caches per queue, last-writer-wins; `GET /catalog?queue=`
serves visitors, auth-gated); default scope = all marked + optional console-side
per-queue allowlist; catalog is always browsable (portal-style panel). Room stays
runspec-free and never executes: a form submit → `request`-kind message (human
text + `data` = tool/args), catalog-validated + size-capped, opens/reopens the
thread. `Message` gained `kind`/`data`; SQLite migrates in place. Visitor UI:
catalog panel + schema-generated forms (`inputSchema` → select/checkbox/number/
text), lifecycle chip, request cards, `?queue=` deep links. 117 tests pass.

**✅ H4 — agent inbox + catalog publisher — DONE** (branch
`claude/runspec-room-h4-w0s5f0`). The runspec-console side: `room_queue` on a
fleet host staffs that queue (agent-mode `hello` with the namespace); Rooms tab
gains an **inbox** of the queue's conversations (lifecycle chips,
Claim/Resolve/Reopen/Close, request-kind messages tagged, replies via the
existing composer); **claim-gated agent turns** at the bridge (`_feed_engine_for`
— a staffed queue's messages feed the trigger engine only when the thread is
assigned to this console); **catalog publisher** (`runspec_console/catalog.py` —
`rawSpec.discoverable ∋ "self-service"`, optional `room_catalog` allowlist,
visitor-form contract, dedupe, name→runnable index kept for enactment; publishes
on subscribe + every discovery refresh). New bridge APIs claim/resolve/reopen/
close_thread; `runspec:room_conversation` UI event. **All five slices done —
helpdesk is end-to-end.** Versions bumped + changelogs written: `runspec-room`
0.5.0, `runspec-console` 0.44.0 (publishing is the user's manual step). Hosts
editor fields for `room_queue`/`room_catalog` are a future polish — hand-edited
TOML keys survive the editor (unmanaged keys are preserved).

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

## ✅ Hatchling sdist issue — FIXED (wheel-only publish, option a)

**Resolved** on branch `claude/console-room-next-session-f6sdma`:
`.github/workflows/console-release.yml` no longer builds or uploads an sdist —
the "Build sdist" step is gone and only the (smoke-tested, force-included) wheel
is published. The wheel is pure `py3-none-any` so it always applies; a comment on
the wheel-build step records why the sdist is intentionally omitted. Confirmed via
scan that `runspec-console` is the **only** package force-including a generated
asset without an sdist target, so no other release workflow needed the change.

Original write-up (for context):

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
