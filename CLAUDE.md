# runspec — Claude Code Context

## What this project is

A language-agnostic, TOML-based interface specification for anything runnable.
Runnables (scripts, apps, MCP tools) define their interface in `runspec.toml`
inside the package directory. The library parses this at runtime, validates
arguments, and exposes a rich `RunSpec` object.

Read `DESIGN.md` for the full design history and rationale.
Read `spec/SPEC.md` for the canonical format specification.

---

## Repository Structure

```
runspec/
  CLAUDE.md                        ← you are here
  DESIGN.md                        ← full design document
  spec/SPEC.md                     ← canonical format specification
  packages/
    python/                        ← active development, reference implementation
      runspec/
        __init__.py                ← public API: parse(), load_spec(), register_type()
        finder.py                  ← locates runspec.toml
        loader.py                  ← reads TOML, normalises to internal dict
        inference.py               ← applies inference rules
        types.py                   ← type registry + runspec-python coercers
        validator.py               ← two-pass validation (args then groups)
        parser.py                  ← orchestrates full pipeline, returns RunSpec
        models.py                  ← Arg, Group, RunSpec dataclasses
        errors.py                  ← human-first errors with fuzzy suggestions
        cli.py                     ← discover, check, emit commands
      tests/
        test_inference.py
        test_types.py
        test_validator.py
      pyproject.toml
    node/                          ← active, published to npm as runspec-node
      src/
        cli.ts                     ← init, discover, check, emit, serve commands
        finder.ts                  ← locates runspec.toml
        loader.ts                  ← reads TOML, normalises to internal dict
        inference.ts               ← applies inference rules
        types.ts                   ← type registry + coercers
        validator.ts               ← two-pass validation
        parser.ts                  ← orchestrates pipeline, returns ParsedArgs
        serve.ts                   ← MCP stdio server
        models.ts                  ← TypeScript interfaces
        errors.ts                  ← typed error classes
      tests/
      bin/runspec.js               ← CLI entry point
    go/                            ← stub, not yet implemented
  tests/
    integration/
      fixtures/                    ← shared TOML configs all language packs test against
        simple.toml
        complex.toml
  .github/
    workflows/
      python.yml                   ← CI for Python (active)
      node.yml                     ← CI for Node (active)
      release.yml                  ← Python PyPI release + GitHub Release on v* tag
      node-release.yml             ← Node npm release + GitHub Release on node-v* tag
      go.yml                       ← stub
      integration.yml              ← compliance suite
```

---

## Key Design Decisions

**Format**
- `runspec.toml` is the only supported config format — no `pyproject.toml` support
- `runspec.toml` lives inside the package directory (e.g. `mypkg/runspec.toml`), not at the project root
- Runnables are top-level sections: `[greeter]`
- `[config]` is the only reserved name — everything else is a runnable

**Internally**
- Code uses `runnables` as the dict key (not `scripts`)
- `__script__` on RunSpec refers to the runnable's name — internal naming, not format

**Types**
- Types declared as strings in spec (`"int"`, `"path"`, etc.)
- Coercion handled by language packs — `runspec-python` is bundled with `pip install runspec`
- Custom types registered via `runspec.register_type(name, coercer)`

**Validation**
- Two-pass: individual args first, group constraints second
- Keeps both extensible independently

**Autonomy**
- Defaults to `"confirm"` — safe by default, opt-in to trust
- Resolution order: per-arg → script-level → `[config]` default → library default
- Escalation rule: most restrictive level wins

**Entry points**
- Entry point name must match the runspec runnable name — enforced by convention
- `runspec` binary auto-appears when added as a dependency
- Script discovery: venv bin only

**Discovery**
- `runspec local`, `runspec serve`: importlib.metadata only — installed packages, no filesystem scanning. `pip install -e .` to make a package visible.
- `runspec jump` takes the SSH host string directly on the command line — no local config lookup required.
- `.git` is the only project boundary marker — language agnostic (used by `runspec init --write-project` to locate the parent directory)

---

## Inference Rules (from spec/SPEC.md)

| Condition | Inference |
|---|---|
| `default = 42` | `type = "int"` |
| `default = 3.14` | `type = "float"` |
| `default = "json"` | `type = "str"` |
| `default = false` | `type = "flag"` |
| `options = [...]` | `type = "choice"` |
| No default present | `required = true` |
| `type = "path"` only | `required = true` |

Bool must be checked before int (bool is a subclass of int in Python).

---

## Python Specifics

```bash
# Requirements
Python 3.10+   (tomllib from stdlib on 3.11+; tomli dependency on 3.10)
Zero runtime dependencies on Python 3.11+

# Environment setup
cd packages/python
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check .
ruff format --check .

# Type check
mypy runspec/
```

`[config.logging]` (0.10.0+): `logger = logging.getLogger(__name__)` just works.
Extra fields (0.11.0): `logger.info('msg', extra={'user_id': '42'})` — standard stdlib
`extra=` API; fields appear nested under `"extra"` in JSON, appended as `{key=value}` on console.
Console routing (0.12.0): INFO → stdout (plain message, reads like `print()`);
WARNING+ → stderr with level prefix. Same in CLI and agent mode — in agent
mode `runspec serve` captures stdout as the MCP tool response, so `logger.info`
lines reach the agent automatically. File handler (JSON) is always on as
the audit trail; its level follows the same `--debug` toggle as stdout
(INFO by default — keeps third-party library DEBUG noise out of the audit
log). `[config.logging]` auto-adds a `--debug` flag (also `RUNSPEC_ARG_DEBUG=1`)
that flips DEBUG on everywhere — stdout, tracebacks, and the audit file —
in one shot. Stderr stays pinned at WARNING regardless. No `level` knob —
silencing INFO would break agent responses.

---

## Do Not

- Add runtime dependencies to the core library (stdlib only)
- Use `scripts` as a TOML key in runspec format examples
- Use `[tool.runspec.*]` format — `runspec.toml` only, always
- Put `runspec.toml` at the project root in examples — it belongs inside the package directory
- Change inference rules without updating `spec/SPEC.md` first
- Change the format without updating `spec/SPEC.md` first
- Name internal variables using the format key names (use `runnables`, not `scripts`)
- Use language-specific files (pyproject.toml, package.json) as project boundary markers — use `.git` only

---

## Node Specifics

```bash
# Requirements
Node 18+, TypeScript

# Environment setup
cd packages/node
npm install

# Run tests
npm test

# Build
npm run build

# Type check
npm run typecheck
```

Node mirrors the Python public API: `parse()`, `loadSpec()`, `registerType()`, `getLogger()`.
CLI commands: `init`, `local`, `jump`, `serve` (renamed in 0.8.0 to mirror Python).
`[config.logging]` implemented in 0.9.0 — runnables call `getLogger(name)` from `runspec-node`.
Extra fields (0.10.0): `logger.info('msg', { user_id: '42' })` — `error` key extracts an Error; all other keys appear under `"extra"` in the JSON log.
Console routing (0.11.0) mirrors Python: INFO → stdout; WARNING+ → stderr with level prefix. No `level` knob. `[config.logging]` auto-adds a `--debug` flag (`RUNSPEC_ARG_DEBUG=1`) that flips DEBUG on everywhere — stdout *and* the audit file (the file defaults to INFO, same as stdout, so external library DEBUG noise stays out of the log).
Run summary (0.12.0): same closing-line + audit-record shape as Python 0.12.3. `[config.logging] summary = true` (default on); per-invocation suppression via `--no-summary` or `RUNSPEC_ARG_NO_SUMMARY=1`. Log file now lands at `{project_root}/logs/{runnable}.log` — the nearest ancestor `package.json` skipping `node_modules`, falling back to `~/logs/`. `runspec serve` returns a `_meta.runspec` block on every `tools/call` response (`tool`, `duration_ms`, `exit_code`).

---

## Current Status

Both Python and Node packages are active and published.

| Package | Version | PyPI / npm |
|---|---|---|
| `runspec` | 0.17.0 | PyPI |
| `runspec-node` | 0.17.0 | npm |
| `runspec-chat` | 0.4.7 | PyPI |
| `runspec-registry` | 0.1.1 | PyPI (archived — registry was removed from `runspec serve` in favour of the SSH+MCP jump-host model) |

**Next:** Config-file value fallback (third tier in value resolution, currently "design for now").

---

## Good Claude Code Prompt Patterns

Be specific and bounded. Examples of good prompts:

```
Read CLAUDE.md. Then look at loader.py and write tests for load_raw
covering the runspec.toml format. Put them in
packages/python/tests/test_loader.py.
```

```
Read CLAUDE.md and inference.py. The infer_arg function needs to handle
the case where both 'type' and 'options' are declared — options should
win and set type to 'choice'. Write the fix and add a test for it.
```

```
Read CLAUDE.md and parser.py. Walk me through what happens step by step
when parse() is called for a script with a subcommand.
```

Avoid open-ended prompts like "build the library" — break work into
one module or one behaviour at a time.
