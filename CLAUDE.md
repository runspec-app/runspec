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

| Package | Version | PyPI / npm | Notes |
|---|---|---|---|
| `runspec` | 0.32.2 | PyPI | Core library (reference implementation) |
| `runspec-node` | 0.28.1 | npm | Node port |
| `runspec-console` | 0.42.0 | PyPI | pywebview desktop console — agentic chat + Forms + SSH jump-host fleet UI (frontend in `packages/console-ui`); desktop toast + taskbar flash on agent confirm-gate events (Windows; `[notifications]` extra); **Rooms** chat-room tab + `chat` trigger source — **always-on, multi-room**: rooms are a property of a fleet host (`room`/`room_port` on the host entry), joined over that host's own SSH tunnel, reconnecting, independent of triggers |
| `runspec-room` | 0.3.0 | (unreleased) | The chat-room server (`packages/console-room/`) — a "dumb message pipe with presence". Two front doors, one process: consoles via NDJSON over the SSH tunnel (localhost), people via an OIDC-authenticated browser web app (Starlette + uvicorn, TLS, SQLite transcript). See `docs/design/console-room-server.md` |
| `runspec-chat` | 0.4.7 | PyPI | Chainlit chat UI (superseded by runspec-console) |
| `runspec-linux` | 0.2.0 | PyPI | 21 Linux system admin runnables — `pip install` into remote venvs (thin wrappers over `runspec-linux-core`) |
| `runspec-linux-core` | 0.1.0 | PyPI | Pure-Python logic core behind `runspec-linux` — importable helpers, **no runspec dep / no runspec.toml / no entry points** (surfaces zero runnables) |
| `runspec-windows` | 0.1.2 | PyPI | 35 Windows system admin + Microsoft 365 (Outlook/Teams/Calendar/OneDrive) runnables |
| `runspec-jsm` | 0.1.0 | PyPI | 8 Jira Service Management (Data Center) service-desk runnables — raise/comment/transition/set-property + portal-field discovery |
| `runspec-snow` | 0.1.0 | PyPI | 8 ServiceNow ITSM runnables — create/get/comment/state/set-field over the Table API + Service Catalog discovery |
| `runspec-webops` | 0.1.0 | npm | HTTPS/TLS certificate + web-endpoint runnables (Node; `packages/node-webops`) |
| `runspec-registry` | 0.1.1 | PyPI | Archived — registry removed in favour of SSH+MCP jump-host model |

**Integration packages** live under `packages/python/` alongside the core library. Each is a
self-contained package: own `pyproject.toml`, `runspec.toml`, tests, and CI
workflow. Release tags follow `{name}-v*` (e.g. `linux-v0.1.0`).

**Releasing — read `docs/releasing.md` before cutting a release.** Tagging is
automated, publishing is manual, and they're decoupled: bumping a package's
`version` on `main` makes `auto-tag.yml` write the `{prefix}v{version}` tag, but
that tag is pushed with `GITHUB_TOKEN` so it does **not** trigger the
`*-release.yml` workflow. You publish by manually running the package's release
workflow (`workflow_dispatch`) on that tag. Agents: stop at the version bump +
changelog on the branch; never push a `{prefix}v*` tag yourself.

`runspec-linux` is a thin runnable shell over **`runspec-linux-core`** (0.2.0+):
each module parses args with `runspec`, calls the matching `runspec_linux_core`
helper, and renders the JSON/exit. The core package holds all the pure logic and
has **no `runspec` dependency, no `runspec.toml`, and no entry points**, so
installing it exposes the helpers for import without surfacing any runnables
(invisible to `runspec local` / `runspec serve` discovery, which only sees dists
that require `runspec`). Core helpers return plain data and *raise* on failure
(`ToolNotFoundError` for an absent system tool, `CommandError` for a non-zero
subprocess); the wrappers catch these to reproduce the CLI/agent behaviour.

Import the helpers two ways: `from runspec_linux_core import nc_send, system_info,
…` (zero-surface — depend on `runspec-linux-core` alone), or
`from runspec_linux import nc_send` (re-exported for back-compat; pulls in the
full fleet). So a wrapper runnable for a TCP-interface app that only wants the
helper should depend on `runspec-linux-core`, not `runspec-linux`. `nc_send(host,
port, command, wait, read_timeout)` and the rest are the public Python API.
Release ordering: publish `runspec-linux-core` before `runspec-linux` (see
`docs/releasing.md`).

`runspec-windows` is the Windows counterpart: 35 runnables across system admin
(`subprocess`/`ctypes`/`winreg` — no pywin32) and Microsoft 365 via Graph
(Outlook + Teams + Calendar + OneDrive). OS-specific calls sit behind `_platform.py` helpers; the pure
output parsers and Graph formatters are unit-tested on any platform (CI runs them
on ubuntu plus a `windows-latest` smoke job). Graph auth is MSAL **device-code**
(public client, no secret); the `[graph]` extra carries `msal`/`httpx`, and the
package exports `graph_get()` / `get_token()` as a public API (parallel to
`nc_send`). runspec-console depends on it (`sys_platform == 'win32'` marker) so
the runnables surface in the console UI automatically. CI mirrors the linux jobs
(`windows-quality`/`windows-unit`/`windows-smoke`); release tag prefix is
`windows-v`.

`runspec-jsm` targets **Jira Service Management Data Center** for service-desk
agents and customers (not Jira product admins): 8 runnables for raise / comment
(public or internal) / transition (by name or id) / set-property, plus
request-type + portal-field discovery (`jsm-list-request-types`,
`jsm-list-fields`). Pure HTTP via `httpx` over a **mixed** REST surface
(`/rest/servicedeskapi` for raise/comment/discovery, `/rest/api/2` for
transitions/properties), so it runs unchanged on Windows and Linux. Auth is
**env-var only** — `JSM_BASE_URL` + (`JSM_PAT` Bearer *or* `JSM_USER`/`JSM_PASSWORD`
Basic; PAT wins), with optional `JSM_<ENV>_*` prefixes selected by the
`--env dev|uat|prod` arg on every runnable. **Identity is deploy-determined — no
sudo flag:** run it as yourself locally or as the service account on a server
(`sudo -u <svc> -E` / systemd / login shell); it makes no OS calls. It exports a
public Python API where every operation takes a `JsmConfig` first arg
(`config_from_env`, `create_request`, `add_comment`, `transition_issue`,
`set_issue_property`, `list_request_types`, `get_request_type_fields`,
`resolve_request_fields`, …) so a private (e.g. Nexus-hosted) package can import
them, inject credentials in code, and bake in a default `service_desk_id` /
`project_key` / `env` to build "one runnable per project" on top. CI mirrors the
linux jobs (`jsm-quality`/`jsm-test`); release tag prefix is `jsm-v`.

`runspec-snow` is the ServiceNow counterpart of `runspec-jsm`, for teams
migrating ITSM tasks to ServiceNow: 8 runnables for create / get (by sys_id or
number) / comment / state / set-field, plus read-only Service Catalog discovery
(`snow-list-catalog-items`, `snow-list-variables`). Tuned for `incident`,
`sc_task` and `sc_req_item` but `--table` accepts any table. Pure HTTP via `httpx`
over the **Table API** (`/api/now/table/{table}`) for CRUD and the **Service
Catalog API** (`/api/sn_sc/servicecatalog`) for discovery. ServiceNow's
`comments` (customer) vs `work_notes` (internal) maps onto JSM's public/internal
split (`--internal`); it has no Jira-style transition endpoint, so a "transition"
is a validated `state` choice-field update with label→value resolution via
`sys_choice`. Auth is **env-var only** — `SNOW_BASE_URL` (or `SNOW_INSTANCE`) +
(`SNOW_TOKEN` Bearer/OAuth *or* `SNOW_USER`/`SNOW_PASSWORD` Basic; token wins),
with optional `SNOW_<ENV>_*` prefixes selected by `--env`. Same **deploy-determined
identity — no sudo flag** model as `runspec-jsm`. It exports a public Python API
where every operation takes a `SnowConfig` first arg (`config_from_env`,
`create_record`, `get_record`, `add_comment`, `update_state`, `resolve_state_value`,
`set_fields`, `list_catalog_items`, …) so a private package can inject credentials
and bake in a default `default_table` / `env` to build "one runnable per table" on
top. CI mirrors the linux jobs (`snow-quality`/`snow-test`); release tag prefix is
`snow-v`.

**Docs drift checks.** `scripts/check_docs.py` guards the MkDocs site against
drift and runs in CI (`docs-check` job, gated by a `docs` paths filter) and
optionally via `.pre-commit-config.yaml`. Three checks: (1) **version-sync** —
each package manifest version must have a matching `CHANGELOG.md` entry
(`## [x]` for Python, `## [node-x]` for Node); (2) **toml-examples** — every
runspec ` ```toml ` block in `docs/` validates against
`schema/runspec.schema.json`; (3) **format-reference** — the arg-field and type
tables in `docs/format.md` must match the schema. Practical upshot: when you
add or change a format field/type, update `schema/runspec.schema.json` and the
`docs/format.md` tables in the same change, and add a CHANGELOG entry for any
version bump — or `docs-check` fails. (`KNOWN_CUSTOM_TYPES` in the script lists
the custom `register_type` names demonstrated in the docs.)

**Next:** Config-file value fallback (third tier in value resolution, currently "design for now").

**Queued:**
- `runspec emit --rundeck` — emit a Rundeck job YAML per runnable to
  `jobs/{runnable}.yaml` at the git root. Always uses a Bash script step
  (not exec) so flag args can be conditionally included. Output goes to
  file; user uploads via Rundeck UI. See `docs/design/emit-ansible-rundeck.md`.
- `runspec emit --ansible` — emit an Ansible module Python file per
  runnable to `library/{runnable}.py`. Generates `DOCUMENTATION`,
  `argument_spec`, and subprocess invocation from `runspec.toml`. Full
  AAP survey form and ansible-lint compatibility. See
  `docs/design/emit-ansible-rundeck.md`.
- `runspec push --rundeck` — future: POST job YAMLs to Rundeck REST API
  (`/api/v*/project/{project}/jobs/import`) with an API token. Deferred
  until file emit is in production use.
- `runspec emit --stubs` — generate a PEP 561 `.pyi` per package from
  `runspec.toml` so a parsed `RunSpec` types precisely (`args.workers` →
  `int`, typos caught, choices as `Literal[...]`) instead of `Any`. The
  consuming-side counterpart to the SchemaStore JSON Schema (authoring
  side). Generated artifact, committed and regenerated only when
  `runspec.toml` changes; staleness enforced via `--check` in CI /
  pre-commit — not run on every edit. Realizes the `emit --python-types`
  flag referenced in the emit doc. See `docs/design/emit-stubs.md`.

**Node parity — done (node-0.18.0):** string validation (`pattern`/`min-length`/
`max-length`) and `require-command` are now implemented in the Node pack at parity
with Python 0.22.0/0.23.0. The shared compliance fixtures
(`tests/integration/fixtures/complex.toml`) now exercise both and are verified
identically by the Python and Node compliance suites.

Build order: `emit --rundeck` first (current platform), `emit --ansible`
second (migration target), `push --rundeck` last (CI/CD automation).

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
