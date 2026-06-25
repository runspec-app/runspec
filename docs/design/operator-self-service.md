# Operator self-service in the console-room

Status: implemented — `runspec-room` 0.29.0, `runspec-console` 0.130.0.

## Problem

The console-room web app had exactly one kind of authenticated user: a **visitor**.
Logging in via email-verify only ever showed the *self-service* catalogue — runnables
whose `discoverable` audience contains `"self-service"`, optionally narrowed by a
host's `room_catalog` allowlist — rendered as a form. Submitting routed a `request`
frame to an on-shift operator console, which enacted it behind its autonomy gate. The
room is a "dumb pipe"; consoles execute.

An operator had no way to log into the room and get the full picture: the **full
operator catalogue** plus the team's **runbooks**, driven through the same forms.

## Design

The room stays a dumb pipe — all execution happens on an operator console behind its
autonomy gate. A runbook is **not** directly runnable by a browser: it is rendered and
run as an agent turn on a console. So "self-service skills" means a *skill request*
(skill id + form values) routed to an operator console that renders and runs it.

### One catalogue channel, per-entry audience

Rather than fork the catalogue into per-audience registries, each published catalogue
entry carries two tags:

- `x-audience` — `"self-service"` | `"operator"`. A visitor sees only `self-service`
  entries; an operator sees everything. **Absent ⇒ `self-service`**, so an older
  console never leaks an operator-only entry.
- `x-kind` — `"runnable"` | `"skill"`. The room and frontend tell them apart by it; a
  skill also carries `x-title` (its human label; the wire `name` is `skill:<id>`).

Filtering happens at two edges, and **authorization is visibility**:

- `GET /catalog` returns `hub.live_catalog(ns, operator=<flag>)` — the on-shift union
  filtered to what the session may see.
- `hub.submit_request` validates the requested tool against the same per-session
  filter before routing, so a visitor can never request what they cannot see.

The room's `LiveCatalog` is unchanged except for an `operator` filter on `live()` and
an `entry_for()` lookup used to read an entry's audience when authorising a request.

### Operator identity

A login email is recognised as an operator via an **explicit allowlist** on the room:
`CONSOLE_ROOM_OPERATORS` / `[auth] operators` (TOML array or comma/space string;
precedence **defaults < config < env < CLI**). At `/verify` the verified email is
checked (`signup.is_operator`) and the boolean is stamped on the signed session
(`session["operator"]`). `web/auth.session_operator` reads it; `/me`, `/catalog`, and
the WebSocket door (→ `BrowserConnection.operator`) consume it. Dev mode honours
`?operator=1` so the path is exercisable without auth.

### Console: building the catalogue

`catalog.build_full_catalog(runnables, self_service_allowlist=…)` emits **one** entry
per runnable, tagged with the broadest audience it qualifies for:

- `self-service` when it is self-service-marked **and** passes the `room_catalog`
  visitor allowlist (visible to everyone), else
- `operator` when it is operator-discoverable (the default audience) — visible only to
  operator sessions, and deliberately **not** narrowed by the visitor allowlist.

`catalog.build_skill_catalog(skills)` appends published runbooks as
`skill:<id>` entries, with `inputSchema` built from each skill's `[skill.args]` via the
shared `skillargs.parse_skill_args`. A runbook opts in via a `discoverable` audience
field on its `[[skill]]` entry (**default empty — not published**). For now published
runbooks are tagged `operator` (operator-only); a future change tags self-service-marked
skills as `self-service` to open them to visitors.

`bridge_triggers._publish_catalog` merges runnables + skills into one channel, keeps
`_catalog_index[ns]` (runnable name → discovered runnable) and `_skill_index[ns]`
(`skill:<id>` → skill id) for enactment, and sends one (unchanged-shape) `catalog`
frame.

### Console: enacting a request

`_enact_assignment` dispatches on the assignment's `kind`:

- `runnable` → the existing `_run_assignment` → `_gated_run_tool` path (unchanged).
- `skill` → `_run_skill_assignment`: resolve the id via `_skill_index`, validate values
  (`skillargs.validate_skill_values`), render the body
  (`memory.render_skill_body`), and run it as an isolated **autonomy-gated agent turn**
  (`_trigger_run_agent_turn`, the same path triggers/schedules use). The turn's
  `chat_id` is the portal card id, so each runnable's confirm prompt renders on the
  card. The request reports `done`/`failed` only when the whole turn ends — the visitor
  sees the real outcome, not a hand-off.

### Frontend

`web/frontend/app.js` reads `operator` from `/me`, splits the catalogue into a
Runnables list and a Runbooks section (entries with `x-kind === "skill"`, shown by
`x-title`), and tags each form submission with `kind`. Vanilla, no build step.

## Decisions

- **Operator = explicit allowlist** (not "email == queue mailbox", which is a shared,
  often-unattended address, nor an "operator team", which is self-selected at login).
- **Runbooks operator-only for now** — the audience plumbing makes visitor-facing
  self-service skills a one-flag change later.
- **Skills opt-in** via a `discoverable` field on the skill (default off — safer for
  runbooks than runnables, which default to operator).
- **`done` on turn completion**, via the portal lifecycle, not on hand-off.

## Tests

- Console: `tests/test_catalog.py` (audience tagging, `build_full_catalog`,
  `skill_to_catalog_entry`/`build_skill_catalog`, `skill_audiences`),
  `tests/test_chat_bridge.py` (`_run_skill_assignment`, kind dispatch, publish).
- Room: `tests/test_signup.py` (`parse_emails`/`is_operator`),
  `tests/test_config.py` (`[auth] operators`), `tests/test_signup_web.py`
  (operator session), `tests/test_web_app.py` (`/me` + `/catalog` filter, audience
  gating over the WS), `tests/test_catalog.py` (`live(operator=)`, `entry_for`),
  `tests/test_hub.py` (skill request routing + `kind`).
