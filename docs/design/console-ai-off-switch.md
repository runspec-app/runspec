# Console "AI off" switch — a non-AI edition of runspec-console

**Status:** planned — codebase surveyed, feasibility confirmed, phased plan below
awaiting go-ahead. Nothing implemented yet. The companion feature (the console as
an MCP *server*, `console-mcp-server.md`) shipped first (0.248.0–0.256.0) and is
treated here as work to **preserve**, not touch.

---

## Goal

Ship runspec-console with the AI/agent features hideable and disableable, so the
**same binary** can run as a non-AI, GUI-only fleet tool. Two motives:

1. **Onboarding.** Give Claude Code / runspec users a non-AI way to get
   comfortable with the runspec concept (hosts, credentials, forms, fleet
   runnables, schedules) before adopting the full agentic console.
2. **Corporate robustness pitch.** Let a security/platform team vet the
   fleet-execution substrate (SSH transport, credentials/keychain, denylist,
   `enforce_run_as`, config-sync/seed) with **zero AI in the loop**, then enable
   the agent later as a seeded flag flip on an architecture they already ran.

The value proposition when AI is off is coherent on its own: **a GUI for
self-serve fleet runnables across local + remote venvs, with Forms, History,
saved runs, schedules, rule-mode triggers, rooms, and the console-as-MCP-server
gate for an *external* coding agent.**

---

## Survey results — the open questions, answered

The note's original open questions, checked against the code (0.256.0):

| Question | Finding | Consequence |
|---|---|---|
| **Bring-up eagerness** | **Lazy.** `bridge_internals._get_adapter` is the *only* place an adapter is built and returns `None` when `[llm] provider` is unset. `Bridge.__init__` stores `_adapter = None`; `app.py main()` never touches the LLM. Plugin adapter imports (`_ensure_plugins_imported`) run only from `_get_adapter` / `list_providers`. | Layer B is a guard at one chokepoint plus refusals at the trigger/schedule validators. **No bring-up refactor.** |
| **Dependency gating** | **Already done.** `anthropic`/`openai`/`bedrock`/`langserve` are extras; each factory in `adapters/base.py` imports its SDK lazily and raises a "missing extra" error. `mcp`, `otel`, `sandbox` are extras too. A plain `pip install runspec-console` ships **no LLM SDK**. | Layer C is documentation + an optional convenience extra, not a refactor. |
| **Lock vs default** | Config-seed and Config-Sync **overlay** `config.toml` (repo/seed wins, local-only keys survive). `preserve` does the *opposite* of a lock — it pins the **local** value. | The original note's "pinned with a `preserve` entry" is backwards. A seeded `ai = false` must **not** be in `preserve`; then every seed re-apply (package upgrade) and sync pull re-asserts it. That is a re-asserting default, not a lock. The only true lock is Layer C: no model extra / plugin installed. |
| **Naming / positioning** | `[console.theme] brand_name` already renders the sidebar title; `branding.py` derives title/docs from the active LLM plugin dist (falls through to defaults with no provider). | Nothing new required. A seed can pair `ai = false` with a `brand_name`. |
| **Interaction with MCP server** | The MCP server publishes runnables (+ opt-in console built-ins) to an *external* agent through the console's autonomy gate. None of the exposed built-ins (`_MCP_SERVER_BUILTIN_NAMES`: status, history, memory, runbook-read, outlook, action tools, download/relay) calls the model; `run_runbook` only renders steps as text. | **Keep the MCP server fully working with AI off.** It is the "external agent, no AI inside" demo. `tool_style`, `expose_builtins`, `tools/list_changed`, cross-host/venv dedup: untouched. |

Two things the original note got wrong about the UI, also from the survey:

- **There is no Rooms tab any more.** Room transport (join, read, send) is
  AI-free (`start_chat` needs only SSH + a host entry); the agent-coupled parts
  are the helpdesk queue cards, the claim flow, `propose_reply` drafting (all
  agent-mode chat *triggers*) and the **runbook** assignment path.
- **The Console tab cannot be hidden.** It is the output pane for *every* run —
  Forms, the command bar, saved runs, History rerun all dispatch
  `runspec:invoke_runnable` into `ConsoleView`. Only its **chat half** is AI:
  plain-text-to-chat routing in `CommandInput`, the `$` runbook menu, the
  History (conversations) and New chat buttons, Stop, "Ask the assistant" on a
  run card, the include-in-context toggle, the token footer, the sidebar
  context gauge.

---

## The switch

```toml
[console]
ai = false        # absent / non-bool ⇒ true (default install byte-identical)
```

- **Read once at bring-up** (`Bridge.__init__`, *after* `seed_apply()` so a
  seed's value applies on first launch) into `Bridge._ai_enabled`. A mid-session
  flip (sync pull, hand edit) takes effect on restart — the UI and the backend
  gates must agree for the whole session, and "the tab I was using vanished" is
  worse than "restart to apply". The Settings notice says so.
- **Config-only.** No Settings toggle. The flag is a governance default that
  rides the seed/sync; a UI switch invites the local flip the seed exists to
  discourage. A read-only line in Settings ("AI features are off —
  `[console] ai = false`, restart after changing") plus a docs link is enough.
  Onboarding users flip it by editing `config.toml`, which is the same step as
  configuring `[llm]` anyway.
- **Not in `preserve`** (see survey). Not in `_SEED_PRESERVE` either.
- **Independent of "no provider configured".** Today an unset `[llm] provider`
  makes chat say "no model provider configured". That state stays; `ai = false`
  is a different, explicit message: *AI features are disabled in this console
  (`[console] ai = false`).*

Two tiers, restated correctly:

- **Soft off** = `[console] ai = false` via seed/sync. Re-asserted on every
  seed re-apply and sync pull; a local flip survives only until then.
- **Hard off** = no model extra (`anthropic`/`openai`/`bedrock`/`langserve`)
  and no plugin adapter installed. Already the state of a bare
  `pip install runspec-console`. The code path exists but cannot bootstrap.

---

## Inventory — what the flag gates

### Backend (Layer B) — refuse, don't just hide

| # | Surface | Where | Behaviour when off |
|---|---|---|---|
| B1 | Adapter construction | `bridge_internals._get_adapter` | Return `None` without reading `[llm]` or importing plugins. Every caller already handles `None`. |
| B2 | Chat turn | `bridge_invoke.send_chat` → `_spawn_chat_turn` | Return a done card carrying the AI-off message instead of "no provider". |
| B3 | Runbook run | `bridge_invoke.run_runbook` | Same refusal (runbooks execute as agent turns). |
| B4 | Chat-history compaction | `_summarize_middle_turns` | Already falls back to the mechanical note when the adapter is `None`. No change. |
| B5 | Trigger validation | `triggers.Trigger.from_config` (via `load_triggers`) | New `ai_enabled` parameter (pure module, no config read). `mode = "agent"` ⇒ per-trigger `ValueError` "agent-mode triggers are disabled ([console] ai = false)"; non-empty `pre_turn` ⇒ same. The trigger shows as *invalid* in the Triggers tab, never silently degrades to rule mode. `validate_rota` unaffected. |
| B6 | Trigger runtime (defence in depth) | `bridge_triggers._trigger_run_agent_turn`, `_trigger_run_pre_turn` | Early return with the AI-off message (covers `test_trigger`, `submit_paste_input`, `claim_email`, the claim poll loop). |
| B7 | Schedule validation | `sources/schedule.parse_schedule_entry` | New `ai_enabled` parameter. `prompt` / `runbook` (and legacy `skill`) / `eval` ⇒ rejected with the AI-off reason; the job is listed as invalid, not dropped silently. |
| B8 | Schedule writes | `bridge_schedules.create_schedule` / `update_schedule` | Reject those actions up front (today they do no validation; the `create_schedule` **action tool is reachable over the MCP server**, so this is a real door). `run_schedule_now` needs nothing — a refused job is never in the list. |
| B9 | Scheduled agent / eval callbacks | `_run_scheduled_agent`, `_run_scheduled_eval` | Early return with the AI-off message (defence in depth). |
| B10 | Room runbook assignment | `bridge_triggers._run_runbook_assignment` | Refuse with the AI-off message; the runnable assignment path `_run_assignment` (gated runnable, no model) **stays**. |
| B11 | Room catalog | `_build_room_runbooks` | Return `[]` so no `skill:<id>` entries are published. Runnable catalog publishing stays. |
| B12 | Evals | `bridge_evals.run_evals` | Return an error result with the AI-off message. `list_eval_fixtures`/save stay (fixtures ride Config Sync). |
| B13 | Improve synthesis | `bridge_improve.synthesize_improve_report` | Refuse with the AI-off message. `run_improve_report` (pure mining) stays. |
| B14 | Action tool schema | `bridge_agent_tools._action_tools` (`create_schedule`) | Drop `prompt`/`runbook`/`eval` from the `action` enum when off — this schema is what the MCP server publishes under `expose_builtins`. |
| B15 | App bring-up | `app.py main()` | Skip `bridge.mcp_start()` (MCP *client* — only the agent consumes it), `observability.init_tracing` (only agent turns are traced), and the sandbox warm-up. `mcp_server_start()`, branding, everything else: unchanged. |
| B16 | New JS API | `ai_enabled() -> bool` (next to `brand_theme` in `bridge_config_ssh.py`) | Added to `tests/test_bridge_api_surface.py` `EXPECTED`. |
| B17 | Telemetry test trace | `send_test_trace` | Reports "AI off" instead of emitting (tab is hidden anyway). |

Untouched on purpose: `_custom_agent_tools` / `_runnables_to_tools` (only
reached from a turn or from the MCP server, which we keep), `McpManager` /
`ConsoleMcpServer` construction in `Bridge.__init__` (cheap, no sockets),
`_sweep_workspaces`, the conversations store, memory/runbook stores and their
`get_*`/`save_*` methods, `start_chat` (rooms), `_publish_catalog`, denylist,
credentials, transfer, saved runs, History, `get_config`.

### Frontend (Layer A) — hide

The frontend learns the flag once at mount: `bridge.ai_enabled()` beside the
existing `brand_theme` read, `.catch(() => true)` so an older backend is
unchanged. Delivered via a small `FeaturesContext` (`useFeatures().ai`) rather
than prop-drilling through six components. Mock returns `true`.

| # | Surface | File | When off |
|---|---|---|---|
| A1 | Nav tabs | `App.tsx` `navTabs` (static array) | Drop **Improve**, **Evals**. Keep Console, History, Logs, Analytics, Specs, Forms, Schedules, Triggers, Rota, Dev. Extract `visibleNavTabs(tabs, features)` as a pure, tested helper. |
| A2 | Sidebar context gauge | `App.tsx` `runspec:context_estimate` block | Never rendered (no chat reports usage anyway; gate explicitly so the AI-only nudges can't appear). |
| A3 | Discovery banner copy | `App.tsx` "The assistant sees more runnables as they load." | Neutral wording. |
| A4 | Command bar | `CommandInput.tsx` | Plain text no longer sends chat: show an inline hint "AI is off — type `/` for runnables, `>` for saved runs". Hide the `$` runbook menu and the Stop control. Placeholder drops "or ask the assistant…". |
| A5 | Console header | `ConsoleView.tsx` | Hide **History** (conversations) and **New chat**; keep Collapse/Expand all. Ignore `runspec:send_chat` / `runspec:run_runbook`. |
| A6 | Run cards | `OutputPanel.tsx` | Hide "Ask the assistant" and the include-in-context toggle (pass `onAskLlm`/`onToggleContext` as `undefined` — both already optional). The `ActivityIndicator` stays (it also tracks human runs). |
| A7 | Settings tabs | `SettingsDrawer.tsx` `items` (static array) | Drop **Model / API**, **Agent tools**, **Runbooks**, **MCP** (client), **Chat History**, **Telemetry**. Keep SSH, Jump Hosts, Local venvs, Saved runs, Self-Service, Filters, Repos, Credentials, Denylist, **MCP Server**, Config Sync, Mailbox, Teams, Sounds. Add the read-only AI-off notice at the top of the drawer. Extract `visibleSettingsTabs(items, features)` as a pure, tested helper. |
| A8 | Triggers editor | `TriggersEditor.tsx` | Hide the Agent/Rule `Segmented` (fixed to rule), `pre_turn`, `rules`, runnable allowlist, `action_allow`, `reply_draft`/`reply_style`. New-trigger default becomes `mode: 'rule'`. `once_per_thread` stays (it gates rule-mode replies too). Existing agent triggers render with their backend validation error, like any invalid trigger. |
| A9 | Schedules editor | `SchedulesView.tsx` action `Select` | Drop *Run a prompt*, *Run a runbook*, *Run evals*. Table rows for refused jobs show the backend's invalid reason. |
| A10 | Rooms / helpdesk | `TriggersView.tsx` chat pane, Settings → Self-Service | Keep the chat pane and reply composer; the `propose_reply` draft and claim cards simply never arrive (those are agent-mode triggers, refused in B5). `SelfServiceTab` copy that mentions the agent gets neutral wording. |
| A11 | History view | `HistoryView.tsx` | Unchanged — `initiatedBy: llm` rows are historical data. |
| A12 | Sounds tab | `SoundsTab` | Unchanged (`reply`/`message` categories are harmless). |

### Layer C — documentation (+ one optional convenience extra)

- Document *hard off* as "install without a model extra" and that `[all]`
  includes the model extras. Optionally add an `ai` aggregate extra
  (`runspec-console[ai]` = anthropic, openai, bedrock, langserve, sandbox, otel,
  mcp) so the enable-later story is one pip line. No code moves.

---

## Phased delivery

Each phase is one PR based on fresh `origin/main`, behind the default-true flag,
so a default install stays byte-identical at every step.

**Phase 1 — backend gate (0.257.0).** B1–B17, `config.ai_enabled()`, the JS API
method, tests, reference-doc row, CHANGELOG. Frontend untouched: with `ai =
false` the UI still shows the AI tabs but every AI action returns the AI-off
message. Reviewable on its own and already useful for the governance story
(automation can't run agent turns).

**Phase 2 — frontend gate (0.258.0).** A1–A12, `FeaturesContext`, the two pure
tab-filter helpers with tests, `CommandInput` behaviour test, mock update,
neutral copy sweep. Plus a new docs page *"Console without AI"* (nav: after
*Get started*) covering the seed rollout recipe and the restart-to-apply rule,
and the CLAUDE.md status-row entry.

**Phase 3 (optional) — Layer C polish.** The `ai` convenience extra + README /
quickstart wording. Can fold into Phase 2 if small.

### Seed rollout recipe (goes in the docs page)

```toml
# config_seed/config.toml in the corporate package
[console]
ai = false
[console.theme]
brand_name = "Acme Fleet"      # optional: present as its own tool
```

Later, ship a seed with `ai = true` + the `[ai]` extra in the package's
dependencies; the *same* installed console lights up the agent on its next
upgrade. Because `console.ai` is not preserved, both transitions re-assert on
every seed re-apply and sync pull.

---

## Tests (what each phase adds)

Phase 1 (Python):
- `test_config.py`: `ai_enabled()` — absent ⇒ True, `false` ⇒ False, garbage ⇒ True.
- `test_triggers.py`: `from_config(..., ai_enabled=False)` refuses `mode="agent"` and non-empty `pre_turn`; rule triggers still load.
- `test_schedule.py`: `parse_schedule_entry(..., ai_enabled=False)` rejects `prompt`/`runbook`/`skill`/`eval`; `runnable`/`saved_run`/`event` still parse.
- Bridge tests: `_get_adapter()` is `None` and never imports plugins when off; `send_chat`/`run_runbook`/`run_evals`/`synthesize_improve_report` return the AI-off message; `create_schedule` rejects `prompt`; `_build_room_runbooks` is empty; the `create_schedule` action-tool enum shrinks (and therefore the MCP-server built-in schema).
- `test_bridge_api_surface.py`: `EXPECTED += {"ai_enabled"}`.
- `test_app` (if bring-up is unit-tested; otherwise a small extracted `_bring_up_ai(bridge, enabled)` helper): `mcp_start`/`init_tracing` not called when off; `mcp_server_start` still called.

Phase 2 (vitest):
- `visibleNavTabs` / `visibleSettingsTabs` helpers.
- `CommandInput`: plain text with AI off shows the hint and never calls `onSendChat`; `/` still runs.
- `SchedulesView` action options with AI off.
- Existing tab-component tests (`LlmTab`, `McpTab`, …) render the component directly and stay green.

---

## Preservation checklist (recently merged work that must not regress)

- **MCP server** (0.248.0–0.256.0): stays enabled with AI off; `tool_style`
  (`leaf`/`runnable`), cross-host + cross-venv collapse, `tools/list_changed`
  push on profile switch, 0600 discovery file, `expose_builtins` — all
  untouched. The only change it sees is B14 (a smaller `create_schedule` enum).
- **Profiles / host groups / saved runs / Forms arg-bindings / credentials by
  scope**: not on any gated path.
- **Config seed / Config Sync**: no new preserve entries; `console.ai` is an
  ordinary `config.toml` key that overlays like the rest of `[console]`.
- **`tests/test_bridge_api_surface.py`**: exactly one new public method.
- **White-label**: `brand_theme` unchanged; Phase 2 reuses its mount pattern.

---

## Decisions to confirm before Phase 1

1. **Rooms stay on** with AI off (transport + runnable enactment + operator
   catalog, minus runbooks). Alternative: gate `start_chat` and hide
   Self-Service too — simpler story, but loses the "portal without AI" demo.
2. **Config-only switch, restart to apply** (no Settings toggle, no live flip).
3. **MCP server stays on**, including `expose_builtins`, with AI off.
4. **Refuse, don't degrade**: an agent-mode trigger / prompt schedule in a
   seeded file shows as *invalid* rather than being skipped quietly.
5. **Two PRs** (backend, then frontend) rather than one.

---

## Follow-up refinements (0.264.0)

After the first real use of AI-off mode, four adjustments (the inventory tables
above predate them):

- **Hide Triggers and Rota nav tabs** (`features.AI_OFF_HIDDEN_VIEWS`, filtered
  in `visibleNavTabs` alongside `AI_ONLY_VIEWS`). Triggers actions lean on the
  agent, and a rule-only trigger doesn't earn a top-level tab; Rota is presence
  governance with no agent turns to gate. A seeded rule trigger still runs
  headless (backend unchanged) — this is UI curation, not a new backend gate.
  Supersedes A1's "keep Triggers, Rota" and A8 (the editor is no longer reached).
- **Rota gate inert** (`bridge_triggers._build_clock_ok` / `_build_lock_ok`
  short-circuit to always-`True` when `_ai_enabled` is false). Lock auto-suspend
  defaults *on*, so with the Rota tab hidden a stale/seeded rota could otherwise
  silently suppress a plain runnable schedule.
- **Hide the "Emit an event" schedule action**
  (`features.AI_OFF_HIDDEN_SCHEDULE_ACTIONS`). `event` needs no AI so the backend
  still accepts it, but its only consumer is a `source="schedule"` Trigger — dead
  with Triggers hidden. UI-only (distinct from `AI_SCHEDULE_ACTIONS`, which
  mirrors a real backend refusal).
- **No advertising.** `AI_OFF_MESSAGE`, the command-bar hint, and the
  trigger/schedule validation errors are neutral — they never name the `[console]
  ai` switch or how to re-enable it. A locked-down console reads as a fleet tool
  with no AI, not one hiding AI behind a flag.

Also in 0.264.0 (unrelated to AI-off, but shipped together): the default config
directory became **venv-scoped** (`{sys.prefix}/runspec-console`) so multiple
installs isolate without `--config-dir`, with a one-time `%APPDATA%` adoption on
upgrade (`config.migrate_legacy_profile`). See `docs/console-reference.md`.

## Explicitly out of scope

- A browser-accessible / headless / always-on web daemon. The operator confirmed
  they want a GUI, not always-on; the desktop console is the vehicle.
- A UI toggle for the flag, or an env-var / CLI override.
- Moving any module or SDK import (Layer C is already satisfied).
