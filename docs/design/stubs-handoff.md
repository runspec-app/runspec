# Handoff: `runspec stubs` (typed `parse()`)

> Session handoff note. This summarizes the design + implementation of the
> `runspec stubs` feature so a fresh session (or the maintainer returning later)
> has full context to answer questions, take feedback, or build the deferred
> next step. Pairs with the original design rationale in
> [`emit-stubs.md`](emit-stubs.md), the user guide
> [`docs/typed-args.md`](../typed-args.md), and the
> [CLI reference](../cli.md#runspec-stubs).

## What it is

`runspec stubs` generates `typings/runspec/__init__.pyi` so that
`args = parse("demo")` is precisely typed at the call site — `args.workers.value`
is `int`, choices become `Literal[...]`, and a typo (`args.wrokers`) is a hard
"no attribute" error with a suggestion, instead of the silent `Any` you get from
`RunSpec.__getattr__`. The type is keyed off the **string literal** passed to
`parse()` via `@overload`s.

## What shipped, and where

| Version | PR | Contents |
|---|---|---|
| `0.40.0` | #294 | The `runspec stubs` command + generator; `Arg` made generic (`Arg[T]`). |
| `0.41.0` | #295 | `runspec init` wiring (placeholder + mypy line + pre-commit) and `runspec local` auto-refresh. |
| (docs) | #296 | The user guide `docs/typed-args.md`. |

All three are merged to `main`. **`0.41.0` is the version that matches the full
guide** (`0.40.0` is just the command). The `v0.41.0` tag exists (auto-tag);
PyPI publish is **manual** (maintainer dispatches `release.yml` on `v0.41.0` —
see `releasing.md`). As of this note the maintainer is publishing it to reach an
internal Nexus on a work PC.

### File map

- `packages/python/runspec/runspec/stubgen.py` — all generation logic (pure text;
  no filesystem side effects). `render_stub`, `render_sentinel_stub`,
  `specs_from_discovered`, `collect_args`, `_render_dataclass`, `_py_type`.
- `packages/python/runspec/runspec/cli.py` — `cmd_stubs` (generate/`--check`/`--off`),
  `_render_installed_stub`, `_stub_status_note` (auto-refresh in `local`),
  `_init_stubs` / `_init_precommit` (init wiring), `_project_root` / `_stub_target`.
- `packages/python/runspec/runspec/runspec.toml` — the `[runspec.commands.stubs]`
  subcommand (`--check`, `--off`).
- `packages/python/runspec/runspec/models.py` — `Arg` is `Generic[T]`, `value: T`.
- Tests: `tests/test_stubgen.py` (units, drift guards, mypy integration),
  `tests/test_cli_init.py` (placeholder + pre-commit + mypy wiring).

## Key design decisions (don't undo these without re-checking)

1. **Self-contained stub.** The generated `.pyi` defines `Arg`/`Group`/`RunSpec`
   *inline* (introspected from the real classes via `dataclasses.fields`), rather
   than re-exporting from `runspec.models`. This is **load-bearing**: once
   `typings/runspec/__init__.pyi` shadows the package, a checker roots `runspec`
   at `typings/` and can no longer resolve `runspec.models`, which silently
   degraded `.value` to `Any`. Verified against the *installed* package, not a
   source tree. The PEP 561 `partial` marker did **not** fix this via `mypy_path`.

2. **Standalone view classes, no `__getattr__`.** Each runnable view
   (`_DemoArgs`) inherits `_RunSpecView` (RunSpec's stable metadata surface —
   `runspec_*`, `get`, `in`) but **not** the dynamic-arg `__getattr__ -> Any`.
   That's what makes typos/unknown args hard errors. `_VIEW_BASE` is
   hand-written and **drift-guarded** by `test_view_base_covers_all_runspec_properties`.

3. **`Arg[T]` generic.** Runtime no-op (`Generic` is erased); only enables the
   stub to pin `.value`. Bare `Arg` annotations in `models.py` became `Arg[Any]`
   to satisfy strict mypy.

4. **`parse()` overloads** key on `Literal["name"]`, with a broad
   `str | None -> RunSpec` fallback. The overlap lint is suppressed on each
   *literal* overload line (`# type: ignore[overload-overlap]`), not the fallback.

5. **Opt-in via the filesystem, not a toml flag.** A `[config] stubs = true`
   field was considered and **deliberately rejected**: `runspec.toml` is
   per-package but the stub is venv-wide, so a per-package flag can't express a
   project-level, non-propagating preference. State lives in `typings/`'s
   presence instead.

6. **`runspec init` placeholder.** Writes a sentinel stub whose `parse()` returns
   `_StubsNotGenerated` — metadata works, arg access errors with "run
   `runspec stubs`". The type's *name* is the message.

7. **Auto-refresh is conservative.** `runspec local` (text mode) **auto-upgrades
   the placeholder** to real types (safe — only ever replaces the placeholder),
   but only **nudges** (no write) for a stale *real* stub, so it never surprises
   CI or dirties the tree. The auto-write is crash-safe (read-only `typings/`
   falls back to a nudge). CI gates with `runspec stubs --check`.

8. **`--off` removes the stub** (restores default `Any`) rather than writing a
   self-shadowing permissive stub.

### Editor wiring
pyright/Pylance read `typings/` by default (zero config). mypy needs
`mypy_path = "typings"` (added to the generated `pyproject.toml` by
`init --write-project`).

## Testing approach
Pure renderers are unit-tested; correctness is proven by **mypy integration
tests** that generate a stub into a temp `typings/` and assert precise
`reveal_type`/`assert_type` results + a flagged typo, run against the *installed*
runspec (the real scenario). Drift guards assert the hand-written metadata
surface and the introspected `Arg`/`Group` fields stay in sync with the real
classes. Note: `tests/test_serve*.py` failures are **pre-existing/environmental**
(MCP subprocess) — they fail on clean `main` too.

## Open next step — "Option C": global strict default (deferred)

The one piece intentionally **not** built. C = ship a `.pyi` in the runspec
package so that even a project that never runs `init`/`stubs` gets the sentinel
(`_StubsNotGenerated`) instead of `Any` — i.e. truly opt-*out*.

- **Why deferred:** it's the only piece that changes how the **runspec package
  itself** is typed (its own `mypy runspec/` CI now checks against that shipped
  interface), so it needs care to not break the package's quality gate.
- **Why it's now optional:** with #295's auto-refresh, an `init`'d project never
  really sits in the `Any` state, so C's marginal benefit shrank to "projects
  that install runspec and type-check but never run init or stubs at all."
- **Recommendation on record:** optional, not necessary. Decide deliberately.

## Likely feedback / tweak points
- Auto-refresh aggressiveness (currently: upgrade placeholder, nudge-only for
  stale real stubs — could be made to auto-write, gated on TTY, if desired).
- Sentinel UX / naming (`_StubsNotGenerated`).
- Whether `typings/` should be committed (guide recommends yes) or gitignored.
- Subcommands are exposed as the **superset** across the command tree (no
  per-subcommand view) — revisit if precise per-command typing is wanted.
