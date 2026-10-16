# Design: deferred built-ins in search mode (the built-in search tier)

**Status:** Built in `runspec-console` 0.190.0 (chat turns only; headless
turns deliberately unchanged). Companion features on the same branch:
`search_history` (0.189.0) — the read-only run-history agent tool that
prompted the "how many built-ins is too many" question this design answers.

Companion doc: `agent-tool-search.md` — the original search mode
(`[llm] tool_mode = "search"`) that hid the per-runnable tools behind
discovery meta-tools. This tier applies the same medicine to the console's
own built-in tools.

## 1. Context & motivation

The chat agent's built-in (non-runnable-backed) tools grew one genuinely
useful tool at a time — Outlook (15), action tools (8), workspace (7), repo
(7), config (5), memory/runbooks (4), transfer/denylist (3), exec (2), status
(1), history (1) — to **~53 schemas, an estimated 12–18k tokens of tool
definitions on every chat turn**. That is the same growth curve the
per-runnable tools were on before search mode, with the same three costs:

- **Context occupancy.** The tool block crowds out conversation history.
- **Cache churn.** Several families are conditional (Outlook only when COM is
  reachable, repo tools only with registered repos, transfer tools only with
  remotes) and several schemas embed live enums (hosts, filters, credential
  ids). The cached tools+system prefix therefore differs across machines and
  invalidates when fleet config changes.
- **Marginal-tool anxiety.** Every new built-in — `search_history` being the
  trigger for this design — permanently taxes every future turn, which is the
  wrong incentive for adding small, useful tools.

Search mode already solved this for runnables: constant ~3-tool footprint,
schemas on demand, an always-on folded index for discovery, and — the part
that matters most here — **gating on the inner thing that actually runs**
(`run_runnable` resolves the runnable first, then the autonomy gate judges
*it*). This tier reuses all four ideas.

## 2. Goals / non-goals

Goals:

- In search mode, the chat turn's tool list stops scaling with the number of
  built-ins: a small **hot kernel** inline, everything else deferred.
- A deferred tool behaves **identically** to an inline one once invoked —
  same confirm prompt, same Console card, same audit record, same handler.
- Discovery stays cheap: the agent must know a deferred tool *exists* without
  a round-trip, and get its schema in one call when needed.
- Zero behavior change for: dedup/per_host modes, headless (trigger/schedule)
  turns, and search-mode deployments that opt out.

Non-goals:

- No keyword search over built-ins. ~40 names fit in an enum and a folded
  menu; `search_runnables`-style ranking would be machinery without a payoff.
- No deferral for headless turns (see §6 — it would be a security regression,
  not an optimisation).
- No change to the `action_allow` vocabulary, `list_action_tools`, or the
  Settings UI.

## 3. The split: hot kernel vs long tail

`_HOT_BUILTIN_NAMES` (in `bridge_const.py`) stays **inline** even in search
mode:

| Family | Why it can't defer |
|---|---|
| `console_status` | One tiny schema; the "what time is it / is the rota on" reflex. |
| `search_history` | Small, read-only, used mid-conversation ("did it run? what did it output?"). |
| memory/runbooks (`remember`, `search_memory`, `save_runbook`, `run_runbook`) | The system prompt references pinned memory; recall must be zero-hop. The folded index advertises every runbook by symptom, so the tool that reads one must be too (0.202.0 — `load_runbook` was folded into `run_runbook`). |
| `list_files` (0.196.0) | Read-only browsing — the entry point of most file tasks; zero-hop so the agent finds exact paths before reaching for the deferred transfer tools. |
| `workspace_*` (7) | **Load-bearing:** the spill envelope tells the model "the full output is in `tool-results/…`, read it with `workspace_read`/`workspace_grep`". A discovery hop between "output spilled" and "read it back" would break the console's large-output story. |

Everything else in `_BUILTIN_TOOL_NAMES` is the **deferred long tail**:
Outlook, repo, transfer + `manage_denylist`, action tools,
`run_python_sandboxed`/`run_sql_sandboxed`, config tools. The split is computed from the *live* tool list
(`_deferred_builtin_tools()` filters `_custom_agent_tools()`), so it is
availability-aware — a tool that wouldn't have been offered inline (Outlook
off-Windows, transfer with no remotes) is not offered through the tier
either, and the meta-tool enums never name a tool that can't run.

## 4. The two meta-tools

Both are built per-turn by `_builtin_meta_tools(deferred)` with their name
enums drawn from the deferred set actually available right now.

### 4.1 `describe_tool` (read-only, autonomous)

`{names: [enum of deferred names]}` → full `{name, description,
input_schema}` for each requested tool, JSON. Batch-friendly; an unknown name
comes back as a per-name error row so one typo doesn't sink the batch.
Classified `autonomous` in `_effective_tool_autonomy` — it is a schema
lookup, the exact analogue of `describe_runnable`.

### 4.2 `use_tool` (an envelope with three name roles)

`{name: enum of deferred names, input: object}`. This is *not* an executor —
it is an envelope, and — following the `run_runnable` precedent exactly — the
raw call and the inner call each have jobs the other cannot do:

```
_gated_run_tool / _gated_run_tool_async
  if tool_name == "use_tool":
      unwrapped = _unwrap_use_tool(tool_input)   # validate against the live deferred set
      if isinstance(unwrapped, str): return it    # readable error, nothing ran
      inner_call = unwrapped                      # resolved, NOT applied yet
  decision  ← _effective_tool_autonomy("use_tool", …) → recurses into the INNER tool
  prompt    ← _register_confirm(RAW name/input)   # frontend segment-matching contract
  card      ← _confirm_card_view → INNER name/input (display_name/display_input)
  execution ← tool_name, tool_input = inner_call  # applied after approval
  ...Console tracking, audit, dispatch — all on the inner name
```

- **Decision — inner.** `_effective_tool_autonomy("use_tool", …)` resolves the
  wrapper and recurses, so a confirm-gated `outlook_send` still prompts and
  `manage_denylist action=list` still runs free.
- **Prompt — raw.** `_register_confirm` must carry the raw `use_tool`
  name/input verbatim: the frontend attaches the Approve/Deny buttons to the
  running tool segment by exact name match (`segment.entry.name == tc.name`,
  set by `runspec:tool_start`). Renaming here is the documented `run_runnable`
  bug (the `_register_confirm` comment) — the prompt never renders and the
  call times out as a deny after `_CONFIRM_TIMEOUT_S`. (0.190.0 shipped with
  exactly that regression — a gate-top rename; 0.190.1 restored the contract.)
- **Card — inner.** `_confirm_card_view` unwraps, so the operator reads and
  approves the real tool + args. A display name differing from the raw name
  also makes the frontend render the confirm **read-only** — the same
  mechanism that keeps `run_runnable` edits off; a defensive backend branch
  applies any edit to the inner input (the shape the card displayed) anyway.
- **Input — preflighted (0.196.0).** `_unwrap_use_tool` validates the inner
  input against the deferred tool's real schema (unknown / missing-required /
  enum, top level plus string-array enums) and bounces a readable error with
  the valid shape before any confirm — the `run_runnable`
  `_preflight_validate` parallel. Search mode surfaces no per-tool schema up
  front, so without this a model that skipped `describe_tool` got opaque
  handler errors or silently ignored fields.
- **Execution — inner.** The resolved call is applied after the gate passes,
  so `_run_builtin_tool_tracked` files the Console card and audit record under
  the inner name and the per-tool handlers run unmodified.

`_unwrap_use_tool` only resolves names in the live deferred set — never a
hot-kernel tool (already inline), never a runnable (that's `run_runnable`'s
job), never `use_tool` itself (no nesting).

## 5. The invariant: fail closed everywhere else

Resolving in the gates is the designed path. Two backstops make skipping it
safe rather than exploitable:

- `_effective_tool_autonomy("use_tool", …)` resolves the inner tool's level;
  an **unresolvable** wrapper (unknown/hot/nested name) classifies `"confirm"`
  — gated by default, never silently autonomous.
- `_run_tool_sync("use_tool", …)` → **refuses** with an error string instead
  of executing. Reaching dispatch still wrapped means a call site skipped the
  gate; executing there would run the inner tool ungated. The error is the
  tripwire that surfaces the missing unwrap in the same turn.

Anyone adding a new entry point that runs tools by name inherits these
backstops for free — the worst outcome of forgetting the unwrap is a refusal,
not a bypass.

## 6. Headless turns: deferred only when unscoped

*(Changed in console 0.346.0; until then every headless turn kept every
built-in inline.)*

A trigger/schedule agent turn **with an `action_allow`** keeps every built-in
inline (`_runnables_to_tools(…, inline_builtins=True)`), even in search mode:

- `action_allow` (the per-trigger allowlist of governed action/Outlook/MCP
  tools) works by **filtering the tool list** (`_filter_headless_tools`). It
  can only narrow what it can see; a run-tools-by-name wrapper would let a turn
  reach any governed tool regardless of its allowlist. So a scoped turn has
  `describe_tool`/`use_tool` stripped.

A turn **with no `action_allow`** (and no `runnables` allowlist) has nothing to
narrow, so it gets chat's short list: hot kernel inline, the long tail behind
`describe_tool`/`use_tool`, and the deferred-tools menu in its system prompt.
On a Windows console with Outlook and external MCP servers this drops roughly
30k tokens of schemas from every model call of the turn. The chat-only tools
stay out either way:

- `_runnables_to_tools(headless=True)` drops `_HEADLESS_EXCLUDED_NAMES`
  (config tools + `load_runbook`) before the hot/deferred split, so they are
  neither inline nor in the `use_tool` enum or the menu.
- `_headless_use_tool_refusal` refuses a `use_tool` call naming one of them
  when the turn's run origin is a trigger/schedule (the wrapper resolves against
  the full deferred set), on top of the `_run_config_tool` origin guard.

## 7. Discovery: the folded menu

Search mode's lesson (`agent-tool-search.md`, "always-on catalog index"): an
agent can only search for what it thinks to search for — it needs to see the
menu. Deferred built-ins ride the exact same mechanism:

- `_build_builtin_index()` — one `- name — first sentence` line per deferred
  tool, derived from each tool's real schema description (no hand-maintained
  list to drift).
- `_chat_index_text()` composes the runnable catalog index and, when
  built-ins are deferred, a labelled deferred-tools section explaining the
  `describe_tool` → `use_tool` flow, then rides the existing
  `_fold_catalog_index` unchanged — prepended to the first user message of a
  per-call copy, so the cached tools+system prefix and the stored history are
  both untouched.
- Same knobs as the runnable index: only in search mode, off with
  `[llm] tool_index = false`, and empty when built-ins are inline (their
  schemas are already visible).

Net shape of a search-mode chat turn: 3 runnable meta-tools + ~13 hot-kernel
tools + 2 builtin meta-tools ≈ **18 schemas**, constant as built-ins grow —
a new built-in costs one folded-menu line, closing the marginal-tool problem
from §1. The prefix is also *stable*: the conditional families and live enums
that churned the cache now live behind `describe_tool` results instead of in
the cached prefix.

## 8. Config & rollout

- Applies only when `[llm] tool_mode = "search"` — the same opt-in that
  already accepted the discover-then-act trade-off for runnables. Dedup (the
  default) and per_host are byte-identical to before.
- `[llm] defer_builtins = false` — zero-code escape hatch: search mode with
  every built-in inline (the pre-0.190.0 layout), mirroring the
  `tool_mode = "per_host"` escape-hatch precedent.
- No new UI. No storage. No migration.

## 9. Files & tests

| File | Change |
|---|---|
| `bridge_const.py` | `_BUILTIN_META_NAMES`, `_HOT_BUILTIN_NAMES` |
| `bridge_agent_tools.py` | `_defer_builtins_enabled()`; `_runnables_to_tools(allow, inline_builtins=False)` splits hot/deferred in the search branch |
| `bridge_search.py` | `_deferred_builtin_tools`, `_builtin_meta_tools`, `_run_describe_builtin_tool`, `_unwrap_use_tool`, `_build_builtin_index`, `_chat_index_text` |
| `bridge_autonomy.py` | both gates resolve `use_tool` early, prompt under the raw name, execute the inner call post-approval; `_effective_tool_autonomy` recurses into the inner tool; `_confirm_card_view` unwraps for display; dispatch branch for `describe_tool`; fail-closed refusal for a still-wrapped `use_tool` |
| `bridge_invoke.py` | chat turn folds `_chat_index_text()` |
| `bridge_triggers.py` | headless turns defer like chat unless `action_allow`/`runnables` scope them (then `inline_builtins=True`); `_filter_headless_tools` strips `_BUILTIN_META_NAMES` only when `action_allow` is set |

`tests/test_builtin_search_tier.py` covers the layout split (hot inline /
tail deferred / enums exact), the escape hatches (`inline_builtins`,
`defer_builtins=false`, dedup untouched), `describe_tool` (schemas, per-name
errors), `_unwrap_use_tool` (valid / hot / unknown / nested / missing),
gate integration (decision + dispatch on the *inner* name; unwrap errors run
nothing), the fail-closed dispatch backstop, the headless meta-tool strip,
and the folded menu (content, gating knobs, stored-history purity).

## 10. Future directions

- **Hot-kernel tuning.** The kernel is a code constant, chosen from usage
  reasoning rather than telemetry. If real transcripts show a deferred tool
  called on most turns (or a kernel tool never called), move it — one
  frozenset edit, no schema or gating changes.
- **Deferring per-runnable *credential enums*.** Dedup mode still embeds live
  credential-id enums per runnable; the same describe-on-demand idea could
  stabilise that part of the prefix too.
- **A `[llm] hot_tools` override** (names to force inline/deferred per
  deployment) if the one-size kernel proves wrong somewhere — deliberately
  not built now; knobs are easier to add than remove.
