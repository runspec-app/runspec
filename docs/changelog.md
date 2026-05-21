# Changelog

!!! note
    This page mirrors [`CHANGELOG.md`](https://github.com/JasonFinestone/runspec/blob/main/CHANGELOG.md)
    at the repository root.

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers follow [Semantic Versioning](https://semver.org/).

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
