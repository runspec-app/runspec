# Design session — Forms view: global args + nested subcommands

_Last updated: 2026-06-04. Written as a handoff so we can pick this up in a
mock design session on the PC and pick a direction before implementing._

> **Decision (2026-06-05, implemented in console 0.16.0): Option C + sectioned
> leaf.** Discovery merges each ancestor's args down the command path (child
> overrides parent), tagging inherited ones (`inherited: true`). Intermediate
> nodes stay picker-only; the leaf renders inherited args in an **Inherited**
> section above the command's own, validated once. We re-derive the merge in
> discovery (open question #4) rather than reusing core's `_resolve_subcommand`,
> to avoid coupling discovery to a parse pass. No executor change was needed:
> the existing `binary <command path> <flags>` order already parses correctly
> because core resolves command tokens first, then matches all flags against the
> merged effective set. Picked C over A because the real target tree
> (`example-app`) is `require-command` at every non-leaf and only 1–2 levels
> deep, so "drill to reach a global" costs nothing.

## TL;DR

The Forms view (and the discovery layer feeding it) was designed **before**
runspec 0.20.0 introduced the global-arg inheritance model. As a result, for a
runnable that has **both** global args **and** subcommands, the form drops the
globals entirely:

- **First level shows no global args** — a node with subcommands renders the
  picker *instead of* its arg form, so the root's globals are never shown.
- **Leaf level shows no args** (or only its own) — discovery never merges the
  inherited globals down the command path, so the effective arg set is wrong.

Both are real. This doc nails down the cause and lays out UI options to choose
between. **Nothing is implemented yet — we pick a direction first.**

## What changed in core (the thing we predate)

runspec 0.20.0 (commit 243c5fc) + 0.20.1 (987835c) introduced **global arg
inheritance**:

- A runnable's **top-level `args` are global args** that *every* subcommand
  inherits (`spec/SPEC.md` ~line 1024).
- The **effective arg set** for a subcommand invocation = root args merged with
  the args declared at each level along the resolved command path; **child
  overrides parent** on a name clash.
- A **required global arg is required regardless of which subcommand** runs.
- CLI ordering: global flags appear **before** the command token
  (`deploy --region eu show`, not `deploy show --region eu`).

Core computes this in `parser.py::_resolve_subcommand()`, which returns the
merged leaf spec plus a `global_args: set[str]` naming which args came from the
root. The console reimplements none of this.

## Reference shape (test fixture from `test_parser.py`)

```toml
[sample-tool]
description = "Sample tool with globals + subcommands"
[sample-tool.args]
region = {type = "choice", options = ["us", "europe"], required = true, short = "-r"}
env    = {required = true, short = "-e"}
[sample-tool.commands.show]
description = "Show a symbol"
[sample-tool.commands.show.args]
symbol = {required = true}
[sample-tool.commands.multi]
description = "Nested group"
[sample-tool.commands.multi.commands.show]
description = "Nested show"
[sample-tool.commands.multi.commands.show.args]
symbol = {required = true}
```

Tree:

```
sample-tool            globals: region, env
├── show               args: symbol
└── multi              args: (none)
    └── show           args: symbol
```

Effective arg set for `sample-tool multi show` should be `region, env, symbol`.

## What the console does today

| Level | Effective set (correct) | What the form shows |
|---|---|---|
| `sample-tool` (root) | region, env | picker only — globals hidden |
| `multi` | region, env | picker only — globals hidden |
| `multi show` (leaf) | region, env, symbol | only `symbol` — globals missing |

### Cause 1 — picker XOR args (`FormsView.tsx:302`)

```tsx
const hasSubcommands = Object.keys(currentNode.commands ?? {}).length > 0
...
{hasSubcommands ? (
  <SubcommandPicker .../>      // root's own args never rendered
) : (
  <ArgForm node={currentNode} .../>
)}
```

Old assumption: a node is *either* a group (subcommands) *or* a leaf (args).
Core now allows both. So the root's globals fall into the picker branch and are
never drawn.

### Cause 2 — no merge in discovery (`discovery.py:158,173`)

```python
"args": _build_args(spec.get("args", {})),               # this node's args only
...
sub_name: _spec_to_runnables(sub_name, sub_spec, host, group)[0]   # recurse, no merge
```

Discovery mirrors the raw TOML tree verbatim — it never merges ancestor globals
into a child's arg list. So even once the UI is willing to draw leaf args, the
globals aren't in the data.

### Related — submit only sees current-node args

`handleSubmit` (`FormsView.tsx:233`) validates only `currentNode.args`, and the
collected `values` only contain whatever got rendered. Whatever option we pick,
globals must end up in `values` **and** be validated before `onSubmit`.

## The one change every option needs (discovery)

Make discovery compute the **effective arg set** the way core does: as it
recurses, carry the accumulated ancestor args and merge them into each node
(child overrides parent on name clash). Tag inherited args so the UI *can*
distinguish them, e.g.:

```python
entry["global"] = True   # set on args inherited from an ancestor (root/global)
```

This mirrors `_resolve_subcommand`. With it, each `Runnable` node carries its
true effective arg set and a flag marking which fields are inherited globals.
The UI options below differ only in *how* they present that.

> Open question: do we reuse core's `_resolve_subcommand` (e.g. by parsing
> through the library and reading back the merged spec / `global_args`) instead
> of re-deriving the merge in discovery? Cleaner, but couples discovery to a
> parse pass. Decide in session.

---

## UI options to evaluate

Mockups are rough — the point is the interaction model, not the pixels.

### Option A — Sticky globals, picker/args below

Globals render once in a pinned section at the top of the modal and stay
visible across every navigation level. The body below swaps between picker and
leaf args as you descend.

```
┌ sample-tool ▸ multi ───────────────────────────┐
│ GLOBAL                                          │
│  --region [ us ▾ ]   * required                 │
│  --env    [______]   * required                 │
│ ───────────────────────────────────────────────│
│ Nested group — choose a subcommand:             │
│  ┌ show ──────────────┐                         │
│  │ Nested show   1 arg│ →                       │
│  └────────────────────┘                         │
└─────────────────────────────────────────────────┘
```

- **Pro:** globals always reachable; fill-as-you-go; mirrors "globals are
  ambient" mental model. One source of truth for global values.
- **Con:** two interaction zones on one screen; need to decide when globals are
  validated (on submit at the leaf, presumably). Sticky section needs design
  care so it doesn't crowd the picker.

### Option B — Combined per-node screen (args + "continue")

Every node shows *its own* args form; if it also has subcommands, the picker
sits beneath under a divider. Selecting a subcommand carries values forward.

```
┌ sample-tool ──────────────────────────────────┐
│  --region [ us ▾ ]   * required                │
│  --env    [______]   * required                │
│ ──────────────  then pick a subcommand  ───────│
│  ┌ show ───────┐   ┌ multi ──────────┐         │
│  │ 1 arg       │ → │ 1 subcommand    │ →       │
│  └─────────────┘   └─────────────────┘         │
└────────────────────────────────────────────────┘
```

- **Pro:** no special "global" concept in the UI — each level just shows the
  args declared there + a picker. Maps cleanly onto the raw TOML structure.
- **Con:** values entered on parent screens must persist across navigation
  (they do via `values`, but back-nav + re-entry needs care). A level with no
  own args + subcommands shows just a picker (fine). Inherited globals shown
  only at the level they're declared, not re-shown at the leaf — operator may
  forget what they set 2 levels up.

### Option C — Flatten to leaf (single form at the end)

Intermediate levels are **picker-only**. At the leaf, render the full merged
effective set (globals + every ancestor + leaf) as one flat form. Matches
core's "effective arg set" exactly.

```
 step 1: sample-tool      step 2: multi          step 3: multi ▸ show
 ┌ choose ────┐           ┌ choose ────┐         ┌ run ─────────────────┐
 │ show       │ →         │ show       │ →       │ --region [ us ▾ ] *  │
 │ multi      │           └────────────┘         │ --env    [______] *  │
 └────────────┘                                  │ --symbol [______] *  │
                                                 │            [ Run ]   │
                                                 └──────────────────────┘
```

- **Pro:** simplest mental model and simplest code — one form, one submit, one
  validation pass; exactly the set core will parse. Globals can carry a small
  "global" badge so the operator sees scope.
- **Con:** can't set a global until you've drilled all the way in; deep trees
  mean several picker clicks before any field. If a global is invalid you find
  out only at the leaf.

### Option D — Two-step wizard (globals first, then drill)

Dedicated first screen for globals with a Next, then the picker, then leaf args.
Globals committed up front, carried down.

```
 step 1: globals              step 2..n: drill        final: leaf args
 ┌ sample-tool ─────┐         ┌ choose ────┐          ┌ run ──────────┐
 │ --region [ us ▾]*│  Next→  │ show       │  →  ...   │ --symbol [__]*│
 │ --env    [____] *│         │ multi      │          │      [ Run ]  │
 │          [Next] │         └────────────┘          └───────────────┘
└──────────────────┘
```

- **Pro:** globals validated up front before any drilling; clear phase
  separation; good when globals gate everything (region/env).
- **Con:** extra click for runnables with no subcommands (need to skip step 1
  when there are no globals, or no subcommands); back-and-forth between phases
  is a little heavier.

---

## Recommendation to react to (not a decision)

Lean **C for the data model** (discovery always merges → leaf carries the true
effective set, validated once) and **A for presentation** if we want globals
reachable from the top without drilling. C is the least code and the most
faithful to core; A is the nicest UX if drilling-to-set-a-global annoys us in
practice. B is the "no new concept" option but risks operators losing track of
values set on higher screens. Worth mocking A and C side by side on the PC.

## Open questions for the session

1. Present globals at the **top** (A/D) or only at the **leaf** (C)? Or
   per-level (B)?
2. When are globals **validated** — up front (D), or at submit (A/B/C)?
3. Do we show an inherited-arg **badge** ("global") so scope is visible, or keep
   it invisible?
4. Discovery: **re-derive** the merge ourselves, or **reuse** core's
   `_resolve_subcommand` / `global_args`?
5. Does `child overrides parent` on name clash need any **UI signal**, or just
   take the child silently like core does?
6. `require-command` levels: a node that *requires* a subcommand should not
   offer a Run button at that level — confirm the picker-only/no-footer behaviour
   already covers this.

## Pointers (where the code lives)

- UI: `packages/console-ui/src/views/FormsView.tsx`
  (`RunModal` ~189-326, `ArgForm` ~92-138, `SubcommandPicker` ~142-185,
  `resolveNode` ~13-19)
- Bridge types: `packages/console-ui/src/bridge/index.ts` (`Runnable`, `ArgDef`)
- Discovery: `packages/python/runspec-console/runspec_console/discovery.py`
  (`_spec_to_runnables` ~146-178, `_build_args` ~181-215)
- Core reference: `packages/python/runspec/parser.py::_resolve_subcommand`
  (~482-575); `spec/SPEC.md` ~1004-1109 (subcommands + global args)
