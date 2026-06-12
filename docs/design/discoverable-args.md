# Design: arg-level `discoverable` — curating which args reach the visitor form

**Status:** Slices 1+2 **shipped** (`runspec` 0.35.0 / `runspec-node` 0.31.0,
PR #214) — the spec change (schema/SPEC/format.md) + per-arg `x-discoverable`
emit, at parity across both packs. **Remaining (next session):** slice 3 (console
`catalog.py` arg filter — the consumer that strips non-`self-service` args before
publishing) and the per-subcommand follow-up (§6). Extends the `discoverable`
keyword (`docs/design/helpdesk-rooms.md` §6, shipped as H0 in `runspec` 0.34.0 /
`runspec-node` 0.30.0) from the **runnable** level down to the **arg** level, so a
self-service runnable can expose only a curated subset of its arguments to the
helpdesk visitor web form.

**Why:** live-testing the first published catalog tool surfaced it — a runnable
with many args dumps *all* of them into the visitor form. Some args are operator
plumbing (an environment selector, an internal id, a `--force`) that a visitor
should never see or set. The runnable-level keyword is all-or-nothing: the whole
tool is in the catalog or out. We need per-arg curation, and — the user's call —
it should live **at the spec** (`runspec.toml`), travelling with the package, not
as a per-queue console setting.

The companion **layout** fix (the visitor form overflowing the 180px catalog
rail) ships separately as `runspec-room` 0.5.2; this note is the **curation**
half.

---

## 1. The decision, in one line

An arg appears in the **self-service visitor form** only if it is **explicitly
marked** `discoverable` for the `self-service` audience. Unmarked args are
operator-only — hidden from the visitor. **Hide-by-default / default-deny**, the
same posture the runnable-level keyword already takes toward the visitor catalog.

```toml
[request-vm]
description  = "Provision a VM for the caller"
discoverable = ["operator", "self-service"]   # the runnable is in the catalog

  [request-vm.args.size]
  options      = ["small", "medium", "large"]
  discoverable = ["self-service"]              # ✅ visitor picks this

  [request-vm.args.region]
  default      = "eu-west-1"
  discoverable = ["self-service"]              # ✅ visitor picks this

  [request-vm.args.justification]
  type         = "str"
  discoverable = ["self-service"]              # ✅ visitor fills this

  [request-vm.args.project_id]
  type         = "str"
  # absent → operator-only: hidden from the visitor form, the operator
  # supplies it (or a default does) when enacting.

  [request-vm.args.force]
  type         = "flag"
  # operator plumbing — never on the visitor form.
```

Visitor sees a 3-field form (`size`, `region`, `justification`). The operator/agent
sees and fills everything when enacting through the autonomy gate.

---

## 2. Semantics — the same keyword, the meaningful axis only

Reuse `discoverable` and its existing audience normaliser (`normalise_discoverable`
/ `validate_discoverable` in `discovery.py` / `discovery.ts`). But note the axes
differ between the two levels, because an arg is an *input the enactor must be
able to provide*:

| Level | `operator` audience | `self-service` audience |
|---|---|---|
| **Runnable** | gates `runspec serve`/`local` + the console (a tool can be hidden from operators) | gates the visitor catalog |
| **Arg** | **always implicitly true** — operators/agents always see every arg (they enact it; hiding an input from the enactor is nonsensical and unsafe) | gates the visitor **form field** |

So at the arg level **only the `self-service` audience is consulted**. An arg is
in the visitor form **iff** its normalised audiences contain `self-service`:

| Arg `discoverable` value | In visitor form? | Operator sees it? |
|---|---|---|
| absent | **no** (default-deny) | yes |
| `["self-service"]` | **yes** | yes |
| `["operator", "self-service"]` | **yes** | yes |
| `["operator"]` / `true` | no | yes |
| `false` | no | yes |

`false` and absent behave the same *for the visitor form* (excluded); the
distinction (`false` = "intentionally never visitor-facing", absent = "not yet
marked") is documentation-only at the arg level. We keep the full keyword shape
for consistency rather than inventing a narrower `self_service = true` arg field —
it's the same mental model authors already learned at the runnable level.

**Two-level gate.** A runnable must itself be `self-service` to appear in the
catalog *at all* (unchanged); then within it, only `self-service` args render.
Hide-by-default at both levels — no runnable and no arg leaks to a visitor without
an explicit opt-in.

**`password` stays hard-excluded.** Secret-typed args are already dropped from
visitor forms unconditionally (`catalog.py`); arg-level `discoverable` is an
*additional* filter, never an override — a `password` arg marked `self-service`
is still excluded (and we should warn on that combination).

---

## 3. What happens to the hidden args at enactment

The visitor form collects only the `self-service` args. The submission becomes a
`request`-kind message carrying `{tool, args}` with just those values — exactly as
today, only the arg set is curated. Enactment is unchanged and still operator/
agent-side behind the autonomy gate, so the hidden args are resolved there:

- an operator-only arg with a **`default`** → runspec fills it at validation;
- an operator-only arg **without** a default → the agent/operator supplies it when
  approving/running the request (the console shows the full arg set; the visitor's
  values are pre-filled, the rest are blank/required).

So a **required, operator-only, default-less** arg is legal: it just isn't on the
visitor form, and the operator must fill it before it runs (runspec validation
enforces this authoritatively at enactment, as it does today). The catalog
publisher **should warn** when it hides a required arg that has no default —
that's a likely authoring mistake (the request can't enact without operator
input), but not an error.

---

## 4. Surface — moves together (the docs-drift rule)

Pure metadata; **no new runtime dependency**. Per `CLAUDE.md` / `scripts/check_docs.py`,
the spec + schema + `docs/format.md` arg table + Python + Node + fixtures +
CHANGELOGs move in one change. Blast radius is **smaller than H0** because operator
discovery (`runspec serve`/`local`) is *unchanged* — arg-level `discoverable` is
consulted by exactly one consumer, the console catalog publisher.

| Area | Change |
|---|---|
| `spec/SPEC.md` | Document `discoverable` on an **arg** (beside the arg's other fields), the self-service-only semantics, the default-deny. |
| `schema/runspec.schema.json` | Add `discoverable` to the **arg** def (`$defs`), `oneOf` boolean / audience-array — reuse the runnable-level subschema. |
| `docs/format.md` | Add the `discoverable` row to the **arg-field table** (the `format-reference` check compares this table to the schema — required or `docs-check` fails). |
| **Python `runspec`** | `loader.py` `_normalise_arg` carries `discoverable`; `models.py` `Arg` gains `discoverable: list[str] | None`; the discovery/MCP emit attaches per-arg `x-discoverable` so the console can read it over SSH+MCP (see §5). No inference rule, no `serve`/`local` filtering change. |
| **Node `runspec-node`** | `loader.ts` + `models.ts` (`ArgSpec`) at parity; emit `x-discoverable` per arg. |
| `tests/integration/fixtures/` | Add an arg with `discoverable` to a shared fixture (exercised by both compliance suites). |
| **`runspec-console` `catalog.py`** | `arg_to_property` path filters args to `self-service` (the one behavioural change); warn on hidden-required-no-default and on `password + self-service`. |
| **`runspec-room`** | **No change** — the room serves whatever `inputSchema` the console publishes; the field is already gone before it reaches the visitor. |
| CHANGELOGs | `runspec` + `runspec-node` bumps (`## [x]` / `## [node-x]`). |

---

## 5. The one wrinkle: getting the arg's audience to the console

The console catalog publisher reads discovered runnables over SSH+MCP and filters
on `rawSpec.discoverable` (runnable level) today. For arg-level filtering it needs
**each arg's `discoverable`** in that discovery payload. So the runspec-side emit
(the schema the console pulls — `runspec local`/serve MCP tool schemas) must carry
per-arg `x-discoverable` (the normalised audiences), the same way runnable-level
already emits `x-discoverable`. That's the load-bearing runspec change; once the
arg's audiences reach the console, `catalog.py` does the filtering and the room/
visitor need no change.

Open sub-question: do we filter **server-side in the publisher** (the console
strips non-self-service args before publishing the `catalog` frame — the room and
visitor never even see them) or **emit all + filter at render**? Strongly prefer
**publisher-side** (the design's default-deny + least-exposure posture: a visitor's
browser should never receive an operator-only arg's existence). The room stays a
dumb pipe; the curated schema is the only thing on the wire.

---

## 6. Scope cuts (deliberate, from the live decision)

- **Per-subcommand `discoverable` is out of this cut.** `require-command` runnables
  are already excluded from the visitor catalog entirely (`catalog.py` skips them —
  the visitor form is flat, no subcommand selector). Subcommand-level audience
  scoping + a subcommand picker in the visitor form is a separate, additive
  follow-up; the arg-level keyword designed here applies identically to a
  subcommand's args when that lands.
- **No console/`room_catalog` arg allowlist.** The user chose the spec-level knob;
  a per-queue arg allowlist (narrowing on top, console-side) is a possible later
  addition but not built now — keep one source of truth.
- **Operator surfaces unchanged.** `runspec serve`/`local` and the console's
  operator tool list show every arg, exactly as today. This keyword only ever
  *subtracts from the visitor form*.

---

## 7. Slice plan

1. **runspec core (Python)** — schema + spec + `format.md` arg table + `Arg` model
   + loader + per-arg `x-discoverable` emit + fixture + CHANGELOG, in one commit
   (docs-drift green). Validation reuses `validate_discoverable`.
2. **runspec-node** — loader + `ArgSpec` + emit parity + the shared fixture passes
   both compliance suites + CHANGELOG.
3. **runspec-console `catalog.py`** — filter args to `self-service` in the publish
   path; the two warnings (hidden-required-no-default, password+self-service);
   tests. Bump.
4. *(later)* per-subcommand `discoverable` + a visitor subcommand picker.

Ship 1+2 together (the spec change); 3 follows (it depends on the emit from 1).
The `runspec-room` 0.5.2 layout fix is independent and already in flight.

---

## 8. Open questions

1. **`true` at the arg level** — treat as `["operator"]` (excluded from the visitor
   form, per §2), or reject it as meaningless at the arg level? Lean: accept and
   normalise to `["operator"]` (so the keyword behaves uniformly), document that
   bare `true` does **not** opt an arg into self-service (same as the runnable
   rule).
2. **Hidden-required-no-default** — warn (this note's recommendation) or hard-error
   in the publisher? Lean warn: it's a valid operator-completes-it pattern, and
   the autonomy-gate enactment already surfaces a validation failure if the
   operator forgets.
3. **Surfacing curation to the operator** — should the console's enact view *mark*
   which args came from the visitor vs which the operator must fill? Nice-to-have,
   not required for the first cut.
