# runspec-room as a fleet MCP server — an always-on door that brokers to a present operator

**Status:** design. Not yet built. Two prerequisites (room result rendering, and
Console-tab alignment for brokered runs) come first and are specified here.
Companions: `operator-self-service.md` (the catalogue channel + request
lifecycle this builds on), `console-mcp-server.md` (the console's own
localhost MCP server — the park-and-block gate this reuses), `helpdesk-channels.md`.

---

## 1. Why this, and not a server console

The question that produced this doc was "can we make a server version of
runspec-console?" — same Python backend, same React UI, pywebview swapped for an
HTTP server, plus login and per-user separation.

Mechanically it is cheap: the `Bridge` is a plain class of JSON-in/JSON-out
methods, the frontend reaches it through one `Proxy` in `bridge/index.ts`, and
events leave through one `_dispatch` queue. Only `bridge/index.ts` and
`mock.ts` know `window.pywebview` exists. A transport swap is a few hundred
lines.

But it is the wrong product. The console is built around **a human being
present and focused**: the confirm gate assumes someone is looking at the card;
rota, "take a break" and lock auto-suspend are presence governance for one
screen; credentials are the operator's own keychain; the SSH identity is the
operator's own key; `~/logs` is that person's audit trail. CLAUDE.md already
records the decision that routing an operator's credentials onto a shared
always-on host is a trust risk that doesn't fit the console (the 0.163.0
removal of schedule host-delegation). A server console re-opens exactly that.

The always-on, multi-user server product already exists in this repo: **the
room**. It is a daemon (`systemctl --user`), it owns an authenticated web door
(email-verify, operator allowlist), it holds a live catalogue that consoles
publish to it, it brokers requests to on-shift consoles, and — the property that
matters — **it never executes anything**. Execution stays on a console with a
human in front of it, behind that human's confirm gate.

So the server product is: **the room grows an MCP door.** Any MCP client (Claude
Code, Cursor, another console's `[mcp]` client, an internal agent platform)
connects to the room over streamable HTTP and sees the published catalogue as
tools. A tool call becomes a room request; an on-shift operator console enacts
it; the result flows back as the tool result. The trust model does not move.

```
MCP client ──streamable HTTP /mcp (bearer)──▶ runspec-room (daemon)
                                                │  tools/list  = LiveCatalog.live(ns, operator=)
                                                │  tools/call  = RequestRegistry.create → next_assignee
                                                ▼
                                   on-shift operator console (NDJSON over SSH tunnel)
                                                │  _enact_assignment → autonomy gate → confirm card
                                                │  (Console tab block, inline Approve/Deny)
                                                ▼
                                   run on the chosen fleet host; result → room → MCP result
```

This is distinct from the two MCP servers that exist today:

| | where it runs | who executes | shared? | always-on? |
|---|---|---|---|---|
| `runspec serve` | in one venv, stdio | itself | no | no |
| `runspec-mcp` | on the agent's machine, stdio | itself (service identity) | no | no |
| console `[mcp_server]` | inside a console, localhost | that console, behind its gate | no (one console) | no (window open) |
| **room `/mcp`** (this doc) | on the room host, HTTP | **a present operator's console**, behind its gate | **yes** | **yes** |

---

## 2. What already exists (most of the build)

Room (`packages/console-room/console_room/`):

- `catalog.py` `LiveCatalog` — per-namespace entries published by consoles, each
  already MCP-shaped (`name`, `description`, `inputSchema`) with `x-audience`,
  `x-kind`, `x-hosts`, `x-title`. `live(namespace, operator=)` is the tool list;
  `entry_for` is the authorization check (visibility == authorization);
  `connected_holders` / `on_shift_agents` say who can enact.
- `requests.py` `RequestRegistry` — the full lifecycle: `create` → `next_assignee`
  (round-robin over on-shift consoles) → `assign` → `start` → `complete` /
  `release`; `unassigned_under` for the unstaffed case.
- `hub.py` — `submit_request`, `complete_request`, `request_status_frame`
  broadcast, the "page the team" chime.
- `web/app.py` — Starlette app, `/me`, `/catalog`, WS door, signed sessions;
  `web/auth.py` `session_operator`.
- `store.py` — SQLite transcript with `kind` + `data` columns (helpdesk migration
  already in place).

Console (`packages/python/runspec-console/runspec_console/`):

- `bridge_triggers.py` `_enact_assignment` / `_run_assignment` /
  `_run_skill_assignment` — enact a request with the requester's chosen `host`,
  through `_gated_run_tool` (the autonomy gate), then `send_message` +
  `send_request_complete`. `_portal_card_id` = the gate's `chat_id`, so the
  Approve/Deny prompt renders on the request's card.
- `mcp_server.py` — SDK-only host (`ConsoleMcpServer`, uvicorn on a thread),
  `_BearerAuthMiddleware`, and pure helpers `escalate_autonomy`,
  `sanitize_tool_name`, `_input_schema`. The "awaiting approval, resumable"
  timeout result under the client's 60 s tool timeout.
- `bridge_utils.format_result_for_room` — pretty-prints JSON output for the
  room (the only result formatting that exists today; see §3).

`runspec-mcp` (`packages/python/runspec-mcp/`): the `tools.listChanged`
capability + `notifications/tools/list_changed` pushed from a background thread
on catalogue change — the pattern the room door copies.

---

## 3. Prerequisite A — room UI: tool results output

The room web app (`web/frontend/`, ~1000 lines of vanilla JS/CSS) was built as
a message pipe and never polished. Before an MCP client or a person gets
results through it, the result path needs to carry structure. Findings:

1. **A result is a plain chat message.** `_run_assignment` posts
   `format_result_for_room(output)` via `send_message` — kind `chat`, no `data`.
   The frontend `addMessage` renders it as `textContent` in a `.msg` bubble. A
   200-line JSON result is a 200-line bubble in the transcript.
2. **Failure is inferred from a text prefix** (`✗ ` / `[exit …]`), on both
   sides. Exit code, duration, host and run id are not on the frame.
3. **Request status is not persisted.** `request_status` frames update an
   inline row keyed by `request_id`; on reload the row is empty (comment in
   `addMessage`). A visitor who refreshes loses "done ✓".
4. **The request bubble shows only the human summary**, not the args that were
   submitted or the host chosen.
5. **No output affordances** — no collapse, no copy, no monospace/JSON
   rendering, no truncation marker when the console capped the output.

### A.1 A structured `result` message kind

Add `KIND_RESULT = "result"` beside `KIND_CHAT` / `KIND_REQUEST` in
`models.py`. A result message keeps `text` as the human-readable body (so an
older room / older frontend still shows something) and carries:

```json
{
  "kind": "result",
  "text": "<formatted output, as today>",
  "data": {
    "request_id": "…",
    "runnable": "service-status",
    "kind": "runnable",
    "host": "web-01",
    "state": "done",
    "exit_code": 0,
    "duration_ms": 1830,
    "format": "json",
    "truncated": false,
    "operator": "alice"
  }
}
```

Console side: `_run_assignment` sends the result with `kind="result"` and this
`data` (needs a `send_result(...)` on the chat source beside `send_message`;
exit code and duration come from the gate's run record — the same fields the
History audit already has). `format_result_for_room` stays as the `text`
formatter. Room side: `hub.py` accepts and persists it like a request (the
`kind`/`data` columns already exist), and `complete_request` writes the final
`state`/`error` into the **request** message's `data` too, so reload restores
the status row from the transcript (finding 3).

Compatibility: a console older than this change still posts a `chat` result;
the frontend keeps rendering that as a bubble. A room older than this change
receives an unknown `kind` — `store.add` persists it, the old frontend renders
`text`. No version gate needed beyond "new console + new room = cards".

### A.2 Rendering

- A **result card** in the transcript, visually attached to its request bubble
  (same `request_id`): status badge (done / failed / exit N), runnable + host,
  duration, operator, and the output in a monospace `<pre>` — collapsed past
  ~20 lines with "Show all", JSON pretty-printed client-side when
  `format == "json"`, a Copy button, and a "(truncated by the console)" note
  when `truncated`.
- The **request bubble** gets a compact args echo (`key: value` rows from
  `data.args`, `password`-typed values never present since the console strips
  them before the request leaves the console) and "on `<host>`" when a host
  was chosen.
- The **status row** re-hydrates from the request message's `data.state` on
  reload; live `request_status` frames keep overriding it.
- Keep the vanilla JS/CSS. The room frontend is deliberately dependency-free
  and served by the daemon; this is ~150 lines of JS + CSS, not a framework
  port.

### A.3 Small polish that rides along (bounded)

Catalogue cards get the runnable's autonomy as a badge (so a visitor knows an
entry will wait for an operator's approval), the request form's submit button
reads "Send to an operator" when the entry is `confirm`, and the presence strip
shows on-shift count rather than a bare name list. Anything beyond this is a
separate ticket; the point of A is the result path.

### A.4 Tests

`tests/test_models.py` (result frame shape, `data` round-trip),
`tests/test_store.py` (persist + read back `kind="result"`), `tests/test_hub.py`
(`complete_request` stamps the request message), `tests/test_web_app.py`
(`/room` backscroll returns state on the request message). The console side is
covered in `tests/test_bridge_triggers*.py` (result frame emitted with exit
code/duration; failure inferred from the run record, not the prefix).

---

## 4. Prerequisite B — brokered runs land in the Console tab

Console 0.269.0 moved trigger runs and their Approve/Deny gate **into the
Console tab** (a fire mirrors in as a live block keyed by the card id, which
doubles as the chat id; the Triggers tab is editing-only; the run-inbox drawer
is gone). The same release hid the helpdesk surfaces (`SHOW_HELPDESK_ROOMS =
false`: the `room_*` host fields and the Self-Service tab), and the queue side
became a self-hiding `HelpdeskPanel`.

A room request today is enacted with `chat_id = "portal:<request_id>"` and its
card is dispatched via `_portal_card` with `source: "portal"` into the trigger
card store — i.e. the pre-0.269.0 inbox model. For an MCP-brokered run the
operator must see and approve it **where they are looking**, which is now the
Console tab. Required:

1. `_portal_card` stamps `detail.mode` (`"rule"` for a runnable request,
   `"agent"` for a runbook request) the way `triggers.py` does at dispatch, so
   `runspec:trigger_card` renders the block in the Console (`OutputPanel`'s
   `trigger` tag path). The block's tag reads the provenance: **Room · queue**
   for a web-form request, **MCP · <client identity>** for a door request
   (§5.6 carries the identity on the assignment frame).
2. The finished result folds onto the same block (`mirror_to_chat(...,
   card_id=)`, already threaded in 0.269.0) rather than appending.
3. The inline Approve/Deny renders on that block because `chat_id ==
   card_id` — unchanged, verified by an `OutputPanel.triggerCard`-style test
   with a `portal:` card.
4. `HelpdeskPanel` stays for queue claim/resolve (helpdesk conversations); it
   is not where a run is approved.

Nothing here depends on `SHOW_HELPDESK_ROOMS`: a console with a `room` on a
host entry (config from seed/sync) enacts requests whether or not the settings
UI shows the fields. The flag gates *setup UI*, not the runtime.

---

## 5. The MCP door

### 5.1 Transport and mounting

A streamable-HTTP MCP endpoint mounted at `/mcp` on the room's existing
Starlette app (`web/mcp.py`, `create_mcp_app(hub, catalog, registry, auth)`),
built on the v2 MCP SDK's low-level `Server` the same way `mcp_server.py` does
— behind a new `[mcp]` extra pinned `>=2,<3`. Same TLS, same public URL, same
process. Off by default:

```toml
# console_room.toml
[mcp]
enabled = true
path = "/mcp"                # mounted on the web app
wait = 50                    # seconds a tools/call blocks before returning "pending"
unstaffed = "queue"          # queue | refuse — when no operator is on shift
tokens_ttl = "30d"           # lifetime of a minted client token
```

Precedence is the room's usual defaults < config < env (`CONSOLE_ROOM_MCP_*`)
< CLI.

### 5.2 Identity — who is calling

The room has one door for people: email-verify. An MCP client needs a bearer
token. Two kinds, both minted **by the room**, stored hashed in SQLite, and
carrying the same audience the web session would:

- **Personal token** — minted from the web app after login ("Connect an agent"
  on `/me`): audience = the session's (self-service, or operator if
  `is_operator`), `visitor` = the verified email. Revocable from the same page.
  This is v1.
- **Service token** — minted by an admin CLI (`console-room token create
  --audience operator --label ci-agent`) for an unattended agent. Explicit
  audience, explicit label, listed/revoked by CLI. v2.

`tools/list` and `tools/call` filter with `LiveCatalog.live(ns,
operator=token.operator)` and `entry_for` — **the same rule the web app
enforces**, so a token can never reach an entry its audience can't see. The
identity rides every request (§5.6) so the operator's confirm card and the
History record say who asked.

### 5.3 Tool list

`tools/list` = the queue's live catalogue mapped 1:1:

- `name` = `sanitize_tool_name(entry name)`; runbooks (`skill:<id>`) become
  `runbook_<id>`; the entry's `x-title` becomes the description lead.
- `inputSchema` = the published schema, plus `host` (enum from `x-hosts`) when
  the entry spans more than one host — the same choice the operator web form
  offers. A single-host entry gets no `host` property; the room fills it.
- `x-autonomy` is carried through as an annotation so a client can show
  "will wait for operator approval" before calling.
- The server declares `tools.listChanged`; catalogue `publish` / `withdraw` /
  presence changes push `notifications/tools/list_changed` (the `runspec-mcp`
  pattern). An unstaffed queue's list is empty when `unstaffed = "refuse"` and
  the full list (annotated *no operator on shift*) when `"queue"`.

Three built-in tools ride alongside: `request_status(request_id)`,
`request_result(request_id)` and `page_operators(request_id)` (the web app's
"Page the team" chime).

### 5.4 Brokering a call

`tools/call` on a catalogue tool:

1. `entry_for(ns, tool)` — unknown/invisible ⇒ MCP error (never leaks the
   entry's existence).
2. Validate the arguments against `inputSchema` (the room already generates
   forms from it; a bounce here is cheaper than a console-side bounce).
3. `RequestRegistry.create(...)` with `kind`, `host`, and the token identity;
   `next_assignee` picks an on-shift console; the room sends the
   `assignment_frame` over that console's NDJSON connection exactly as for a
   web-form request.
4. The call **blocks up to `wait`** seconds for `complete_request`. Output lines
   the console streams during the run (a `request_progress` frame, new)
   become MCP progress notifications when the client sent a `progressToken`.
5. On completion the MCP result is the §A.1 structured result: `content` =
   the output text, `structuredContent` = the `data` block (exit code, host,
   duration, operator), `isError` on `failed`.
6. On timeout the result is **not an error**: `"Request <id> is pending —
   assigned to alice, awaiting approval. Call request_result('<id>') to
   collect it."` The request keeps running; nothing is orphaned. This is the
   console MCP server's resumable pattern, one level up.

`request_result` returns the stored §A.1 result (from the transcript, so it
survives a room restart) or the current state if not done. A client that
re-issues the original `tools/call` instead of polling gets a **new** request
— idempotency is the client's job; the description says so.

### 5.5 No operator on shift

`unstaffed = "queue"` (default): the request is created unassigned, the call
returns *pending — no operator on shift* immediately (no point waiting), and it
is assigned when a console comes on shift (`RequestRegistry.unassigned_under`
is already drained on presence change). `"refuse"`: MCP error, nothing stored.

What the door **never** does is execute the request itself, run an
`autonomous` entry on the room host, or fall back to a headless executor. If
unattended execution of autonomous entries is ever wanted, that is
`runspec-mcp` run as a daemon under a service identity — a separate product
with its own trust argument, not a mode of this one.

### 5.6 Provenance

The `assignment_frame` gains `via` (`"web"` | `"mcp"`) and `requester`
(`{identity, label, audience}`). The console:

- shows it on the Console-tab block tag and the confirm card ("Requested via
  MCP by ci-agent (operator)"),
- stamps it into the run's `run_summary` (`trigger = "room"`, `initiatedBy`
  keeps `user`, plus `requester`), so `search_history` and the History tab can
  answer "what did the CI agent ask us to run last week",
- puts the operator's name on the §A.1 result, so the client's audit says who
  approved.

The room's SQLite keeps the request + result messages for the requester's side.

### 5.7 Autonomy and denylist

Unchanged and enforced where they always were: the enacting console's gate
reads the entry's effective autonomy (`confirm` parks on the operator's card,
`autonomous` runs, `manual` is refused and the result says so); the venv's
denylist is enforced by the core binary on the host. The room carries
`x-autonomy` for display only. A door request can never be less gated than the
same request from the web form.

### 5.8 Security notes

- Localhost is not assumed: the room is already the TLS-terminating public
  daemon. Tokens are bearer-over-TLS, hashed at rest, per-identity, revocable,
  TTL-bound.
- `inputSchema` validation happens before a request exists; oversize args are
  rejected at the schema stage (the room's existing form limits).
- Rate limit `tools/call` per token (a small leaky bucket in memory), because a
  looping agent can flood a queue and page operators.
- The catalogue filter is the authorization. A token with self-service
  audience sees exactly what a self-service visitor sees.
- Nothing secret transits the room: credentials are applied on the enacting
  console (`_apply_credentials` scope auto-apply), `password` args are absent
  from published schemas already.

---

## 6. Decisions

- **The room is the server product.** No server console. Execution stays on a
  present operator's console; the room brokers.
- **Extend `runspec-room`, don't start a package.** One process, one auth
  door, one catalogue, one SQLite, one TLS cert.
- **Results become structured messages first (§3).** The MCP door returns the
  same `data` the room renders; building the door on plain-text results would
  bake the text-prefix failure sniffing into a wire contract.
- **Brokered runs render in the Console tab (§4)**, with the inline gate, like
  a trigger fire. No hidden inbox.
- **Blocking wait + status/result tools**, not the MCP *tasks* primitive, in
  v1. Tasks can replace the polling pair later without changing the request
  model.
- **Personal tokens first, service tokens second.** Audience is always
  explicit and always the catalogue's own filter.
- **`unstaffed = "queue"` by default.** A pending request with no one on shift
  is honest; silently executing is the failure mode this design exists to
  avoid.

## 7. Open questions

1. Should a `confirm` result carry the operator's **edited** args (the console
   gate allows edits before approval) back to the client? Proposed: yes, in
   `data.args_applied`, so the agent learns what actually ran.
2. Long outputs: the console caps what it posts; should `request_result` offer
   the full spill via the console's `download_file` path? Proposed: no in v1 —
   mark `truncated` and let the operator hand the file over.
3. A console's own `[mcp]` client connecting to a room door makes
   console → room → console chains possible. Allowed (it's just a client), but
   the provenance tag should show the chain so a confirm card isn't ambiguous.
4. Per-queue tokens vs per-room tokens when one daemon hosts several queues.
   Proposed: a token names one queue namespace.

## 8. Build sequence

1. **A — room results** (room `0.37.0` + console): `KIND_RESULT`, console
   `send_result`, hub persistence + request-state stamping, frontend result
   card / args echo / status rehydrate, tests. Ships value on its own — the
   room is usable as a helpdesk without the door.
2. **B — Console-tab alignment** (console): `_portal_card` mode + provenance
   tag, result folds onto the block, `portal:` card test.
3. **C — the door, v1** (room, `[mcp]` extra): `web/mcp.py`, personal tokens on
   `/me`, `tools/list` + `listChanged`, brokered `tools/call` with wait,
   `request_status` / `request_result` / `page_operators`, `via`/`requester`
   on the assignment frame, console provenance into confirm card + History.
4. **D — service tokens + rate limiting** (room): CLI mint/list/revoke, per-
   token bucket, progress notifications.

Version notes ride each step's `CHANGELOG.md`; `docs/console-room.md` (or its
successor) gains a "Connect an agent" section at step C.

## 9. Key file:symbol reference

Room: `catalog.py` `LiveCatalog.live` / `entry_for` / `connected_holders`;
`requests.py` `RequestRegistry.create` / `next_assignee` / `complete` /
`unassigned_under`; `hub.py` `submit_request` / `complete_request` /
`request_status_frame`; `models.py` `KIND_CHAT` / `KIND_REQUEST` /
`Message.frame`; `store.py` `add` / `messages` (kind + data columns);
`web/app.py` `create_app` / `me` / `catalog` / `ws_room`; `web/auth.py`
`session_operator`; `web/frontend/app.js` `addMessage` / `setReqStatus` /
`renderCatalog`.

Console: `bridge_triggers.py` `_enact_assignment` / `_run_assignment` /
`_run_skill_assignment` / `_portal_card` / `_portal_card_id` / `_portal_fail`;
`bridge_utils.py` `format_result_for_room`; `mcp_server.py`
`ConsoleMcpServer` / `_BearerAuthMiddleware` / `escalate_autonomy` /
`sanitize_tool_name`; `triggers.py` card `detail.mode` stamp;
`bridge_helpers.py` `mirror_to_chat(card_id=)`; `console-ui`
`OutputPanel.tsx` (`trigger` tag block), `HelpdeskPanel.tsx`,
`SettingsDrawer.tsx` `SHOW_HELPDESK_ROOMS`.

`runspec-mcp`: `server.py` `listChanged` push thread.
