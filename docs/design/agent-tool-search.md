# Design: agent tool **search** mode (`tool_mode = "search"`)

Status: implemented in runspec-console 0.86.0 (this doc is the original design).
Package: `runspec-console` (`packages/python/runspec-console/`)
Builds on: the dedup tool layout shipped in 0.85.0 (`[llm] tool_mode = "dedup" | "per_host"`).

> Implementation notes / deltas from the original design:
> - **Triggers fall back to dedup.** Rather than make `search_runnables` honour the
>   allowlist (design §8), search mode applies only to the unrestricted manual-chat
>   tool list; an agent trigger that passes an `allow` set keeps the scoped dedup
>   layout. A trigger curated to a handful of runnables gains nothing from a search
>   interface, and this keeps the meta-tool executors allowlist-free.
> - **Pre-flight is lightweight structural validation** (required / choice /
>   int+float / range / unknown-arg / require-command), per §5.2's recommendation;
>   core's validator is not reused (the cached `rawSpec` excludes args/commands).
>   Anything subtler still bounces from the remote.
> - The unwrap (design §6) lives in `_run_tool_sync` (dispatch), `_effective_tool_autonomy`
>   (gate), and `_confirm_card_view` (UI), plus `_resolve_id_host` for id→runnable.

---

## 1. Context & motivation

The console exposes each fleet runnable to the chat agent as an LLM tool. The tool
list is re-sent to the model **on every turn**, so its size is a standing context
cost.

**0.85.0 (dedup)** collapsed the *host axis*: a uniform fleet of N hosts running
the same M runnables went from N×M tools (`host__runnable`) to M tools with a
required `host` enum. An 18×20 fleet: 360 → 20 tools. (Until 0.254.0 the deduped
tool was named `group__runnable` and keyed on `(group, name)`, which re-split a
runnable that ships in more than one venv/package; it is now keyed and named by
the runnable name alone — see [Multiple local venvs](../console-hosts.md).)

What dedup does **not** address is the *breadth axis* — the number of **distinct**
runnables. A realistic fleet installs several packages: `runspec-linux` (21) +
`runspec-windows` (35) + `runspec-jsm` (8) + `runspec-snow` (8) + local ones ≈
**70–100 distinct deduped tools**, each still carrying a full `input_schema` every
turn. Search mode collapses that axis: replace the per-runnable tools with a small,
fixed set of **meta-tools**, and let the agent *discover* runnables on demand.

**Why this is a good fit here specifically:** every runnable is `runspec.toml`-based,
so each arg is a **typed, parsed, already-cached contract** (`type`, `options`,
`default`, `required`, `range`, `description`, `position`, `autonomy`,
`password`-typed, plus `commands`/`requireCommand`). Unlike a generic tool-search
over free-text tool descriptions, we don't *lose* schema fidelity in search mode —
we **defer** it from "all tools, every turn" to "matched tools, on search." Two
levers (structured search results + client-side pre-flight validation, §5) make
first-call argument accuracy *as good as or better than* the dedup layout while
shrinking the per-turn tool block to ~3–5 entries.

This is the option-1 ("tool search / lazy loading") path noted as future work when
dedup (option 2) shipped. The dedup refactor deliberately left the seam: host is
already an argument, and routing/schema/dispatch are already factored into reusable
methods.

---

## 2. Goals / non-goals

**Goals**
- Per-turn tool block size is ~constant (≈3–5 tools) regardless of fleet breadth.
- Provider-agnostic — works across all adapters (Anthropic, OpenAI, Bedrock,
  LangServe, plugins), so implemented in the console, **not** via any provider's
  native tool-search beta.
- Preserve argument accuracy using the structured runspec metadata.
- Preserve every existing guarantee unchanged: autonomy gate, credential
  auto-apply/override, password→env secret split, `run_as`, positionals,
  subcommands, trigger allowlists, audit/summary records.
- Opt-in and reversible via the existing `[llm] tool_mode` knob.

**Non-goals (for the first cut)**
- Dynamic per-turn injection of full typed schemas (the "working set" approach) —
  kept as a documented phase-2 fallback (§14), only if accuracy disappoints.
- Changing the `runspec.toml` format, `spec/SPEC.md`, or `schema/runspec.schema.json`.
  This is console-internal.
- Semantic/embedding search. Start with deterministic lexical ranking.

---

## 3. Architecture

Add a third value to `_tool_mode()`: `"search"` (alongside `"dedup"` default and
`"per_host"`). In search mode, `_runnables_to_tools()` returns **only**:

1. `search_runnables` — discover runnables (returns structured, typed rows).
2. `run_runnable` — the generic executor (id + host + args).
3. `describe_runnable` — full JSON Schema for one runnable, on demand (optional but
   recommended).
4. The existing built-in file-transfer tools (`download_file`, `relay_file`) — left
   as-is (they're few and always relevant).

The per-runnable runnable tools are **not** emitted. The agent's loop becomes:
`search_runnables(query)` → read typed rows → `run_runnable(id, host, args)` (→ on a
validation bounce, correct and retry; → optionally `describe_runnable(id)` if it
needs the exact schema first).

The model is told this protocol via a short addition to the agent system prompt in
search mode (e.g. "Tools are discovered: call `search_runnables` to find a runnable,
then `run_runnable` to execute it. You may call `search_runnables` multiple times to
refine.").

---

## 4. Meta-tool contracts

### 4.1 `search_runnables`

```jsonc
// input_schema
{
  "type": "object",
  "properties": {
    "query": { "type": "string",
      "description": "Keywords matched against runnable name, description, and group/package." },
    "host":  { "type": "string", "description": "Optional: only runnables available on this host." },
    "group": { "type": "string", "description": "Optional: only runnables from this package/folder." }
  },
  "required": ["query"]
}
```

**Result** (returned as the tool-result string — JSON the model can parse): a capped
list (default top 10) of structured rows built from `_runnables_cache`. Each row:

```jsonc
{
  "id": "fleet__drain-node",        // == the dedup tool name; the run_runnable handle
  "name": "drain-node",
  "group": "runspec-linux",
  "hosts": ["web01", "web02", "db01"],
  "description": "Cordon and drain a node",
  "autonomy": "confirm",
  "args": [                          // structured, typed — see §5
    { "name": "node",   "type": "str",    "required": true },
    { "name": "region", "type": "choice", "options": ["us","eu"], "default": "us" },
    { "name": "count",  "type": "int",    "minimum": 1, "maximum": 10, "default": 3 }
  ],
  "command": { "required": true, "options": ["drain","list"] }  // present only if the runnable has subcommands
}
```

Notes:
- `id` is exactly `self._dedup_tool_name(rep)` so the existing routing applies
  verbatim (§6). Rows are grouped by `(group, name)` like dedup; `hosts` is the union.
- `args` is built by reusing **`catalog.arg_to_property`** (already maps runspec
  types → JSON-schema, `options`→`enum`, `range`→`minimum`/`maximum`, carries
  `default`/`description`) plus the arg `name`/`required`/`type`. `password`-typed
  args are **omitted** (§5.3).
- Ranking: deterministic lexical to start — token overlap / substring over
  `name` + `description` + `group`, name-hit weighted highest. `difflib`-style
  fuzzy is enough; the core lib's fuzzy suggester in `errors.py` is a reference, not
  a dependency. Cap results and append `"_truncated": true` + a "refine your query"
  hint when over the cap.
- Honors the trigger allowlist (§8): only allowed `(host, name)` rows, with `hosts`
  narrowed to allowed hosts.

#### 4.1a Runbooks as a second result kind — added in 0.202.0

Saved runbooks are the operator's standing procedures, so a query describing a
symptom should surface the runbook that handles it next to the runnables it would
call. They ride the **same** call under a separate top-level `runbooks` key:

```jsonc
{ "id": "web-502", "kind": "runbook", "title": "Triage 502s",
  "symptom": "web nodes returning 502s after a deploy",
  "tags": ["web","incident"],
  "args": [ { "name": "host", "required": true, "type": "string",
              "enum": ["web-01","web-02"] } ] }
```

Design points worth keeping:

- **One search, not a `search_runbooks` tool.** §13a's own lesson — an agent only
  searches for what it thinks to search for — applies here: a separate tool needs
  the model to decide to call it.
- **Never in `results`.** `run_runnable`'s description tells the model to pass any
  `id` from a result straight to it, and its `id` has no enum, so a runbook id there
  would go to the wrong tool. A runbook id is also operator-chosen and stored
  verbatim, so it *can* look like a dedup id — hence the explicit `kind` too, and
  the `run_runbook` hint both `_resolve_id_host` and `_run_describe_tool` return.
- **Ranking weights differ from runnables** — id/title 10, **symptom 5**, tags 3.
  For a runnable the name is what the operator knows and the description is prose
  nobody searches for; for a runbook the query usually *is* a restatement of the
  symptom.
- **Suppressed when `host`/`group` is set.** A runbook belongs to no host and no
  package; returning them anyway would make the filter a lie.
- **Capped at 5** with `runbooks_truncated`, separate from the runnable cap so the
  two kinds can't crowd each other out.
- **Fails soft.** This runs inside fleet-wide discovery, and a hand-edited or
  Config-Sync'd `runspec_runbooks.toml` isn't guaranteed to parse
  (`validate_runbook_arg_table` only guards the agent's *save* path). A bad store,
  or one bad `[runbook.args]` table, must never take down `search_runnables`.

**Non-goal, deliberately:** `run_runnable` must not learn to accept a runbook id,
and a runbook row must not gain a `next`/`then` field. Keeping the kinds separate
is what holds the §1 "no workflow engine" line — a runbook stays prose the agent
reads, not a step graph the console executes.

### 4.2 `run_runnable`

```jsonc
{
  "type": "object",
  "properties": {
    "id":            { "type": "string", "description": "The runnable id from search_runnables." },
    "host":          { "type": "string", "description": "Which host to run on (from the row's hosts)." },
    "args":          { "type": "object", "description": "Arguments by name, per the row's args.",
                       "additionalProperties": true },
    "command":       { "type": "string", "description": "Subcommand, if the runnable has one." },
    "credential_id": { "type": "string", "description": "Optional saved credential id to apply." }
  },
  "required": ["id", "host"]
}
```

`args` is a free-form object (validated at dispatch, §5/§6) — keeping it nested
avoids collisions with `id`/`host`/`command`. Single-host runnables may omit `host`
(resolver defaults it, mirroring dedup).

### 4.3 `describe_runnable` (recommended)

```jsonc
{ "type": "object",
  "properties": { "id": { "type": "string" }, "command": { "type": "string" } },
  "required": ["id"] }
```

Returns one runnable's interface in the shape of the `run_runnable` call it is
for (console 0.335.0): `hosts`, the global `args` as typed rows (name, required,
type, enum, default — the same rows a search result uses), and, for a subcommand
runnable, a `commands` map of **command path → its description + its OWN args**
(globals listed once, not per command). Passing `command` (a space-joined path)
returns that one command's complete arg list, globals included, with exact
`required` flags. Past a 300-row budget the no-command form lists only arg names
per command (all-or-nothing) and says to ask for one command.

Before 0.335.0 this returned the dedup tool schema (`_build_input_schema`), which
flattened every command's args into one property list: a name shared by two
commands kept only the first command's description and choices, per-command
`required` was a text note, and the shape didn't match `run_runnable`. On a
many-verb runnable (whose search row defers to describe) the agent misread it,
guessed args, and drifted to a different command whose args it did know.

---

## 5. Argument-accuracy levers (the runspec.toml payoff)

### 5.1 Structured, typed search rows (defer the schema, don't discard it)

Because `_runnables_cache` already holds fully-typed args (verified fields:
`name, type, required, description, options, default, range, position, env,
multiple, short, autonomy`; types `str/path/choice/int/float/bool/flag/password`),
`search_runnables` returns a **structured** `args` array (not prose). The closed
type set + explicit `options`/`range`/`default` give the model exact legal values to
construct a correct call in one shot — without a separate `describe` round-trip in
the common case. This is the core reason search mode doesn't trade away accuracy.

### 5.2 Pre-flight validation in `run_runnable` (catch errors before the SSH hop)

Today (dedup or per_host) a bad argument travels to the remote host and bounces back
as a core validation error. Because the console holds the structured spec locally,
`run_runnable` does a deterministic **pre-flight** against the resolved runnable's
cached args *before* dispatch and returns a precise correction **in the same turn**:

- required args present (respecting `command`-scoped required + `requireCommand`);
- `choice` value ∈ `options`;
- `int`/`float` parseable, and within `range` `[min,max]` when present;
- unknown arg names rejected with the valid set (reuse `_declared_arg_names(cached,
  command_path)`);
- `command` ∈ the runnable's `commands` when supplied / required.

On failure, return a structured, human-readable error (same shape the agent already
reads on a remote bounce) and **do not** dispatch. This makes first-call accuracy in
search mode potentially **better** than the status quo (no wasted remote round-trip).

**Validation source — decision to make during implementation (probe):** reuse core
`runspec` `validator.py` for authoritative parity vs. lightweight structural checks.
Constraint discovered: discovery's `rawSpec` is *the spec minus args/commands*
(`_RAW_SPEC_EXCLUDE`), so the cache does **not** retain a re-validatable full spec —
core's validator expects a parsed spec/RunSpec, and remote packages' custom
`register_type` args won't be registered locally, so full local validation can
diverge. **Recommendation: ship lightweight structural checks** (the bullets above),
which are deterministic, dependency-free, and cover the overwhelming majority; treat
"reconstruct a spec and call core's validator" as a stretch goal, gated on whether
custom types show up in practice. Whatever isn't caught locally still bounces from
the remote exactly as today — no correctness regression, only fewer round-trips.

### 5.3 Secrets: omit `password`-typed args from suggestions (accuracy *and* safety)

`password`-typed args are flagged in the spec and detected by
**`_secret_arg_names(cached, command_path)`**. `search_runnables`/`describe_runnable`
**omit** them from the suggested `args` (same rule as `catalog.runnable_to_catalog_entry`
and the core agent-schema emit), so the model never tries to pass a secret inline.
At dispatch, `_run_tool_sync` already splits `password` args into the `secrets=` env
channel and fills bound values from credentials — unchanged. One field buys both
correct calls and the no-secrets-in-argv guarantee.

---

## 6. Routing & dispatch integration (maximal reuse)

The `id` is the dedup tool name, so `_resolve_tool_target` already maps
`(id, host) → cached_runnable` (including the `host__name` fallback). The new work is
to **unwrap** the `run_runnable` meta-call into that existing path:

- **`_resolve_tool_target(tool_name, tool_input)`**: add a branch — if
  `tool_name == "run_runnable"`, read `id = tool_input["id"]`, `host =
  tool_input.get("host")`, and resolve as today using `id`+`host`. Return the inner
  `(host, name, cached_r, host_key)`. (For meta-calls, `host_key` is informational;
  the host lives at the top level of the meta-input, not inside `args`.)
- **`_run_tool_sync`**: when `tool_name == "run_runnable"`, build the inner tool
  input from the meta-call — `inner = dict(tool_input.get("args") or {})`, then carry
  `command`/`credential_id` from the meta level, and use `host` from the meta level.
  Run the **pre-flight** (§5.2); on success, feed the existing machinery unchanged:
  `_become_params`, `_secret_arg_names`, `_apply_credentials`,
  `_positionals_for`, `run_remote`/`run_local`. No change below the resolution step.
- **Autonomy gate** — the important subtlety: `_effective_tool_autonomy` /
  `_confirm_decision` must reflect the **inner** runnable, not the `run_runnable`
  wrapper. Add unwrap so the level comes from the resolved runnable's `autonomy` and
  per-arg escalation reads `tool_input["args"]`. There is **no new security hole**:
  autonomy is per-runnable and enforced after resolution regardless of how the tool
  was surfaced — an `autonomous` runnable discovered via search is still
  `autonomous`; a `manual` one is still hard-blocked; `confirm`/`supervised` still
  prompt.
- **Confirm prompt UI** — `_register_confirm`/the dispatched `runspec:tool_confirm`
  detail must show the **resolved** runnable name + the real `args` (e.g. "run
  drain-node on web02 {node: n1}"), not the `run_runnable` wrapper. Build the
  confirm detail from the unwrapped target so the operator approves what actually
  runs. The `display` string (currently `tool_name.split("__",1)[-1]`) becomes the
  resolved runnable name.

Cleanest shape: a small helper `_unwrap_agent_call(tool_name, tool_input) ->
(effective_tool_name, effective_tool_input)` used at the top of the gate and
`_run_tool_sync`, returning `(id, {**args, host, command, credential_id})` for
`run_runnable` and the inputs untouched otherwise. Then the **entire** existing
gate+dispatch path runs unchanged (it already handles the dedup `id` + `host` arg).
This keeps the new logic to one normalization point.

---

## 7. Builder changes (`_runnables_to_tools`)

- `_tool_mode()` accepts `"search"`.
- New branch returns `[search_runnables, run_runnable, describe_runnable]` +
  `_custom_agent_tools()`. The `run_runnable`/`describe`/`search` schemas are static
  (no per-runnable expansion).
- Add the three meta-tool names to a `_META_TOOL_NAMES` frozenset (parallel to
  `_CUSTOM_TOOL_NAMES`) so the dispatch/gate can recognize them.
- The search **execution** of `search_runnables`/`describe_runnable` themselves
  (read-only, no side effects) is handled in `_run_tool_sync` (or a sibling like
  `_run_custom_tool`), returning JSON. They are always autonomy `"autonomous"`
  (read-only) — but `run_runnable` is **never** auto-allowed on its own; its decision
  comes from the unwrapped inner runnable (§6).

---

## 8. Allowlist / trigger behavior

Agent-mode triggers pass an `allow` set to `_runnables_to_tools`. In search mode:
- `search_runnables`/`describe_runnable` filter rows through the same allow logic
  used today (bare `name`, `host__name` raw/sanitised, or the dedup `id`), reusing
  `_allowed_hosts` to narrow each row's `hosts`.
- `run_runnable` re-checks the allow set at dispatch (defense in depth): a call whose
  resolved `(host, name)` isn't allowed returns a refusal, never executes.
- The `allow` set must be threaded into the meta-tool handlers (today it only scopes
  the static tool list). Plumb it via the existing agent-turn call site that already
  computes `allowed_runnables`.

---

## 9. Prompt caching

A nice side effect: in search mode the tool block is a **fixed** ~5 entries, so it
prompt-caches perfectly turn-over-turn — *better* than dedup, where adding/removing a
host changes a `host` enum and busts the cached tools prefix. Confirm against
`tests/test_prompt_caching.py` expectations (the cached prefix should now be stable
across discovery refreshes in search mode).

---

## 10. Config & rollout

- `[llm] tool_mode = "search"` — third value; `"dedup"` stays the default.
  `_tool_mode()` already validates membership and falls back to `"dedup"` on an
  unknown value (so `"search"` just joins the allowed set).
- Document in `README.md` (the `tool_mode` subsection added in 0.85.0) and a
  `CHANGELOG.md` entry on the version bump.
- Fully reversible: flip back to `dedup`/`per_host`, no code path removed.
- Optional later: an automatic heuristic ("use search when distinct runnables > T")
  — **not** in the first cut; explicit opt-in only.

---

## 11. Files to change

- `runspec_console/bridge.py`
  - `_tool_mode` — allow `"search"`.
  - `_runnables_to_tools` — `search` branch returning the meta-tools.
  - New `_META_TOOL_NAMES`, meta-tool schema builders, and `search`/`describe`
    executors.
  - `_search_runnables(query, host, group, allow)` — rank `_runnables_cache`, build
    structured rows (reuse `catalog.arg_to_property`).
  - `_preflight_validate(cached_r, command_path, args)` — §5.2 structural checks.
  - `_unwrap_agent_call` + branches in `_resolve_tool_target`, `_run_tool_sync`,
    `_effective_tool_autonomy`/`_confirm_decision`, and the confirm-detail builder.
- `runspec_console/catalog.py` — reuse `arg_to_property`/`_TYPE_MAP`; possibly factor
  a shared `arg_summary(arg)` if search wants a leaner row than the full property.
- `README.md`, `CHANGELOG.md`, `pyproject.toml` (version bump).
- `tests/test_tool_search.py` (new).
- Possibly the agent system-prompt builder (`_base_system` / adapter default) to add
  the search-protocol hint in `search` mode only.

Reused as-is (no change): `_resolve_tool_target` core, `_build_input_schema`,
`_apply_credentials`, `_union_scoped_credentials`, `_scoped_credentials`,
`_secret_arg_names`, `_positionals_for`, `_declared_arg_names`, `_become_params`,
`run_remote`/`run_local`, the gate/confirm round-trip.

---

## 12. Test plan (`tests/test_tool_search.py`)

Mirror the `_Bridge` harness pattern from `tests/test_tool_dedup.py` (bind the real
methods onto a light stub; `get_config` returns `{"llm": {"tool_mode": "search"}}`).

1. **Layout:** search mode emits exactly `{search_runnables, run_runnable,
   describe_runnable}` + custom tools, regardless of fleet size (assert constant
   count for 1 vs 100 runnables).
2. **Search ranking & rows:** query matches by name/description/group; rows carry
   structured typed `args` (choice→enum/options, int→range), `hosts` union, `id` ==
   dedup name; `password` args omitted; results capped + `_truncated` flag; `host`/
   `group` filters narrow results.
3. **describe_runnable:** returns the same schema `_build_input_schema` would (host
   enum + command enum + credential_id), by id.
4. **Routing/unwrap:** `run_runnable({id, host, args})` resolves to the right
   `(host, name, cached_r)`; single-host id defaults host; bad/unknown id or host →
   readable error.
5. **Pre-flight:** missing required → error (no dispatch); bad choice value → error
   listing options; int out of `range` → error; unknown arg → error with valid set;
   `requireCommand` without `command` → error; valid call → dispatches and `host`/
   `command`/`credential_id` are stripped from argv (assert via stubbed
   `run_local`/`run_remote`).
6. **Autonomy unwrap:** inner runnable `autonomous` runs without prompt; `manual`
   hard-blocked; `confirm` prompts — driven through `run_runnable`. Per-arg
   escalation reads `args`.
7. **Confirm detail:** the dispatched `runspec:tool_confirm` shows the resolved
   runnable name + real args, not `run_runnable`.
8. **Secrets:** a `password` arg supplied inside `args` is split into `secrets=` and
   never reaches argv (reuse the `test_agent_secret_split` assertions through the
   meta path).
9. **Allowlist:** search rows and `run_runnable` both honor `allow` (bare name → all
   hosts; `host__name` → narrowed; disallowed → refused at dispatch).
10. **Headline:** print the per-turn tool-block size for a ~80-distinct-runnable
    fleet across `per_host` vs `dedup` vs `search` (expect ~360+ → ~80 → ~5 tools).

Plus: full suite green, `ruff check`/`ruff format`. Update any existing stub that
binds the touched gate/dispatch methods (as the dedup PR did) to also bind the new
unwrap helper.

---

## 13. End-to-end verification

- Unit: `python -m pytest tests/test_tool_search.py -q -s` (prints the size headline);
  then `python -m pytest -q` (whole package).
- Manual smoke: set `[llm] tool_mode = "search"` in a dev `runspec_config.toml` with
  a multi-package fleet; in the console chat, confirm the agent (a) calls
  `search_runnables`, (b) constructs a correct `run_runnable` call from the row, (c)
  gets a same-turn pre-flight correction on a deliberately bad arg, (d) hits the
  confirm gate showing the real runnable+args, and (e) executes with credentials/
  secrets/positionals intact. Compare token usage vs `dedup` on the same fleet.

---

## 13a. Always-on catalog index — implemented in 0.88.0 (the *discovery* fix)

Production feedback surfaced a failure §5 doesn't address: a runnable whose
distinguishing detail lived only in its **description** (one JSM service-desk among
several similarly-named raise-runnables) went unfound — the agent answered without
ever searching the right term, because in pure search mode it **can't see what
exists** and so can't know to search for it. This is a *discovery* miss, upstream of
ranking; better ranking can't fix a query the agent never thought to make.

Fix (0.88.0): fold an **always-on catalog index** into each search-mode chat turn —
one line per runnable, `id — first sentence of description`, deduped by
`(group, name)`, sorted, **no schemas**. The agent always sees the full menu (the
property dedup mode had for free), then uses `search_runnables`/`describe_runnable`
for arguments + hosts. Restores discoverability at ~1/10th the full-schema cost.

- `_build_catalog_index()` builds it from `_runnables_cache` (reusing
  `_dedup_tool_name` for the id + a `_first_sentence` helper); bounded by
  `[llm] tool_index_limit` (default 300) with an overflow note; gated by
  `[llm] tool_index` (default true).
- `_fold_catalog_index(history, index)` prepends it to the first user message of a
  **per-call copy** of the history — provider-agnostic (no adapter-interface change,
  preserving the public `stream_with_tools` plugin contract) and leaving the cached
  tools+system prefix intact (§9). The stored `_chat_history` is never mutated.
- Wired into `_agentic_chat_turn` **and** (since 0.129.0) `_run_isolated_agent_turn`
  when `_tool_mode() == "search"` and the turn is unrestricted (no `allow` set) — so
  an unrestricted agent-mode **trigger or schedule** turn discovers runnables the
  same way chat does. dedup/per_host and allow-scoped triggers are untouched (an
  allow-scoped turn already falls back to dedup, which carries full schemas inline).
  Tests in `tests/test_catalog_index.py`.

This is the recommended primary remedy for discovery misses; §14 (dynamic schema
injection) remains the separate, still-deferred remedy for *construction* misses.

---

## 14. Phase 2 (only if first-call accuracy disappoints): dynamic working set

If structured rows + pre-flight prove insufficient in practice, layer in dynamic
schema injection: keep the full dedup tool definitions but include in the per-turn
tools array only those a prior `search_runnables` surfaced this conversation (a
per-chat "working set"), exactly like this harness's deferred-tools mechanism.
Because `_build_input_schema` already produces the exact schema, injection is cheap
and lossless. Trade-offs: stateful per-conversation set + prompt-cache churn each
time the working set changes (the very thing §9 avoids). Reach for it only if needed;
the §5 levers are designed to make it unnecessary.

---

## 15. Open questions

1. **Validator reuse (§5.2):** can we cheaply reconstruct enough spec to call core's
   `validator.py`, or do lightweight checks suffice? Probe: do any in-use remote
   packages register custom types whose validation we'd miss locally?
2. **`describe_runnable` necessity:** measure how often the model needs it once rows
   are structured — it may be droppable, or fold into a `verbose` flag on
   `search_runnables`.
3. **System-prompt phrasing:** the search protocol hint must be terse but reliable
   across providers; verify with the LangServe/OpenAI adapters, not just Anthropic.
4. **Result cap / ranking quality:** is top-10 lexical enough for a 100-runnable
   fleet, or do we need group-scoped browse (`search_runnables(group=…)` with empty
   query → list a package)? Likely yes — support empty/blank query as "browse".
