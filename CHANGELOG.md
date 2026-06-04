# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers follow [Semantic Versioning](https://semver.org/).

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
