# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers follow [Semantic Versioning](https://semver.org/).

---

## [0.51.2] — 2026-09-16

### Fixed
- **`runspec deny` explains a permission-denied write instead of tracing back.**
  The denylist lives in the venv root (`{sys.prefix}/runspec_denylist.toml`),
  so a venv created as a service account (`sudo -u svc python -m venv …`, e.g.
  runspec-linux's `create-venv --run-as`) refuses the operator's `deny add` /
  `all` / `mode` / `clear` with a raw `PermissionError` traceback. The CLI now
  catches it and prints a one-line refusal naming the path, the venv's owner,
  and the exact escalated re-run (`sudo -u <owner> runspec deny …`), exit 1.
  The happy path is unchanged. (`cli._deny_write` / `_deny_permission_denied`.)

## [0.51.1] — 2026-09-14

### Fixed
- **`runspec local --format json` now carries `[config] autonomy-default`.** The
  JSON discovery output read the raw `config.autonomy` key, but `load_raw`
  normalises the section to `autonomy_default` — so the configured default was
  **always dropped** (`config_autonomy = None`). Consumers that resolve a
  no-autonomy runnable/subcommand as `spec.autonomy or config_autonomy or
  "confirm"` (the runspec-console Forms tab) therefore showed **`confirm` on
  every subcommand** even when the package declared `[config] autonomy-default =
  "autonomous"`. `cli.py` now reads `autonomy_default`; a package's configured
  default reaches the console and subcommands resolve to it. (Note: a
  subcommand with no `autonomy` falls back to the `[config]` default, not to its
  parent runnable's declared `autonomy` — parent→child inheritance is not part
  of the spec.)

## [0.51.0] / [node-0.38.0] — 2026-09-14

### Changed

- **`ParseHarness.smoke()` is group-aware for *presence* constraints too.**
  smoke was aware of the *capping* groups (`exclusive` / `exactly-one`) but not
  the *presence* ones, so a runnable with an `at-least-one` group whose members
  weren't listed in `values` made smoke build a call omitting them all — which
  the validator rejects — turning a valid-invocation check into a spurious
  failure. smoke now satisfies every group constraint the validator enforces:
  for `at-least-one`, `inclusive`, and `if`+`requires` groups it **adds a
  fabricatable member** (a flag, or a `choice` filled from its options) so the
  built call passes. A group whose only members are non-fabricatable
  (plain `str`/`int`/etc. args with no supplied value) has nothing to add, so
  smoke **skips** that leaf's valid-invocation check rather than building a call
  that fails — the same graceful skip already used for an `exactly-one` group
  with no supplied member. Supply one member in `values` to cover such a leaf.
  Both packs (`testing.py` / `testing.ts` `_build_valid_argv` / `buildValidArgv`,
  with new `_arg_fragment` / `_presence_fill` / `argFragment` / `presenceFill`
  helpers). Docs: `docs/testing.md`.

## [0.50.1] — 2026-08-07

### Fixed

- **Argument groups now ignore defaults when deciding what was "provided".**
  Group constraints (`exclusive` / `inclusive` / `exactly-one` / `at-least-one`
  / conditional) are validated *after* defaults are applied, and a `flag` left
  unset carries its `false` default (not `None`), so it read as "provided". That
  made an `exclusive` group containing a flag impossible to satisfy — e.g. a
  `--run-as <user>` / `--sudo` pair rejected `--run-as deploy` on its own with
  "`--run-as` and `--sudo` cannot be used together" even though `--sudo` was
  never passed. `parse()` now feeds `validate_groups` a source-filtered view so
  only args the user actually supplied (cli/env) count as provided; defaulted
  values no longer trip any group constraint.

## [node-0.37.0] — 2026-07-19

### Added

- **Per-argument autonomy in emitted schemas** — parity with Python 0.50.0. An
  argument that declares its own `autonomy` now carries it as `x-autonomy` on
  its property in every emitted tool schema (`runspec local --format
  mcp/openai/anthropic` and the `runspec serve` `tools/list`), placed on the
  property itself even when `multiple = true` wraps it in an array. Lets
  schema consumers reproduce per-call effective autonomy (most restrictive
  level wins when the arg is provided). (`cli.ts` `argToJsonSchema`.)

## [0.50.0] — 2026-07-19

### Added

- **Per-argument autonomy in emitted schemas.** An argument that declares its
  own `autonomy` now carries it as `x-autonomy` on its property in every
  emitted tool schema (`runspec local --format mcp/openai/anthropic` and the
  `runspec serve` `tools/list`). Previously only the runnable-level
  `x-autonomy` was emitted, so a schema consumer could not see per-arg
  escalation (most restrictive level wins when the arg is provided) and could
  under-gate an `autonomous` runnable with an escalating arg. Lets client-side
  gates reproduce `effective_autonomy()` per call — e.g. the new Claude Code
  plugin example. (`cli.py` `_arg_to_json_schema`; `docs/agents.md`,
  `docs/cli.md`.)
- **Claude Code plugin example** (`examples/claude-code-plugin/`): a working
  Claude Code plugin that exposes a remote venv's runnables as MCP tools over
  the `runspec jump` SSH transport (`ssh <host> runspec serve`), with declared
  autonomy carried over as permission decisions by a PreToolUse hook
  (`autonomous` → allow, `confirm`/`supervised` → ask, `manual` → deny;
  fail-closed to ask). Includes a stdlib-only manifest generator baking
  `autonomy.json` from the serve's `tools/list`. Design notes in
  `docs/design/emit-claude-plugin.md`.

## [0.49.0] — 2026-07-02

### Added

- **`ui = "time"` / `"month"` / `"color"` / `"slider"` form-control hints.** More
  explicit `ui` values (never inferred), alongside `date`/`datetime`:
  - `ui = "time"` on a `str` arg → a time picker; value written as an `HH:MM:SS`
    string.
  - `ui = "month"` on a `str` arg → a month picker; value a `YYYY-MM` string.
  - `ui = "color"` on a `str` arg → a colour picker; value a `#rrggbb` hex string.
  - `ui = "slider"` on an `int`/`float` arg **with a `range`** → a slider (falls
    back to a number field when no range is declared).
  Type and parsing are unchanged — a `str` stays a string, a numeric stays
  numeric. Used by the runspec-console Forms tab and the runspec-room web form.
  (`schema/runspec.schema.json`, `spec/SPEC.md`, `docs/format.md`.)

## [0.48.0] — 2026-06-29

### Added

- **`ui = "date"` / `ui = "datetime"` form-control hints.** The `ui` field gains
  two explicit values that ask a UI to render a date / date-time picker for an
  argument that stays `type = "str"`. The picked value is written as an ISO-8601
  string — `YYYY-MM-DD` for `date`, a full timestamp for `datetime` — so the wire
  contract is an ordinary string and nothing about parsing or validation changes.
  These are explicit opt-ins (never inferred). Used by the runspec-console Forms
  tab and the runspec-room web form to surface calendar controls for date
  arguments. (`schema/runspec.schema.json`, `spec/SPEC.md`, `docs/format.md`.)

## [0.47.0] / [node-0.36.0] — 2026-06-27

### Changed

- **`discoverable` is now a plain boolean — the self-service audience left the
  format.** The keyword bundled two unrelated jobs: hiding an internal-helper
  runnable from discovery, and marking a runnable (or arg) for a *self-service*
  visitor catalog. The latter is a front-end concern that means nothing to the
  runnable as a CLI, so it was removed from `runspec.toml` entirely. `discoverable`
  reverts to `true` (listed by `runspec serve`/`local`, the default) or `false`
  (hidden — an internal helper, still runnable when invoked directly); the
  `["operator", "self-service"]` audience-list form and the entire **arg-level**
  `discoverable` field are gone. `runspec serve` now skips a runnable only when
  `discoverable = false`; `runspec local` annotates a hidden one as `[hidden]` and
  emits `x-discoverable: false` for it (omitted otherwise). Designating which
  runnables a self-service visitor may request is now a runspec-console concern
  (`runspec_self_service.toml`), not a `runspec.toml` one. Parity across Python and
  Node. (`discovery.py`/`discovery.ts`, `serve.py`/`serve.ts`, `cli.py`/`cli.ts`,
  `models.py`/`models.ts`, `loader.*`, `parser.py`; `schema/runspec.schema.json`,
  `spec/SPEC.md`, `docs/format.md`; `docs/design/discoverable-args.md` superseded.)

## [0.46.0] / [node-0.35.0] — 2026-06-23

### Changed

- **`ParseHarness.smoke()` is choice-aware.** `smoke()`'s `values` is one flat
  map keyed by arg name, but the same arg name can declare **different
  `options`** on different subcommands — so a single shared value can't be a
  valid choice everywhere. smoke now validates a `choice` arg's value against
  *each leaf's own* options: a shared value that isn't valid on a given
  subcommand falls back to that leaf's first option, and a `choice` arg absent
  from `values` is filled from each leaf's options (so a repeated `status`-style
  choice needs no per-subcommand workaround, and choice args need no value at
  all). The Python harness now also applies the same **inference** a real
  `parse()` does (Node already did), so `required_args()`, choice options, and
  the smoke builder match runtime behaviour. (`testing.py` / `testing.ts`
  `_build_valid_argv` / `buildValidArgv`.) Docs: `docs/testing.md`, the
  `add-runnable` skill.

## [0.45.0] / [node-0.34.0] — 2026-06-23

### Added

- **`--help` now lists argument-group constraints.** Groups (`exclusive`,
  `exactly-one`, `at-least-one`, `inclusive`, conditional) are enforced at parse
  time but were invisible in `--help`. The help renderer now prints a
  **Constraints:** section — e.g. `exactly one of: --json, --yaml`,
  `at most one of: --out, --err` — so a mutual-exclusivity / co-requirement
  relationship is visible without triggering a validation error. Implemented in
  both packs (`parser.py` `_group_constraint_text`; `parser.ts`
  `groupConstraintText`).

### Changed

- **`ParseHarness.smoke()` is group-aware.** When a leaf has an `exclusive` or
  `exactly-one` group, smoke now includes **at most one** member when building
  the valid invocation, so listing every member in `values` no longer builds a
  mutually-exclusive call the group (correctly) rejects. For an `exactly-one`
  group with no supplied member, smoke skips that leaf's valid-invocation check
  rather than failing it. Both packs (`testing.py` / `testing.ts`
  `_build_valid_argv` / `buildValidArgv`, with a new `_merged_groups` /
  `mergedGroups` helper). Docs: `docs/format.md` (group members must be
  optional), `docs/testing.md`, and the `add-runnable` skill.

## [0.44.0] — 2026-06-22

### Changed

- **`runspec serve` leaf tools now carry inherited global args.** When a
  runnable defines subcommands, `runspec serve` expands each leaf subcommand
  into its own MCP tool. Previously the leaf tool's JSON Schema advertised only
  that subcommand's own args, hiding the runnable's global (inherited) args —
  `model`, `permission-mode`, etc. — from agents, even though `parse()` accepts
  them at runtime. Each leaf tool's schema (and its exec arg-spec) now exposes
  the **effective** argument set: the inherited globals merged with the
  command's own args, with a same-named command arg overriding the inherited
  one (child-wins, matching `parse()`). Purely additive — no autonomy/confirm
  behaviour changes, and flat runnables (no subcommands) are unaffected.
  (`serve.py` `_expand_tools`; see `spec/SPEC.md` → Subcommands.)

## [0.43.1] / [node-0.33.3] — 2026-06-21

### Changed

- **Canonical JSON Schema now served from the docs domain.** The schema is
  published at `https://runspec.app/runspec.schema.json` (copied from
  `schema/runspec.schema.json` into the MkDocs site at build time via a build
  hook — single source of truth, no drift). The schema's own `$id` and the
  `#:schema` line emitted by `runspec init` (Python and Node) now point there
  instead of `raw.githubusercontent.com`, so editor TOML validation resolves
  against the stable, owner-controlled domain. The old `raw.githubusercontent.com`
  URL still works (the file remains in the repo), so existing `runspec.toml`
  files keep validating.
- **Project URLs point at `https://runspec.app`.** The `runspec` package's
  Homepage/Documentation metadata and the README docs links follow the docs
  site's move to the owned domain (away from `github.io`).


## [0.43.0] — 2026-06-21

### Added

- **Denylist (kill switch).** A venv-scoped way to disable runnables — one, a
  set, or all — without uninstalling packages or editing each `runspec.toml`.
  `parse()` reads `{sys.prefix}/runspec_denylist.toml` (override with
  `RUNSPEC_DENYLIST_FILE`) on every run, so the effect is uniform across direct
  CLI runs, installed entry points, and agent runs via `runspec serve`. Rules
  match by runnable name, **effective** user, and hostname as shell-style globs
  that combine (an omitted dimension means "all"); multiple rules OR together.
  `disabled = true` is a fast path that denies everything. `mode` (`enforce`
  default / `warn` / `off`) lives in the file and is authoritative — there is no
  per-runnable opt-out (unlike `enforce_run_as`), so a runnable can't disarm an
  operator's kill switch. `--help` and introspection (`runspec local` /
  `runspec stubs`) are never blocked, and the reserved `runspec` name is exempt
  so the control plane always works. New module `runspec/denylist.py`; error
  `RunnableDenied`. See `spec/SPEC.md` → "Denylist File" and `docs/denylist.md`.
- **`runspec deny` CLI** — `add` / `all` / `list` / `clear` / `mode` edit the
  venv denylist; `list --json` emits structured status (`{path, present, mode,
  disabled, reason, rules}`). It is a core subcommand (not a runnable), so it can
  never disable itself: `runspec deny clear` is the guaranteed re-enable path on
  any SSH-reachable host.

### runspec-console (0.106.0)

- **Denylist fleet kill switch — Settings tab + agent tool.** A **Settings →
  Denylist** tab (host picker, *Disable all* / *Re-enable all* danger buttons, an
  add-rule form with runnable/user/host globs, and an enforce/warn/off toggle)
  and a confirm-gated built-in agent tool, both running `runspec deny`
  (`list`/`add`/`all`/`clear`/`mode`) across selected fleet hosts over their
  existing SSH connections, once per venv. `targets` selects hosts (omit ⇒ all).
  Because it shells out to the core `runspec` binary (exempt from its own
  denylist), `clear` always re-enables — the console can never be locked out.
  Remote hosts need `runspec >= 0.43.0`. The tab shows a **live status table**
  per host/venv (via `denylist_status` parsing `runspec deny list --json`,
  refreshed after each action), and the agent tool's read-only `action='list'`
  is **autonomous** so the agent can check what's disabled without a prompt
  (mutations stay confirm-gated).

---

## [0.42.0] — 2026-06-19

### Added

- **`RUNSPEC_SKILL` → `run_summary.extra.skill`.** A new provenance axis recorded
  in the audit record, mirroring `RUNSPEC_TRIGGER` → `extra.trigger`. When set, it
  names the saved skill (runbook) that drove the run, so a host can attribute its
  runs to a skill (runspec-console reads it for the History/Analytics "Skill"
  column). Defaults to `""` (a normal run); orthogonal to `agent`/`trigger`.


## [0.41.0] — 2026-06-19

### Added

- **`runspec init` now wires up `parse()` typing.** A freshly scaffolded project
  gets a **placeholder** `typings/runspec/__init__.pyi` so the editor immediately
  shows a clear "run `runspec stubs`" signal (`parse("x")` is typed
  `_StubsNotGenerated` — metadata works, but touching an arg is a hard error, not
  a silent `Any`). With `--write-project` it also adds `[tool.mypy] mypy_path =
  "typings"` to the generated `pyproject.toml` and a `.pre-commit-config.yaml`
  hook that regenerates the stub when a `runspec.toml` changes.
- **`runspec local` keeps the stub fresh.** In text mode it auto-upgrades the
  `runspec init` placeholder to real types once the package is installed, and
  nudges (without writing) when a real stub is out of date. No-op when no stub
  exists — entirely opt-in via the stub's presence. CI should still gate with
  `runspec stubs --check`.

---

## [0.40.0] — 2026-06-19

### Added

- **`runspec stubs` — precise per-runnable types for `parse()`.** A new command
  scans the installed venv (same discovery as `runspec local`) and writes a
  self-contained `typings/runspec/__init__.pyi` at the project root. Each
  runnable becomes a typed view so `args = parse("demo")` is precisely typed at
  the call site with **no annotation or cast** — `args.workers.value` is `int`,
  `args.format.value` is `Literal["json", "yaml"]`, and a typo like
  `args.wrokers` is a hard "no attribute" error (with a suggestion) instead of a
  silent `Any`. Subcommand args are exposed as the superset across the command
  tree; the runnable's metadata surface (`runspec_*`, `get`, `in`) is preserved.
  pyright reads `typings/` by default; for mypy add `mypy_path = typings`.
  - `runspec stubs --check` exits non-zero when the stub is missing or stale
    (for CI / pre-commit), and `runspec stubs --off` removes the stub to restore
    the default `Any`-typed access.
- **`Arg` is now generic (`Arg[T]`).** `Arg.value` is typed `T`, so a generated
  stub can pin it precisely (`Arg[int]`, `Arg[Literal[...]]`). `Generic` is
  erased at runtime, so there is **no behaviour change** — plain runtime use is
  `Arg[Any]` exactly as before.

---

## [0.39.0] — 2026-06-19

### Added

- **`run_summary` now records a `trigger` provenance field.** A new
  `RUNSPEC_TRIGGER` environment variable (e.g. `RUNSPEC_TRIGGER=schedule`) is
  recorded into the `run_summary` audit record as `extra.trigger` — the
  provenance of *what caused* the run (an event source id), `""` for an
  interactive/manual run. It is an axis orthogonal to `agent` (`RUNSPEC_AGENT`):
  a single run can be both scheduled and an LLM turn. Consumers like
  runspec-console use it to attribute and badge runs (e.g. a scheduled-run
  marker) in History/Analytics. No behaviour change when the env var is unset.

---

## [0.38.0] — 2026-06-19

### Changed (BREAKING)

- **`Arg` is no longer a transparent proxy — read `.value`.** Attribute access
  on a parsed spec (`args.workers`) returns an `Arg`; to use the value you now
  write `args.workers.value`. Previously `Arg` forwarded ~30 dunders so it
  behaved like its native value in expressions (`args.workers + 1`, `if
  args.dry_run:`, `args.format == "json"`, `str(args.path)`, iteration,
  indexing, `os.fspath`). That convenience leaked in silent, surprising ways:
  `args.opt is None` never matched an unset optional (it was an `Arg` wrapping
  `None`), `isinstance(args.x, str)` was `False`, `str()` of an unset optional
  was the literal `"None"`, and the value would not JSON-serialise — so `.value`
  was needed *sometimes but not always*, with no visible boundary. `.value` is
  now the single, explicit way to read a value.

  To fail loudly instead of silently, the two operations a forgotten `.value`
  would otherwise get *wrong* — truthiness (`if args.x:`) and equality
  (`args.x == y` / `!=`) — raise a `TypeError` that names the arg and tells you
  to add `.value`. All other value operations (arithmetic, `int()`/`float()`,
  iteration, indexing, `len()`, `os.fspath`, attribute delegation like
  `args.path.is_dir()`) are simply absent, so they raise `TypeError`/
  `AttributeError` at the use site. The `Arg` still carries its metadata
  (`.type`, `.source`, `.meta`, `.options`, …), and `RunSpec`'s `runspec_*`
  metadata properties are unchanged. An unset optional is now honestly `None`
  via `.value`.

  **Migration:** add `.value` at each value use site. Fast path — run the
  runnable (or its tests) and follow the error messages; each names the arg.
  The bundled integration packages (`runspec-linux`, `-logops`, `-jsm`,
  `-snow`, `-windows`) and the `runspec init` Python stubs were migrated in
  this release. (Node parity is tracked separately and unchanged here.)

### Added

- **`RunSpec.get(name, default=None)` and `name in args`.** With subcommands the
  effective arg set is scoped to the active command path, so an arg declared
  only on a *different* subcommand isn't present and reading it as an attribute
  raises `AttributeError`. `"symbol" in args` tests membership; `args.get(name,
  default)` always returns an `Arg` (a synthetic `not_set` one wrapping
  `default` when the arg doesn't apply), so `args.get(name).value` is always
  safe — replacing the up-front "does this arg exist?" guards. `dir(args)` now
  lists the active args.

## [0.37.2] / [node-0.33.2] — 2026-06-15

### Added

- **`runspec.testing.no_run_as_check()`** (Node: `withoutRunAsCheck(fn)`) — a
  test-only escape hatch for the `enforce_run_as` identity gate, for tests that
  drive a runnable's `main()` directly. `ParseHarness` (0.37.1) skips the gate,
  but only for parses it makes itself; a `main()`-driven test calls
  `runspec.parse()` *inside* `main()` — the real parse — so the gate still fired
  there and a `run_as` runnable exited non-zero on a developer's machine. Wrap
  the `main()` call in `no_run_as_check()` (or drop it in an autouse
  `conftest.py` fixture) to skip the gate for that block. It lives in the
  test-only `runspec.testing` module and patches only the parser's view of the
  check, so no production code path can bypass the gate. Importing a runnable's
  module and calling its plain functions directly never touches `parse()`, so it
  needs none of this. See `docs/testing.md`.

## [0.37.1] / [node-0.33.1] — 2026-06-15

### Fixed

- **Testing a `run_as` runnable no longer trips `enforce_run_as`.** The
  `enforce_run_as` identity gate (added in 0.37.0 / node-0.33.0) refuses to run a
  `run_as` runnable as the wrong user — correct for real execution, but it also
  fired inside `ParseHarness` (`expect_ok` / `expect_exit` / `smoke`) and the
  in-process phase of `runspec test`, so a runnable declaring `run_as = "svc"`
  failed its own parse/wiring tests on a developer's machine. The harness now
  skips the gate, exactly as it already skips interactive secret prompts and as
  `load_spec` skips it for introspection — wiring tests must not depend on *who*
  runs the suite. Arg validation, type coercion, and `require-command` still run;
  only the identity comparison is skipped. The gate's own behaviour is unchanged
  on the real `parse()` path and is covered directly by `test_run_as_enforce.py`.

## [0.37.0] — 2026-06-14

### Added

- **`enforce_run_as`** — a runtime identity gate for the direct/import execution
  path. `run_as` is applied by an executor (`runspec serve` / SSH `sudo -u`)
  *before* the process starts, so under one the runnable is already the target
  user; but a runnable invoked directly (its entry point run by hand, from cron,
  or imported) never escalates and nothing checked who was running it. On
  `parse()`, the resolved `run_as` user is now compared **by uid** to the
  process's effective user, and `enforce_run_as` controls the reaction:
  `"error"` (default) refuses to run, `"warn"` logs to stderr and continues,
  `"off"` disables the check. Settable per-runnable or project-wide via
  `[config] enforce_run_as`; the runnable's own value wins. No-op when `run_as`
  resolves empty (no escalation intended) or on a platform with no POSIX uid
  model (e.g. Windows), and skipped entirely during introspection (`load_spec`,
  `runspec local`, `emit`).
  - New `runspec.become.verify_run_as_identity()` performs the comparison;
    `errors.RunAsMismatch` is raised on the `"error"` path.

### Note

- **Behaviour change:** the default is `"error"`, so a runnable that declares
  `run_as` and is run directly as anyone other than the target user now exits
  non-zero (previously the direct path ignored `run_as` entirely). Under
  `runspec serve` / `jump` nothing changes — escalation already makes the
  effective user match. Set `enforce_run_as = "warn"` (or `"off"`), per-runnable
  or in `[config]`, to relax it.

## [node-0.33.0] — 2026-06-14

### Added

- **`enforce_run_as`** — Node parity with Python 0.37.0. On `parse()`, the
  resolved `run_as` user is compared **by uid** to the process's effective user,
  and `enforce_run_as` controls the reaction on a mismatch: `"error"` (default)
  throws `RunAsMismatch`, `"warn"` writes to stderr and continues, `"off"`
  disables the check. Settable per-runnable or project-wide via `[config]
  enforce_run_as`; the runnable's own value wins. No-op when `run_as` resolves
  empty or on a platform with no POSIX uid model (`process.geteuid` undefined,
  e.g. Windows), and skipped during introspection (`loadSpec`).
  - New `become.ts` ports the `run_as` resolver (`resolveRunAs` — string /
    `$ENV` / per-host / first-match patterns) and `verifyRunAsIdentity` (the uid
    comparison, looking users up via `id`). This is the first `run_as` support
    in the Node pack — it had none previously.
  - `RunAsMismatch` is exported from the package entry point.

### Note

- **Behaviour change:** the default is `"error"`, so a runnable that declares
  `run_as` and is run directly as anyone other than the target user now throws
  (previously the Node pack ignored `run_as` entirely). Set `enforce_run_as =
  "warn"` (or `"off"`), per-runnable or in `[config]`, to relax it.

## [0.36.0] / [node-0.32.0] — 2026-06-12

### Removed

- **The auto-injected `--no-summary` flag** (and its `RUNSPEC_*_ARG_NO_SUMMARY`
  env var) is gone from every `[config.logging]` runnable. It dated from when the
  run-summary line was *always* printed to stderr and existed to mute that noise
  for a single run. The `0.33.0` / `node-0.29.0` split made the terminal echo
  opt-in (`summary_console`, default off), which left the flag mostly doing one
  thing: suppressing the structured **audit record** — the data the console
  **history**/**analytics** tabs and `runspec logs` read. Exposing a per-run
  "don't record me" switch as a global flag on every runnable undermined that
  feature and read confusingly (a double-negative defaulting to false), so it has
  been removed.
    - **Suppression is now package-level only.** Set `summary = false` in
      `[config.logging]` to drop the audit record; leave `summary_console` off
      (the default) to keep the terminal quiet. Both remain in place and
      unchanged.
    - **No behaviour change for a default config** — the audit record was, and
      still is, written on every run.
    - `configureLogging` / `configure_logging` no longer accept a
      `noSummary` / `no_summary` parameter. The `ParseHarness` `suppress_summary`
      / `suppressSummary` option is retained for back-compat but is now a no-op
      (the opt-in echo never leaks into test output).
    - Applied at parity in **Python** (`0.36.0`) and **Node** (`node-0.32.0`);
      `spec/SPEC.md`, `docs/logging.md`, and `docs/testing.md` updated.

## [node-0.31.0] — 2026-06-12

### Added

- **Arg-level `discoverable` — Node parity** with `runspec` 0.35.0. The
  `discoverable` keyword may be set on an individual argument to curate which of a
  runnable's args appear on a visitor self-service form. Default-deny,
  self-service-only: an arg is on the visitor form only when its `discoverable`
  lists `"self-service"`; operators always see every arg. Carried through the
  loader (`ArgSpec.discoverable`) and emitted as per-arg `x-discoverable` on the
  tool schema when set.

## [0.35.0] — 2026-06-12

### Added

- **Arg-level `discoverable` — curate which args reach a visitor form.** The
  `discoverable` keyword (added at the runnable level in 0.34.0) may now be set on
  an individual **argument**, to control which of a runnable's args appear on a
  visitor-facing self-service catalog form — without hiding anything from
  operators or the agent.
    - **Default-deny, self-service-only.** The arg level consults *only* the
      `self-service` audience: an arg is on the visitor form **only** when its
      `discoverable` explicitly lists `"self-service"`. Operators and the agent
      always see every argument (they enact the runnable — an input can't be
      hidden from the caller), so `true`, `false`, and `["operator"]` all keep an
      arg off the visitor form. A `password` arg is always excluded regardless.
    - **Emitted as `x-discoverable`** on the per-arg schema (when set), mirroring
      the runnable-level field, so the self-service catalog publisher can filter.
    - **Operator surfaces unchanged:** `runspec serve`/`local` show every arg —
      arg-level `discoverable` only ever subtracts from the visitor form.
    - Carried through the loader and surfaced on the parsed `Arg` (`Arg.discoverable`).
      Schema, `spec/SPEC.md`, and `docs/format.md` updated together. Node parity
      is tracked separately. Per-subcommand curation is a later addition.

## [0.34.0] / [node-0.30.0] — 2026-06-12

### Added

- **`discoverable` keyword — per-runnable, audience-scoped visibility.** A new
  optional runnable-level field that declares *which audiences* may discover a
  runnable, orthogonal to `serve` (which declares *the context*). Until now
  discovery was all-or-nothing per package: installing any dist that depends on
  `runspec` surfaced **every** runnable in it to `runspec serve`/`local`.
  `discoverable` lets one package hold both an agent-facing runnable and an
  internal helper and expose only the former.
    - **Forms:** `true` (default) ≡ `["operator"]` — operator-visible
      (`runspec serve`/`local`); `false` ≡ `[]` — hidden from all discovery
      surfaces; or an explicit audience list. Audiences: `operator` (the trusted
      agent surfaces) and `self-service` (a visitor-facing catalog, e.g. a help
      desk).
    - **Self-service is opt-in / default-deny.** A runnable reaches the
      `self-service` audience *only* by listing it explicitly — the bare `true`
      shorthand never implies it, so a runnable is never offered to untrusted
      users by accident (matching runspec's "safe by default" posture).
    - **Backward compatible:** absence means `["operator"]`, so every existing
      package keeps surfacing in `runspec serve`/`local` unchanged. Metadata
      only — no runtime behaviour is added to the runnable, no new dependency.
    - **Surfaces:** `runspec serve` skips runnables not discoverable to
      `operator` (validating the field first, like `serve`); `runspec local`
      still lists everything (the developer's diagnostic view), annotates any
      non-default audience, and emits the canonical audience list as
      `x-discoverable` on every tool schema (`--format mcp`/`openai`/`anthropic`)
      so downstream consumers can filter by audience. A parsed `RunSpec` exposes
      `runspec_discoverable` (Python).
    - Implemented at parity in **Python** (`runspec` 0.34.0) and **Node**
      (`runspec-node` 0.30.0); covered by the shared compliance fixtures
      (`tests/integration/fixtures/complex.toml`). Documented in `spec/SPEC.md`,
      `schema/runspec.schema.json`, and `docs/format.md`.

## [0.33.0] / [node-0.29.0] — 2026-06-11

### Changed

- **Run summary split into an always-on audit record and an opt-in console
  echo.** The `errors`→`err` abbreviation in `0.32.2` could not fix the Rundeck
  problem because Rundeck classifies log lines by **stream**, not text: it tags
  *everything* on stderr as ERROR regardless of content, and the summary line
  was always written to stderr. Rather than route the line cleverly (which would
  trade the Rundeck issue for polluted stdout pipes), the summary is now two
  independently controlled parts:
    - `summary` (`[config.logging]`, default **true**, unchanged) writes the
      structured `run_summary` record to the audit log — the data the console
      **history**/**analytics** tabs and `runspec logs` read.
    - `summary_console` (default **false**, new) echoes the one-line
      human-readable summary to stderr. Off by default, so a clean run never
      lands on a stream a processor tags ERROR (Rundeck/CI stay green with zero
      config) and stdout stays clean for shell pipes. Turn it on for interactive
      use.
  The per-invocation `--no-summary` / `RUNSPEC_*_ARG_NO_SUMMARY=1` kill switch
  still suppresses both parts. The JSON audit record shape is unchanged, so
  existing history/analytics keep working untouched. The previously fused
  behaviour (`summary = false` dropping the audit record too) is now decoupled —
  `summary_console` controls only the terminal echo. Same change in both the
  Python (`0.33.0`) and Node (`node-0.29.0`) packs.

## [0.32.2] / [node-0.28.1] — 2026-06-10

### Changed

- **Run-summary stderr line abbreviates the level words** — `2 warnings, 0 errors`
  is now `2 warn, 0 err`. The literal word `error`/`errors` tripped log
  processors that flag any case-insensitive `error` match (notably Rundeck's
  output highlighter), which coloured a clean run red and tagged the line at
  ERROR level for downstream consumers. The abbreviated form keeps `warn` in the
  warning bucket and never emits `error` on a successful run. Counts no longer
  pluralise (`1 warn`, `2 warn`). The JSON audit record is unchanged (it keys
  counts by the canonical `WARNING`/`ERROR` level names). Same change in both
  the Python (`0.32.2`) and Node (`node-0.28.1`) packs.

## [0.32.1] — 2026-06-10

### Fixed

- **`runspec init --lang typescript|javascript` now imports from `runspec-node`.**
  The Python CLI's Node stub templates imported from `'runspec'`, but the npm
  package is published as `runspec-node` (the Node CLI's own `init` already used
  the correct name). Scaffolded TS/JS stubs were therefore broken on install.

## [0.32.0] / [node-0.28.0] — 2026-06-08

### Added

- **`runspec test` — a CI smoke gate across every runnable.** Iterates the
  runnables discovered in the environment and, per runnable, runs two checks:
  an in-process **spec smoke** (the `runspec.testing.ParseHarness.smoke()` walk —
  `--help` works for every command, `require-command` is enforced, required args
  are validated) and a **subprocess that executes the installed entry point with
  `--help`** (proves the runnable's own code imports and is wired to the right
  spec — something the spec smoke can't catch). Exits 1 if any runnable fails
  either check; `--format json` prints machine-readable results for CI;
  `--runnable <name>` filters to one. Built for deployment packages that combine
  third-party runnables into one environment and want to catch broken ones
  before they ship.
  - **Python** discovers every installed `runspec`-dependent package in the venv
    (importlib.metadata). Each phase is isolated so one broken runnable can't
    abort the run.
  - **Node** (`node-0.28.0`) discovers every runnable in the venv-shaped folder's
    `runspec.toml` and resolves each entry point via the same `bin/<runnable>` /
    `node_modules/.bin` lookup `serve` uses.

- **`runspec-node/testing` — `ParseHarness` ported to Node** (`node-0.28.0`).
  The Node counterpart to Python's `runspec.testing`, exposed as a subpath
  export: `import { ParseHarness } from 'runspec-node/testing'`. `parse()` already
  takes `argv`/`configPath`, so testing a runnable needs no `process.argv`
  monkeypatching — `expectOk(argv)` / `expectExit(argv, { code, contains })`
  assert parse/exit behaviour (intercepting `process.exit` so `--help` doesn't
  kill the test runner, and mapping a thrown `RunSpecError` to a non-zero exit),
  and `smoke({ values })` auto-walks the whole subcommand tree. Spec
  introspection mirrors Python: `commandPaths()`, `requiredArgs(path)`,
  `requiresCommand(path)`, `argsFor(path)`.

## [0.31.0] — 2026-06-07

### Added

- **`runspec.testing` — test helpers for runnables.** A stdlib-only module
  (no pytest dependency) that removes the boilerplate from testing a runnable.
  `ParseHarness(script_name=..., config_path=...)` (or `toml=` for an inline
  spec) drives `runspec.parse()` with an argv list — no `sys.argv`
  monkeypatching or parser mocking:
  - `expect_ok(argv)` asserts a clean parse and returns the `RunSpec`;
    `expect_exit(argv, code=1, contains=...)` asserts a clean error exit and
    returns the captured output (no pytest `capsys` needed).
  - `smoke(values=...)` auto-walks the whole subcommand tree: `--help` works for
    every command, `require-command` is enforced, missing required args exit 1,
    and (given shared `values`) every leaf accepts a full valid invocation —
    meaningful coverage for a big migrated runnable in a few lines.
  - Spec introspection: `command_paths()`, `required_args(path)`,
    `requires_command(path)`, `args_for(path)`.
  - See `docs/testing.md` for the two-layer pattern (parse-wiring vs. mocking
    only the I/O boundary).

---

## [node-0.27.0] — 2026-06-05

### Added

- **Node parity for the `password` type and `hint` field** (matches `runspec`
  0.30.0):
  - `password` coerces/validates like `str` but is refused on the command line
    (`--secret value` / `--secret=value` / short-flag all error), resolved from
    the environment (`RUNSPEC_<RUNNABLE>_ARG_<NAME>` or an `env` alias), and
    prompted for without echo when run interactively (POSIX TTY; skipped for
    agent / non-interactive runs and `loadSpec`).
  - `password` args are omitted from emitted agent schemas (`local --format mcp`
    etc., and the MCP `serve` tool list).
  - `hint` is parsed and carried through (advisory UI placeholder text; never
    affects parsing).

---

## [0.30.0] — 2026-06-05

### Added

- **`password` argument type** for secrets (passwords, tokens, API keys). It
  coerces and validates like `str` (`pattern` / `min-length` / `max-length`
  still apply) but is handled so the value never leaks:
  - **Refused on the command line** — `--secret value` / `--secret=value` /
    short-flag forms all error, since a secret on the CLI lands in shell history
    and `ps`. The value resolves from the environment instead
    (`RUNSPEC_<RUNNABLE>_ARG_<NAME>`, an `env` alias, or `.runspec_env`).
  - **Interactive prompt** — a required `password` still unresolved in a
    terminal is prompted for without echo (skipped for agent / non-interactive
    runs).
  - **Omitted from emitted agent schemas** (`runspec local --format mcp` etc.)
    so an agent is never asked to supply a secret.
  - **Redacted from the run-summary audit record** (`invocation_args`).
- **`hint` argument field** — optional placeholder/hint text for UI form fields.
  Advisory only; runspec never reads it during parsing or validation.
- Both are reflected in `schema/runspec.schema.json` (the `type`/`ui` enums and
  a new `hint` property) and documented in `spec/SPEC.md` and `docs/format.md`.

### runspec-console (0.17.0)

- Forms render a `password` arg as a masked field and a `hint` as the input's
  placeholder. A password's value is injected into the run as the
  `RUNSPEC_<RUNNABLE>_ARG_<NAME>` environment variable (local subprocess env, or
  the SSH command's env prefix for remote hosts) — never as a command-line
  argument — and is kept out of the in-flight/args record. No persistent secret
  store is involved; the value is per-invocation and transient.

---

## [node-0.26.1] — 2026-06-04

### Fixed

- **Uncaught-exception handler now covers runspec's own parse pipeline** (parity
  with `runspec` 0.29.1). The `uncaughtException` / `unhandledRejection` handler
  installed by `[config.logging]` previously went up only at the *end* of
  `parse()` (via `configureLogging`), so a failure *inside* runspec — a failing
  type coercion or a validation slip during parsing — escaped uncaught and
  dumped a raw stack trace instead of the uniform one-liner / `--debug` trace.
  The handler is now installed at the start of `parse()`, as soon as
  `[config.logging]` is seen. The structured audit record is written only once
  handlers exist, so a failure during the parse pipeline itself still gets the
  console one-liner but no audit record (there is no log file yet).

---

## [node-0.26.0] — 2026-06-04

### Added

- **`runspec bin` now writes Windows `.cmd` shims** alongside the POSIX ones —
  a `bin/<runnable>.cmd` (and `bin/runspec.cmd`) per runnable that resolves the
  folder relative to itself (`%~dp0..`) and runs `node` with the args forwarded.
  A venv-shaped Node folder is now cross-platform: the same folder runs on
  Linux/macOS (POSIX shim) and Windows (`.cmd`), so a controller can invoke
  `bin/<runnable>` on either OS.

---

## [node-0.25.0] — 2026-06-04

Node logging parity with Python — the two pieces a node-only deployment folder
needs to be fully self-sufficient.

### Added

- **`runspec logs` — view / status / prune / compact.** The read + maintenance
  side of `store = "per-run"`, ported from Python (`runspec` 0.28.0/0.29.0) at
  parity:
  - `runspec logs <runnable>` merges a runnable's per-invocation files into one
    timestamp-sorted stream (`--since` / `--user` / `--run` / `--json`;
    `--follow` polls for live tail).
  - `runspec logs status [runnable]` — per-runnable file count, archives, disk
    size, newest record; `--json` emits the same shape Python does.
  - `runspec logs prune [runnable]` — `--older-than` / `--max-files` /
    `--max-total-size` (at least one required), `--dry-run`, `--json`.
  - `runspec logs compact [runnable] --older-than <dur> [--gzip]` — rolls old
    per-run files into a dated archive, `--dry-run`, `--json`.
  - Resolves the project's `logs/` the same way the write side does (nearest
    `package.json` skipping `node_modules`, then `~/logs`), and **never touches
    a single-mode `{runnable}.log`** — only per-run files and archives. This is
    what lets a Python-less node-only host manage its own logs on the box.

- **Stdout capture into the audit log (print-capture).** `console.log` output
  is now tee'd into the per-invocation file as `runspec.print` records (mirrors
  Python's `_StdoutTee`), so a runnable's actual output — not just the run
  summary — is preserved in the log and shown by `runspec logs <runnable>`.
  Output still reaches the real stdout unchanged (clean for pipes / `--json`),
  and captured lines are file-only: not echoed twice and not counted as log
  events in the run summary.

---

## [node-0.24.0] — 2026-06-04

### Added

- **`runspec bin` — generate a venv-shaped `bin/`.** Writes a `bin/runspec`
  shim plus one `bin/<runnable>` per runnable declared in `runspec.toml` (and a
  `logs/`), so a controller (runspec-console) can invoke `bin/<runnable>` and
  discover the folder via `bin/runspec` from any working directory, incl. over
  SSH — the same language-agnostic contract it already uses for Python venvs.
  Builds on caller-relative `findConfig` (node-0.23.0): the shims resolve their
  own location and need no `cd`. Each runnable's script is resolved from
  `package.json` `bin[name]`, falling back to `./<name>.{js,cjs,mjs}` next to
  `runspec.toml`. Re-run after adding a runnable or `npm install`. POSIX shims
  (Windows is a follow-up).

---

## [node-0.23.0] — 2026-06-04

### Changed

- **Caller-relative `runspec.toml` resolution.** `findConfig` now resolves the
  config relative to the **entry script** (`process.argv[1]`) before falling
  back to `process.cwd()` — mirroring Python's `parse()`, "what makes installed
  entry points work from any working directory." This lets an installed Node
  runnable (and `runspec local`) find its own config when launched from an
  unrelated working directory — e.g. a controller invoking `bin/greet` over SSH
  with `cwd` = the login home rather than the package. Resolution order is now
  `RUNSPEC_CONFIG` env → explicit start → entry-script dir → cwd, and a
  `runspec.toml` found *inside* `node_modules` is skipped (a dependency's spec,
  not the project's). `process.argv[1]` is preferred over
  `require.main.filename` because the latter realpath-resolves symlinks, which
  under `npm link` / workspaces / local installs jumps the entry out of the
  project.

---

## [node-0.22.0] — 2026-06-03

### Added

- **Per-invocation log files — `[config.logging] store = "per-run"`.** Node
  parity with `runspec` 0.27.0: opt-in layout writing one
  `{runnable}.{utc-ts}.{run_id}.log` per invocation (plain non-rotating handler)
  instead of a single rotating `{runnable}.log`. Safe for shared venvs with
  multiple users / parallel runs. Default `store = "single"` is unchanged.
- **`run_id` on every file record.** Each invocation now mints a UUID4 at
  `configureLogging()` time and injects it as `extra.run_id` on every
  file-handler record and the run-summary record (SPEC §run_id) — previously
  the Node pack omitted `run_id` entirely. This is what lets the per-run
  filename token match the records, and lets runspec-console attribute Node
  logs by run, not filename.

---

## [0.29.1] — 2026-06-04

### Fixed

- **Uncaught-exception handler now covers runspec's own parse pipeline.** The
  `sys.excepthook` installed by `[config.logging]` previously went up only at
  the *end* of `parse()`, so an exception raised *inside* runspec — a failing
  type coercion, a validation slip, or reaching for an argument that doesn't
  exist (`args.no_such_arg`) — escaped uncaught and dumped a raw traceback
  exposing internal frames, instead of the uniform one-liner / `--debug` trace.
  The hook is now installed at the start of `parse()`, as soon as
  `[config.logging]` is seen. The structured audit record is written only once
  logging is fully configured; a failure during the parse pipeline itself still
  gets the console treatment but no audit record (there is no log file yet),
  and never falls through to `logging.lastResort`.

---

## [0.29.0] — 2026-06-04

### Added

- **`runspec logs status` — per-runnable file + disk inventory.** A read-only
  "what's on disk" verb for `store = "per-run"`: lists each runnable's per-run
  file count, archive count, total size, and newest record. `--json` emits a
  single machine-readable object (`dirs`, `runnables[]`, `total_bytes`,
  `total_files`). This is the inventory the runspec-console **Logs tab** reads
  to populate per-venv usage before offering compact/prune — over SSH it's the
  same uniform, version-correct interface as `runspec local`.
- **`--json` for `runspec logs prune` / `compact`.** Both verbs now emit a
  structured result object instead of per-file text lines when `--json` is
  given (prune: `count`/`freed_bytes`/`deleted[]`; compact:
  `compacted`/`archives[]`), so a UI can render dry-run previews and applied
  results without parsing human text. Text output is unchanged by default.

---

## [0.28.0] — 2026-06-03

### Added

- **`runspec logs` — view / prune / compact per-invocation logs.** The read and
  maintenance side of `store = "per-run"`:
  - `runspec logs <runnable>` merges a runnable's per-invocation files into one
    timestamp-sorted stream (filters: `--since`, `--user`, `--run`, `--json`;
    `--follow` live-tails). The stream is TTY-aware and SIGPIPE-clean so it
    composes with `grep`/`head`/`less`/`jq` and process substitution.
  - `runspec logs prune [runnable]` deletes old per-invocation files by
    `--older-than` / `--max-files` / `--max-total-size` (combine freely;
    at least one is required). `--dry-run` previews.
  - `runspec logs compact [runnable] --older-than <dur> [--gzip]` rolls old
    per-invocation files into a dated `{runnable}.archive.{date}.log[.gz]`
    archive (raw JSON lines preserved, including `exc_structured`) and deletes
    the originals.
  - Both prune and compact default to all runnables in the venv and **never
    touch a single-mode `{runnable}.log`** — only per-run files and archives,
    matched by an exact suffix so a dotted runnable name is safe.

---

## [0.27.0] — 2026-06-03

### Added

- **Per-invocation log files — `[config.logging] store = "per-run"`.** Opt-in
  log layout that writes one `{runnable}.{utc-ts}.{run_id}.log` file per
  invocation instead of a single rotating `{runnable}.log`. Built for shared
  deployment venvs where multiple users — or one user running in parallel — log
  at once: because no file is ever shared there is no in-process rotation race
  and no cross-user ownership conflict. The `run_id` in the filename matches the
  `run_id` field in the records it contains. `rotate`/`keep` are inert in this
  mode; retention is an explicit operator action (the forthcoming `runspec logs`
  command). The default `store = "single"` is unchanged, so existing
  deployments are unaffected.

---

## [0.26.0] / [node-0.21.0] — 2026-06-03

### Added

- **Uniform uncaught-exception handling.** When `[config.logging]` is present,
  both packs now route every uncaught exception through one automatic path — no
  `try`/`except` wrappers or code template needed in runnables:
  - A structured record is always written to the audit file on a dedicated
    file-only `runspec.exception` logger — independent of `--debug` *and* of the
    `summary` toggle (previously the exception was only captured when summaries
    were on). The record adds an `exc_structured` object (`type`, `message`,
    `module`, `frames[]`) alongside the existing `exc` string, so UI tools can
    render the exception in a table. (Node frames carry no source `code` line —
    it isn't recoverable from a V8 stack string.)
  - On the console, the verbose traceback is shown only with `--debug`, rendered
    as a neat, aligned, dependency-free trace with internal runspec frames
    filtered out of the display.

### Changed

- **Uncaught exceptions no longer dump a full traceback to the console by
  default.** Without `--debug`, a single concise line is written to stderr
  (`ERROR: <Type>: <message>  (run with --debug for traceback)`); the full
  traceback is always in the audit file, and `--debug` restores it (neater) on
  the console. This reuses the existing `--debug` knob — uncaught exceptions now
  behave like every other log record.

## [0.25.0] / [node-0.20.0] — 2026-06-03

### Added

- **Effective value type for `multiple` args.** After per-item coercion, a
  `multiple = true` arg returns a list, but `type` stays the *item* type
  (`"str"`), which made the parsed result confusing to introspect. Both packs
  now expose the composed type:
  - Python: `Arg.python_type` → e.g. `"list[str]"` / `"int"` / `"Path"`
    (a computed property; `Arg.type` is unchanged).
  - Node: new exported `tsTypeOf(arg)` helper → e.g. `"string[]"` (a pure
    function computed on demand from an `ArgSpec`, mirroring the Python
    property; custom registered types fall back to `"unknown"`). `arg.type`
    stays the item type.

  Editor-level per-argument hints (`args.tag: list[str]`) still require the
  planned `runspec emit --stubs`; this is the runtime/introspection signal in
  the meantime.

### Fixed

- Documented the string-validation `Arg` fields (`pattern` / `min_length` /
  `max_length`) that were missing from the Python library reference.

### Internal

- Regression tests pinning that a `multiple` arg emits JSON Schema
  `{"type":"array","items":{…per-item constraints…}}` in both packs (behavior
  was already correct; now covered).

## [0.24.0] / [node-0.19.0] — 2026-06-03

### Fixed

- **`multiple` args now coerce and validate per item.** A `multiple = true`
  arg previously passed the whole accumulated list to the scalar type coercer,
  so `type = "str"` stringified the list (`"['a', 'b']"` in Python, `"a,b"` in
  Node) instead of returning a list, and `pattern` matched against that mangled
  string rather than each element. The arg's type is now applied to **each
  item**, returning a real list of coerced values (`type = "int"` →
  `[1, 2, 3]`), and every per-item check — `pattern` / `min-length` /
  `max-length` for `str`, `range` for numbers, `options` for `choice` — runs on
  each element.

### Added

- **Per-item failure reporting.** When one or more items in a `multiple` arg
  fail validation, the whole list is checked and every offending element is
  reported together by position and value:

  ```
  ✗  --ticket: 2 of 3 item(s) failed validation:

     • item 2 ('bad-2'):
       ✗  Invalid value for --ticket: 'bad-2'
          Expected: a value matching pattern '[A-Z]+-[0-9]+'
          Got: 'bad-2'
     • item 3 ('nope'):
       ...
  ```

  New `format_invalid_items` / `formatInvalidItems` formatter. `type = "rest"`
  (which manages its own list) is unaffected. Applied to Python and Node
  simultaneously.

## [node-0.18.0] — 2026-06-03

### Added

- **Node parity: string validation (`pattern`, `min-length`, `max-length`).**
  Ports Python 0.22.0 to the Node pack. `str` args accept a `pattern` regex
  (full-match semantics — anchored as `^(?:…)$`, reproducing Python's
  `re.fullmatch` even for top-level alternations) plus `min-length`/`max-length`
  character bounds. New `formatInvalidPattern` / `formatTooShort` /
  `formatTooLong` error formatters mirror the Python messages, and `runspec
  local --format mcp/openai/anthropic` emits native JSON Schema
  `pattern`/`minLength`/`maxLength`.

- **Node parity: required subcommands (`require-command`).** Ports Python
  0.23.0. A runnable or any nested command with its own `commands` can set
  `require-command = true`; parsing that level with no command throws a
  `RunSpecError` listing the available commands, while `loadSpec()` and schema
  emit stay unaffected (introspection must not be blocked). Help output labels
  the section `Commands (required):`.

  The shared compliance fixtures (`tests/integration/fixtures/complex.toml`) now
  exercise both features, verified identically by the Python and Node compliance
  suites.

## [0.23.0] — 2026-06-02

### Added

- **Required subcommands: `require-command`.** A runnable (or any nested command
  that itself has `commands`) can set `require-command = true` to make choosing a
  command mandatory, matching `argparse`'s `add_subparsers(required=True)`, Click
  groups, and `git`/`docker`/`kubectl`. Invoking that level with no command — or
  with a bare token that is not one of its commands — errors with the list of
  available commands; a mistyped token gets a `Did you mean` suggestion.

  ```toml
  [db]
  require-command = true

  [db.commands.migrate]
  [db.commands.seed]
  ```

  It is a **parent-level** flag (it governs the act of choosing among the
  commands, not any individual child) and is enforced only when parsing real CLI
  arguments — `load_spec()` introspection and `emit` are unaffected. New
  `MissingCommand` error class with a human-first formatter. (Node parity added
  in [node-0.18.0].)

## [0.22.0] — 2026-06-02

### Added

- **String validation: `pattern`, `min-length`, `max-length`.** `str` args can
  now declare format constraints. `pattern` is a regex the value must **fully**
  match (`re.fullmatch` semantics — anchored at both ends, so no `^`/`$`
  needed); `min-length`/`max-length` bound the character count. All three are
  `str`-only.

  ```toml
  jira-key = { type = "str", pattern = "[A-Z]+-[0-9]+", description = "e.g. PROJ-123" }
  slug     = { type = "str", min-length = 3, max-length = 40 }
  ```

  New `InvalidPattern` / `InvalidLength` error classes with human-first
  formatters. Emit maps these to native JSON Schema `pattern` / `minLength` /
  `maxLength`, so MCP hosts and survey forms enforce the same constraints.
  (Node parity added in [node-0.18.0].)

### Fixed

- **Parsed args now type-check ergonomically.** `RunSpec.__getattr__` was
  annotated `-> Arg`, so direct attribute access returned an `Arg`, which a type
  checker won't accept where the underlying `int`/`str`/`Path` value is expected
  — `workers: int = args.workers` failed with an assignment error, forcing
  callers to unwrap (`int(args.workers)` / `args.workers.value`) purely to
  satisfy the checker, defeating the transparent-value protocol. It is now typed
  `-> Any` (the same idiom as `argparse.Namespace` / `SimpleNamespace`): args are
  dynamic — their names and types come from `runspec.toml` at runtime — so `Any`
  is the honest static type, and gradual typing lets callers pin the type at the
  use site. Runtime behaviour is unchanged; the value is still a transparent
  `Arg`. A new `tests/test_typing.py` runs mypy over fixtures to guard this.

---

## [0.21.0] — 2026-06-02

### Added

- **`runspec.become` module — privilege escalation is now actually applied.**
  `run_as` was parsed, resolved, and validated but never used at execution
  time: both `runspec serve` and the console SSH executor ran the runnable as
  the login user, silently ignoring `run_as`/`become_method`. A new shared
  `runspec.become` module is the single source of truth for the SPEC *Remote
  Execution* command table:
  - `resolve_run_as()` — resolves the effective user from a literal string, a
    `$ENV` reference, a per-host mapping, or pattern rules, falling back to the
    declared default. An empty/unset `run_as` never escalates.
  - `validate_run_as_patterns()` — validates the pattern rules at parse time.
  - `build_become_argv()` — builds the escalation command for `sudo`, `su`,
    `pbrun`, and `dzdo`, honouring `become_flags` and passing the environment
    through `env(1)` so `RUNSPEC_*` variables survive `sudo`.

  `serve.py` builds the become command in `_handle_tools_call` and re-exports
  the former private names for compatibility. Verified end to end: a real
  `runspec serve` emits `sudo -H -u svc-deploy env RUNSPEC_AGENT=1 … <bin>`.

### Fixed

- **Inferred arg types were missing from emitted tool schemas.** `_build_schema`
  read the raw arg dicts, so an arg relying on type inference (no explicit
  `type`) was emitted as JSON Schema `"string"`, and inferred-required args were
  omitted from the `required` list. Each arg now runs through `infer_arg` before
  the schema is built, so `int`→`integer`, `float`→`number`, `flag`→`boolean`,
  `choice`→`string`+`enum`, and no-default→required all reach the emitted
  MCP / OpenAI / Anthropic schema. The implicit `[]` default on rest args is
  suppressed.

---

## [0.20.2] — 2026-06-01

### Fixed

- **Hyphenated arg names failed validation** — an argument whose TOML key
  contained a hyphen (e.g. `output-file`) was never seen as provided during
  validation: a required hyphenated arg always raised "Missing required
  argument" even when supplied, and group constraints on hyphenated args never
  triggered. `_parse_argv` stores values under the underscore-normalised name
  (`output_file`), but `validate_args` and `validate_groups` looked them up by
  the raw hyphenated name. Both now normalise the name before the lookup (the
  raw name is still used for display in error messages).

---

## [0.20.1] — 2026-06-01

### Fixed

- **Subcommand help usage-line ordering** — an arg declared on an *intermediate*
  command in a nested path was rendered before the command path in the `--help`
  usage line (e.g. `sample --region <…> --symbols <str> multi show`), as if it
  were a global. Only the root runnable's own args are globals now; any arg
  declared on a command along the path renders *after* the command path
  (`sample --region <…> multi show --symbols <str>`), and the *Global options*
  vs *Command options* sections are split on the same rule.

---

## [0.20.0] — 2026-06-01

### Added

- **Global (inherited) arguments for subcommands** — a runnable's top-level
  `args` now act as *global args* that every subcommand inherits. The effective
  argument set for a subcommand invocation is the runnable's top-level args
  merged with the args declared at each level along the resolved command path;
  a subcommand arg with the same name as an inherited one overrides the parent.
  A required global is required regardless of which subcommand is invoked.
  Documented in `spec/SPEC.md` → *Subcommands → Global (inherited) arguments*.

### Fixed

- **Subcommand resolution with global flags** — a runnable that declared both
  required top-level args and subcommands could not be invoked at all:
  `_resolve_subcommand` bailed as soon as the first argv token was a flag, and
  descending into a subcommand replaced the parent spec wholesale, dropping the
  top-level args. Global flags may now appear **before** the command token
  (`tool --region eu --env qa show --symbol X`), matching `git`/`docker` and
  Python's `argparse` subparsers. A flag value that happens to match a command
  name (`--region show`) is consumed as the value, not read as the command.

### Changed

- **Subcommand help** now surfaces inherited globals: `--help` for a subcommand
  renders a *Global options (inherited)* section alongside the command's own
  *Command options*, and the usage line places globals before the command path
  — the only ordering the parser accepts.

---

## [0.19.0] — 2026-05-27

### Added

- **Arg source provenance (`Arg.source`)** — every resolved argument now carries
  a `source` field that records where its value originated. Five values:
  - `"cli"` — provided explicitly on the command line
  - `"env"` — resolved from a system environment variable (auto `RUNSPEC_X_ARG_Y`
    convention or a developer-declared `env = [...]` alias)
  - `"runspec_env"` — resolved from the `.runspec_env` file loaded at parse time
  - `"spec_default"` — came from `default = ...` in `runspec.toml`
  - `"not_set"` — no value provided and no default declared (optional arg left absent)

  The distinction between `"env"` and `"runspec_env"` is accurate: `apply_env_file`
  now returns a `frozenset` of the keys it actually wrote to `os.environ` (keys
  already present in the environment are not overwritten and are excluded). This
  frozenset is threaded through `_apply_env` so each arg's source is definitively
  classified.

- **Arg provenance in the run_summary audit record** — `configure_logging()` now
  accepts an optional `invocation_args` dict (`{argname: {value, source}}`).
  `_emit_run_summary()` includes `args` (plain values) and `arg_sources`
  (provenance strings) in the file-handler log record, closing the loop from
  `Arg.source` to the persistent audit trail.

### Changed

- `apply_env_file()` return type changed from `dict[str, str]` to
  `tuple[dict[str, str], frozenset[str]]` — callers receive both the full file
  contents and the set of keys actually applied to `os.environ`.

### Internal

- Source tracking is a parallel `sources: dict[str, str]` that flows alongside
  `parsed_values` through `_parse_argv` → `_apply_env` → `_apply_defaults` →
  `_coerce_values`. The old `_determine_source()` stub is removed.
- Bridge `_parse_log_by_run_id` and `_parse_log_sequential` now read
  `extra.arg_sources` and pass it through to `HistoryRecord.argSources`.
- `HistoryView` shows provenance badges next to arg values for non-CLI sources:
  blue "env", purple ".env" (runspec_env), gray "default" (spec_default). CLI
  args show no badge — the common case is uncluttered.

---

## [0.18.0] — 2026-05-27

### Added

- **Per-invocation `run_id` in every JSON log record** — `configure_logging()`
  now generates a UUID4 for each process invocation and injects it into every
  JSON log record as `extra.run_id` via a `_RunIdFilter` on the file handler.
  Multi-user scenarios where several operators run the same runnable concurrently
  produce interleaved records in a single log file; `run_id` lets the history
  view (and any external log aggregator) separate runs cleanly without relying
  on sequential position between `run_summary` markers.

- **`print()` capture in the audit log** — a `_StdoutTee` replaces `sys.stdout`
  after handlers are set up. Every complete line written via `print()` is
  forwarded to `logger.info` (as `runspec.print`, marked `_from_print=True`).
  The file handler captures these records; the stdout console handler suppresses
  them to avoid double-printing. The result: runnables that use `print()` for
  user-visible output (e.g. for subprocess piping) are now fully represented in
  the audit log without any code changes required.

- **`run_id` in `run_summary`** — the summary record written at process exit
  includes `extra.run_id` alongside the existing `duration_ms`, `exit_code`, and
  `events` fields.

### Internal

- Bridge `_parse_log_text` now detects `run_id` presence and routes to
  `_parse_log_by_run_id` (one `HistoryRecord` per UUID group) or falls back to
  `_parse_log_sequential` (legacy logs from <0.18).

---

## [0.17.1] — 2026-05-26

### Fixed

- **Windows: `runspec local` now discovers runnables correctly** — `runspec local`
  (text and JSON output) filtered runnables through an entry-point existence check
  that never matched on Windows because pip installs entry points as `<name>.exe`
  launchers, not bare `<name>` files. Both the `--format json` callable filter and
  the `--format text` `[not callable]` marker now check for `<name>.exe` as a
  fallback on Windows.

---

## [0.16.0] — 2026-05-26

### Breaking

- **`RUNSPEC_ARG_*` renamed to `RUNSPEC_{RUNNABLE}_ARG_*`** — per-arg
  environment variables now include the runnable name as a middle segment to
  prevent clashes when multiple runnables share the same argument name
  (e.g. `run-this --region` and `run-that --region` both reacting to
  `RUNSPEC_ARG_REGION=europe`). New form:
  `RUNSPEC_<RUNNABLE_UPPERCASED>_ARG_<ARG_NAME_UPPERCASED>`.
  Applied to Python and Node simultaneously (parser, serve, logging_setup).
  Framework vars `RUNSPEC_AGENT` and `RUNSPEC_CONFIG` are unchanged.

### Added

- **`.runspec_env` file** — a `KEY=VALUE` dotenv file loaded at parse time and
  merged into `os.environ` (existing env vars win). Path resolution: four tiers,
  first match wins: `RUNSPEC_ENV_FILE` env var → per-runnable `runspec_env` key
  in `runspec.toml` → `[config] runspec_env` key → `{sys.prefix}/.runspec_env`
  (default, silent skip if absent). Relative paths in TOML keys resolve from
  `sys.prefix`. The venv is the deployment container — values placed there stay
  there across reinstalls of the package itself.
- **`RunSpec.get_runspec_env()`** — method on the parsed result that returns
  the loaded env file contents as a `SimpleNamespace` with lowercased keys
  (`MY_API_KEY` → `ns.my_api_key`). Returns an empty namespace when no file
  was found.
- **`runspec_env` TOML key** — accepted in `[config]` and in any per-runnable
  section to override the default file path for that runnable.
- **`runspec env` CLI command** — `runspec env` shows the default resolved file
  path and its contents; `runspec env <runnable>` shows the file resolved for a
  specific runnable, annotated by which resolution tier was used.
- **`runspec_` namespace reservation** — arg names starting with `runspec_` or
  `runspec-` now raise a hard error at parse time. Reserved for the framework
  (`runspec_runnable`, `runspec_autonomy`, `runspec_agent`, etc.).

---

## [0.14.0] — 2026-05-24

### Added

- **Run summary captures real invoking user** (Python and Node). The closing
  stderr line and JSON audit record now include who actually ran the tool.
  `SUDO_USER` is captured when present so the real person is recorded even
  when running as a shared account via `sudo`.
  Format: `user: alice` (no sudo) or `user: alice → root (sudo)`.

---

## [0.13.1] — 2026-05-22

### Fixed

- **`runspec serve` no longer injects spec defaults into subprocess env** —
  `_args_to_runspec_env` was falling back to spec defaults when the MCP call
  omitted an arg, then merging those defaults after `os.environ`. This
  overwrote `RUNSPEC_ARG_*` vars already set in the server environment,
  breaking the env-var default tier through `serve`. Fix: only inject
  explicitly-provided MCP args. Applied to both Python and Node.
- Node server version string was hardcoded to `0.6.0`; now reads from
  `package.json`.

---

## [0.13.0] — 2026-05-22

### Added (Breaking)

- **`RUNSPEC_ARG_*` env var tier for all args** — every arg now automatically
  reads `RUNSPEC_ARG_<ARGNAME>` as an environment variable fallback before the
  spec default. No author opt-in required. Resolution order:
  CLI arg → `RUNSPEC_ARG_*` → `env` aliases → spec default.
- `env` field now accepts a string or list of strings for developer-declared
  env aliases (for CI, Ansible, etc.) checked after `RUNSPEC_ARG_*`.

### Breaking

- Runtime-injected subprocess vars renamed: `RUNSPEC_DEBUG` →
  `RUNSPEC_ARG_DEBUG`, `RUNSPEC_NO_SUMMARY` → `RUNSPEC_ARG_NO_SUMMARY`, all
  other runtime-injected arg vars gain the `_ARG_` infix for consistency.
  Framework vars `RUNSPEC_AGENT` and `RUNSPEC_CONFIG` are unchanged.

---

## [0.12.2] — 2026-05-21

### Fixed

- **`parse()` now locates `runspec.toml` next to the calling module**, not just
  by walking up from `cwd`. Installed entry points (via `pip install`,
  `poetry install`, `uv sync`, etc.) previously only worked when the user
  happened to be inside the source tree — the cwd-walk never reached
  `site-packages/<pkg>/runspec.toml`. Resolution order is now:
  explicit `config_path=` → `RUNSPEC_CONFIG` env var → walk up from the
  caller's `__file__` (new) → walk up from cwd (fallback) → error.
  Verified end-to-end with `pip`, `poetry`, and `uv` in both editable and
  wheel install modes. Python only.

---

## [0.12.1] / [node-0.11.1] — 2026-05-21

### Fixed

- **File handler now follows the `--debug` toggle** — was previously hard-wired
  to DEBUG, which meant every imported library logging to root at DEBUG
  (urllib3, boto3, sqlalchemy, etc.) flooded the audit file. The file now
  defaults to INFO and flips to DEBUG together with stdout when `--debug` /
  `RUNSPEC_DEBUG=1` is set. Stderr stays pinned at WARNING. No new TOML key —
  the existing `--debug` flag auto-added by `[config.logging]` just governs
  both surfaces now. Applies to both Python and Node.

---

## [0.12.0] / [node-0.11.0] — 2026-05-20

### Changed

- **Console routing by level** — a single `logger.X` call now does the right
  thing in both CLI mode and agent mode. INFO and below go to stdout (plain
  message, reads like `print()`); WARNING and above go to stderr (prefixed
  with the level name). The split matches Unix stream conventions and means
  `runspec serve` can capture stdout as the MCP tool response without losing
  warnings/errors. Applies to both Python and Node.

### Removed

- **`level` knob** in `[config.logging]` — silencing INFO would break agent
  responses, so the threshold is no longer configurable.

### Added

- **`--debug` flag**, auto-added when `[config.logging]` is present (also
  settable via `RUNSPEC_DEBUG=1`). Includes DEBUG records and tracebacks on
  stdout for in-terminal debugging. The flag only *raises* visibility — it
  never silences anything. The `debug` name is reserved when
  `[config.logging]` is present.

---

## [0.11.0] / [node-0.10.0] — 2026-05-20

### Added

- **Extra fields on logger calls** — attach structured context to any log record.
  - Python: `logger.info('msg', extra={'user_id': '42', 'region': 'eu-west'})`
    (standard stdlib `extra=` API — no wrapper needed).
  - Node: `logger.info('msg', { user_id: '42', region: 'eu-west' })`;
    the `error` key is special and extracts an `Error` object.
  - Extra fields appear nested under `"extra"` in JSON file output and as
    `{key=value ...}` appended to console lines.
  - Sensitive key names (`token`, `password`, `api_key`, `secret`, etc.) are
    unconditionally redacted; other string values pass through the standard
    sensitive-data filter.

---

## [0.10.0] — 2026-05-20

### Added

- **`[config.logging]`** — define logging behaviour in `runspec.toml`. When
  present, `parse()` automatically configures Python's stdlib logging system.
  Developers just use `logger = logging.getLogger(__name__)` — no extra imports
  or setup calls needed.

  - **File logging** always on: `{package_dir}/logs/{runnable}.log`, structured
    JSON at DEBUG, with `midnight` rotation (7-day retention by default).
    Falls back to `~/logs/` when the package directory is not writable.
  - **Console logging** (non-agent mode): human-readable `HH:MM:SS LEVEL
    logger: msg`; tracebacks only when `level = "debug"`.
  - **Agent mode** (`RUNSPEC_AGENT=1`): no console handler — stderr is the
    MCP/SSH streaming side-channel. File log is the debugging interface.
  - **`--log-level` arg** auto-injected when `[config.logging]` is present,
    defaulting to the configured `level`. Also settable via `RUNSPEC_LOG_LEVEL`.
  - **Sensitive data filter** applied to all output: passwords, tokens,
    `Authorization` headers, URL credentials, and JSON/form-encoded credential
    fields are replaced with `[REDACTED]`. Filter errors are silent.
  - Rotation: `"N MB"`, `"N KB"`, `"N GB"` (size), `"daily"`, `"midnight"`,
    `"weekly"` (time). Defaults to midnight/7.

- **`RunSpec.runspec_prefix`** — new property returning the parent directory of
  `runspec.toml` (the package root). Useful when runnables need to resolve paths
  relative to the package.

---

## [node-0.9.0] — 2026-05-20

### Added

- **`[config.logging]`** ported to Node/TypeScript — parity with Python 0.10.0.
  When `[config.logging]` is present, `parse()` configures a lightweight logger
  automatically. Runnables call `getLogger(name)` (exported from `runspec-node`)
  to obtain a named logger; no other setup required.

  - **File logging** always on: `{package_dir}/logs/{runnable}.log`, structured
    JSON at DEBUG, with `midnight` rotation (7-day retention by default).
    Falls back to `~/logs/` when the package directory is not writable.
  - **Console logging** (non-agent mode): human-readable `HH:MM:SS LEVEL
    logger: msg`; tracebacks only when `level = "debug"`.
  - **Agent mode** (`RUNSPEC_AGENT=1`): no console handler.
  - **`--log-level` arg** auto-injected; also settable via `RUNSPEC_LOG_LEVEL`.
  - **Sensitive data filter** on all output: passwords, tokens, `Authorization`
    headers, URL credentials, and JSON/form-encoded credential fields replaced
    with `[REDACTED]`.
  - Rotation: `"N MB"`, `"N KB"`, `"N GB"` (size), `"daily"`, `"midnight"`,
    `"weekly"` (time). Zero new runtime dependencies — stdlib `fs`/`path`/`os`
    only.

- **`runspec_prefix`** — new getter on `ParsedArgs` returning the directory
  containing `runspec.toml` (the package root).

---

## [0.9.0] — 2026-05-19

### Fixed

- `runspec jump` error messages now tailor to whether the bin path was set
  explicitly or discovered via `PATH`, giving actionable guidance in each case.
- Locked the `jump-hosts.bin` field to `runspec`-named executables only —
  prevents accidental redirection to arbitrary binaries on the remote.
- `RUNSPEC_CONFIG` is now forwarded to MCP-served subprocesses, so jump
  invocations through `runspec serve` can find their config correctly.
- `--list-jump-hosts` JSON output now shows the effective `bin` value rather
  than `null` when the default is in use.
- Remote tool failures are correctly propagated as non-zero exit codes from
  `runspec jump`.

---

## [0.7.0] — 2026-05-18

### Changed

- **CLI renamed** — `discover` → `local`, `run` → `jump`. The `check` and `emit`
  commands have been absorbed into `local` (use `runspec local` for inline
  validation, `runspec local --format mcp` for schema emission).

- **`runspec local`** — lists every installed runspec-aware runnable with inline
  validation. Exits with code 1 on errors, making it usable as a CI check.
  Accepts `--format text|json|mcp|openai|anthropic` and `--script <name>` flags.

- **`runspec jump`** — replaces `runspec run`. Without a tool name, queries the
  registry and lists all available tools and their hosts. With a tool name and
  `--host`, connects via SSH and runs the tool. Everything after `--` is passed
  to the remote tool.

- **Subcommand flattening in `runspec serve`** — runnables with nested
  `.commands` are automatically expanded into flat MCP tools with
  underscore-joined names (e.g. `portal-api_orders_get-list`). The command path
  is prepended to argv at invocation time.

- **Script discovery in `runspec serve`** is now venv-bin only. The previous
  fallback that searched the TOML directory and guessed file extensions has been
  removed. Scripts must be installed (`pip install` or `pip install -e .`).

---

## [0.5.0] — 2026-05-18

### Added

- **Recursive dev-mode discovery** — `find_configs_dev()` now walks the full
  directory tree under the `.git` root, not just one level deep. Monorepos
  with `packages/python/mypkg/runspec.toml` layouts are found automatically.
  Skips `.venv`, `__pycache__`, `node_modules`, `dist`, `build`, and all
  hidden directories.

- **`test_finder.py`** — new test file covering `find_config` (walk-up) and
  `find_configs_dev` (recursive scan, skip dirs, multiple configs, no-git
  fallback).

### Changed

- **`runspec.toml` is now the sole supported format.** Support for reading
  runspec configuration from `pyproject.toml` (under `[tool.runspec.*]`) has
  been removed. All docs, specs, and examples updated accordingly.

---

## [0.2.0] — 2026-05-17

### Added

- **`runspec serve`** — starts a live MCP stdio server for the current environment.
  Exposes every runnable as an MCP tool over JSON-RPC 2.0 on stdin/stdout.
  Zero extra dependencies. Connect to Claude Desktop or any MCP-compatible agent
  via `claude_desktop_config.json`.

- **`output` field on runnables** — declares what the runnable writes to stdout.
  Values: `"text"` (default), `"json"` (agent can parse the response),
  `"html"` (reserved for future UI use).
  Surfaces as `x-output` in all emitted schemas.

- **`args.__agent__`** — `RunSpec` now exposes `__agent__: bool`. It is `True`
  when the runnable is called via `runspec serve` (detected from `RUNSPEC_AGENT=1`
  in the environment). Use it to switch output format for agent vs human callers.

- **Installed package discovery** — `runspec discover` now finds packages in the
  current Python environment that list `runspec` as a dependency. Checks package
  data files for a shipped `runspec.toml`, and falls back to `direct_url.json`
  for editable installs.

---

## [0.1.1] — 2026-05-17

### Fixed

- Added `Documentation` URL to PyPI metadata (previously a dead link on the
  project page).

---

## [0.1.0] — 2026-05-17

### Added

Initial release.

- **`runspec.parse()`** — finds config, resolves runnable, parses `sys.argv`,
  validates, coerces, and returns a `RunSpec`.
- **`RunSpec`** — argument namespace with full spec metadata (`__script__`,
  `__source__`, `__command__`, `__autonomy__`, `__spec__`, `__groups__`).
- **`Arg`** — transparent value wrapper; behaves as its native type in all
  expressions (arithmetic, comparison, iteration, path methods).
- **Inference rules** — type and required inferred from defaults and options.
- **Types** — `str`, `int`, `float`, `bool`, `flag`, `path`, `choice`.
- **Validation** — two-pass: individual args first, group constraints second.
- **Groups** — `exclusive`, `inclusive`, `at-least-one`, `exactly-one`,
  `conditional`.
- **Subcommands** — nested command dispatch under a runnable.
- **Autonomy** — per-runnable and per-arg levels; most restrictive wins.
- **`runspec check`** — validates the current project's runspec setup.
- **`runspec discover`** — finds runspec-aware runnables in the local project.
- **`runspec emit`** — emits tool schemas in MCP, OpenAI, or Anthropic format.
- **`register_type()`** — register custom types with a coercer function.
- **`load_spec()`** — loads spec without parsing `sys.argv` (for tooling).
- Python 3.10–3.13 support. Zero runtime dependencies on Python 3.11+.

[0.7.0]: https://github.com/JasonFinestone/runspec/releases/tag/v0.7.0
[0.5.0]: https://github.com/JasonFinestone/runspec/releases/tag/v0.5.0
[0.2.0]: https://github.com/JasonFinestone/runspec/releases/tag/v0.2.0
[0.1.1]: https://github.com/JasonFinestone/runspec/releases/tag/v0.1.1
[0.1.0]: https://github.com/JasonFinestone/runspec/releases/tag/v0.1.0
