# Design: `runspec emit --stubs`

## Problem

Args parsed from `runspec.toml` are dynamic — their names and value types are
known only at runtime. `parse()` returns a `RunSpec` whose attribute access
(`RunSpec.__getattr__`) is typed `-> Any` (see `models.py`), which is the
honest static type and lets a parsed value flow into an annotated variable
without unwrapping:

```python
args = rs.parse("greeter")
workers: int = args.workers   # ok — Any flows into int
```

That unblocks typed *usage*, but the type checker still doesn't *know* the
shape of the runnable. It can't:

- reveal `args.workers` as `int` (it's `Any`),
- catch a typo like `args.wrokers`,
- autocomplete `args.name.upper()`,

even though every one of those facts is already declared in `runspec.toml`.
The schema is sitting right there; the checker just can't see it.

We already publish a JSON Schema for `runspec.toml` itself (SchemaStore entry),
which gives **authoring-side** validation and completion while you *write* the
TOML. Stubs are the complementary **consuming-side** feature: precise Python
types while you *write the runnable code that reads the parsed args*.

---

## What `--stubs` emits

`runspec emit --stubs` reads `runspec.toml` and generates a PEP 561 stub
(`.pyi`) describing a precisely-typed view of each runnable's parsed result.

Given:

```toml
[greeter]
[greeter.args]
workers = { type = "int", default = 4 }
name    = { type = "str" }
verbose = { type = "flag" }
quality = { options = ["low", "high"] }
```

it generates something like:

```python
# runspec_stubs.pyi  (generated — do not edit)
from typing import Literal, overload
from runspec import RunSpec

class _GreeterArgs(RunSpec):
    workers: int
    name: str
    verbose: bool
    quality: Literal["low", "high"]

@overload
def parse(script_name: Literal["greeter"], *args, **kwargs) -> _GreeterArgs: ...
@overload
def parse(script_name: str | None = ..., *args, **kwargs) -> RunSpec: ...
```

Then, with no change to the runnable:

```python
args = rs.parse("greeter")
reveal_type(args.workers)   # int            ← not Any
args.wrokers                # error: no such attribute
args.name.upper()           # str autocomplete
args.quality                # Literal["low", "high"]
```

Type mapping reuses the same table the JSON-Schema and MCP emitters already
use (`int`/`float`/`str`/`flag→bool`/`path→Path`/`choice→Literal[...]`,
`multiple=true → list[...]`). No new TOML fields are required — the data is
already in the parsed spec.

This is the concrete realization of the `emit --python-types` flag referenced
(aspirationally) in `emit-ansible-rundeck.md`; `--stubs` is the preferred
name. (Note: `runspec emit` does not exist yet — it is introduced by the
queued emit work. Until then this can ship as `runspec local --format stubs`,
mirroring the current `local --format mcp/openai/anthropic` surface.)

---

## Workflow — does the dev have to run it all the time?

**No.** The generator is not in the hot path. Stubs are a build artifact, the
same as `protoc`-generated code, an OpenAPI client, or Prisma's client. The
intended workflow:

1. **Generate once, commit the `.pyi`.** Run `runspec emit --stubs` and check
   the result into the repo next to the runnable. Day-to-day editing needs no
   regeneration — the editor and mypy just read the committed stub.

2. **Regenerate only when `runspec.toml` changes** — i.e. when you add,
   remove, or retype an arg. That's the only event that can make a stub stale.

3. **Keep it honest automatically**, so nobody has to remember step 2:
   - **Pre-commit hook** — regenerate (or check) on commit when `runspec.toml`
     is staged. This is the lightest-touch option for a single dev.
   - **CI check** — `runspec emit --stubs --check` exits non-zero if the
     committed stub doesn't match what the current `runspec.toml` would
     generate (same pattern as `ruff format --check`). This catches a stale
     stub in review without forcing local tooling on anyone.

   Recommended default: ship the `--check` mode and document the pre-commit
   hook; let teams opt into whichever fits.

So the mental model is: **you run it when the interface changes, not when you
edit code** — and CI is there to remind you if you forget.

### Relationship to the SchemaStore entry

The two cover opposite ends of the same schema and don't overlap:

| | Where it helps | What it validates |
|---|---|---|
| SchemaStore JSON Schema | editing `runspec.toml` | the TOML is well-formed (authoring) |
| `--stubs` `.pyi` | editing the runnable's `.py` | parsed args used correctly (consuming) |

A team can adopt either independently; together they give end-to-end typing
from the spec to the code that reads it.

---

## Open questions

- **Stub location / module.** A single `runspec_stubs.pyi` per package vs.
  one stub per runnable. Single file is simpler to wire into mypy; per-runnable
  is cleaner for large packages. Leaning single-file.
- **`parse()` overload vs. generated subclass.** The `@overload` on
  `script_name` literals (above) gives precise return types with zero call-site
  changes, but the overloads live in a stub that must shadow the real
  `runspec.parse`. Alternative: emit a typed `Args` class the author imports
  explicitly (`args = rs.parse("greeter"); args: GreeterArgs`). Overload is
  more ergonomic; explicit class is less magic. Decide before implementing.
- **Hyphenated arg names.** Stubs must use the normalized underscore form
  (`out-dir → out_dir`), matching `RunSpec._set_arg`.
- **Subcommands.** Each command path has its own merged arg set (globals +
  command args). Stubs likely key off the leaf command — needs a naming
  scheme, e.g. `_MultiShowArgs`.
- **Node parity.** TypeScript already gets structural types from `ParsedArgs`;
  evaluate whether a `.d.ts` emit adds value or if a generated interface per
  runnable is the better parallel. Python first; Node follows once stable.

---

## Build notes

- Reuse the existing arg→type mapping from the JSON-Schema / MCP emitter rather
  than introducing a second source of truth for type names.
- Ship `--check` alongside the generator from day one — a stub feature without
  a staleness guard rots immediately.
- Python-only initially. Slots after the `emit --rundeck` / `--ansible` work,
  since it shares the (not-yet-built) `runspec emit` command surface.
