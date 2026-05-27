# Design: `runspec emit --ansible` and `runspec emit --rundeck`

## Problem

Runnables defined in `runspec.toml` are already human-runnable CLIs, MCP
tools, and SSH-jumpable scripts. But teams that use Ansible Automation
Platform (AAP) or Rundeck for job orchestration today have to maintain
**two sources of truth**: the runnable itself, and a separately-maintained
job definition in their orchestration platform.

When the runnable's args change, the platform job definition is manually
updated — or silently goes stale.

---

## The migration story

This is the core use case that motivated the feature:

```
runspec.toml  ←  single source of truth
     │
     ├── runspec emit --rundeck  →  Rundeck job YAML  (current platform)
     └── runspec emit --ansible  →  Ansible module    (future platform)
```

A team migrating from Rundeck to Ansible Automation Platform can ship
runnables now, run them in Rundeck today, and emit Ansible modules for
AAP tomorrow — **with no changes to the runnable or its `runspec.toml`**.

This is directly relevant to teams where the Rundeck → AAP migration is
a multi-year programme. Runnables shipped before the migration is complete
are forward-compatible with the destination platform by design.

---

## The `env` field connection

The `env` field on args (added in 0.13.0) was designed with this use case
in mind:

```toml
[scan]
description = "Scan a target directory"

[scan.args.target]
type = "path"
description = "Path to scan"
env = ["ANSIBLE_SCAN_TARGET", "RUNDECK_SCAN_TARGET"]
```

Ansible playbooks and Rundeck jobs can pass values via environment
variables without the emit layer needing special treatment — the runnable
already declares its env aliases.

---

## Phase 1 — generic thin wrapper (low priority)

A single `runspec_run` Ansible module or Rundeck job that takes any
runnable name plus an `args` dict:

```yaml
# Ansible
- runspec_run:
    runnable: scan
    venv: /opt/myapp/.venv
    args:
      target: /var/www
      depth: 3
```

Ships as `runspec-ansible` on PyPI. Useful immediately with no codegen.
No `DOCUMENTATION` block per runnable — ansible-lint and IDE support
are limited. This is a valid stopgap if Phase 2 is not yet available.

**Status: not the priority.** Phase 2 (emit) delivers more value and is
built once. Proceed to Phase 2 directly.

---

## Phase 2 — `runspec emit` targets (the main feature)

### `runspec emit --ansible`

Emits a per-runnable Ansible module Python file to `library/{runnable}.py`.
Nothing goes to stdout except confirmation lines:

```
Wrote library/scan.py
Wrote library/deploy.py
```

The generated file has full Ansible ecosystem compatibility:

```python
DOCUMENTATION = '''
module: scan
short_description: Scan a target directory
description:
  - Scans a target directory to the specified depth.
options:
  target:
    description: Path to scan
    required: true
    type: path
  depth:
    description: Scan depth
    required: false
    type: int
    default: 3
  verbose:
    description: Enable verbose output
    required: false
    type: bool
    default: false
'''

EXAMPLES = '''
- name: Scan web root
  scan:
    target: /var/www
    depth: 3
'''

RETURN = '''
stdout:
  description: Captured stdout from the runnable
  type: str
changed:
  description: Whether the runnable reported a change
  type: bool
'''
```

The module body translates `module.params` to CLI flags and invokes the
runnable subprocess. JSON output with a `changed` key is used directly;
non-JSON output defaults to `changed: true` (see Idempotency below).

#### Type mapping — runspec → Ansible

| runspec type | Ansible argument_spec type | Notes |
|---|---|---|
| `"str"` | `str` | Direct |
| `"int"` | `int` | Direct |
| `"float"` | `float` | Direct |
| `"flag"` | `bool` | Direct |
| `"path"` | `path` | Direct |
| `"choice"` | `str` + `choices=[...]` | `options` list becomes choices |
| `"rest"` | `list`, `elements: str` | Space-joined when building argv |

#### Idempotency convention

Ansible tasks expect `changed: true/false`. Runnables targeting Ansible
should include a `changed` key in their JSON output (`output = "json"`):

```python
print(json.dumps({"changed": False, "result": "already up to date"}))
```

Runnables that do not emit `changed` in their JSON output default to
`changed: true`. The module passes through `check_mode` by skipping
execution and returning `changed: false` when `module.check_mode` is true
(conservative — assumes nothing would have changed).

This is a **convention**, not a spec enforcement. Document it in the
runspec format guide under "Ansible-compatible runnables". Do not add
a lint rule or parse-time check — many runnables are not intended for
Ansible use.

#### Autonomy

The generated module injects `RUNSPEC_AUTONOMY=autonomous` into the
subprocess environment so existing runnables with `autonomy = "confirm"`
work correctly in a non-interactive playbook run without requiring the
runnable author to change their TOML.

#### AAP note

Ansible Automation Platform generates a survey/form UI from module
parameters automatically — the `DOCUMENTATION` block drives it. Teams
get a proper AAP job form with field labels and descriptions without any
extra work.

---

### `runspec emit --rundeck`

Emits a per-runnable Rundeck job YAML file to `jobs/{runnable}.yaml` at
the `.git` root (same boundary marker as `runspec init`). Nothing goes
to stdout except confirmation lines:

```
Wrote jobs/scan.yaml
Wrote jobs/deploy.yaml
```

The user uploads the YAML manually: Rundeck UI → Project → Jobs → Upload.

#### Always use script step

Every emitted job uses a Bash script step, never a plain `exec:` step.
Reason: `exec:` is a static string — it cannot conditionally include a
`--flag` based on a boolean option value. Script step handles this
correctly and is idiomatic Rundeck:

```yaml
- name: scan
  description: Scan a target directory
  group: runspec
  loglevel: INFO
  sequence:
    keepgoing: false
    strategy: node-first
    commands:
    - script: |
        #!/bin/bash
        ARGS=""
        ARGS="$ARGS --target ${option.target}"
        ARGS="$ARGS --depth ${option.depth}"
        if [ "${option.verbose}" = "true" ]; then
          ARGS="$ARGS --verbose"
        fi
        /opt/myapp/.venv/bin/scan $ARGS
      interpreter: bash
  options:
  - name: target
    label: Target path
    description: Path to scan
    required: true
    type: text
  - name: depth
    label: Scan depth
    description: How deep to scan
    required: false
    type: integer
    value: '3'
  - name: verbose
    label: Verbose output
    required: false
    type: text
    values:
    - 'true'
    - 'false'
    enforced: true
    value: 'false'
```

Script step generation rules:
- **Named args:** `ARGS="$ARGS --{name} ${option.{name}}"`
- **Flag args:** wrapped in `if [ "${option.{name}}" = "true" ]` — flag
  appended without a value
- **Positional args:** appended in `position` order without flag prefix
- **Rest args:** single option, document as space-separated in description;
  runnable splits on receipt. Emit as `ARGS="$ARGS ${option.{name}}"`.

The venv bin path (`/opt/myapp/.venv/bin/scan`) is a required emit
parameter — either declared in `runspec.toml` under `[config]` or passed
as `runspec emit --rundeck --venv /path/to/.venv`.

#### Type mapping — runspec → Rundeck

| runspec type | Rundeck option type | Notes |
|---|---|---|
| `"str"` | `text` | Direct |
| `"int"` | `integer` | Direct |
| `"float"` | `float` | Direct |
| `"flag"` | `text`, `values: [true, false]`, `enforced: true` | No native bool in Rundeck; conditional in script handles actual flag passing |
| `"path"` | `text` | No native path type in Rundeck |
| `"choice"` | `text`, `values: [...]`, `enforced: true` | Direct |
| `"rest"` | `text` | Space-separated string; split by runnable |

Required args: `required: true` in the Rundeck option. Defaults: always
stringified as `value: '{default}'`.

#### No step plugin

A Rundeck step plugin would provide tighter integration but requires
company approval for older Rundeck versions. The YAML job + manual upload
approach works without any plugin installation.

#### Rundeck API push (future)

The Rundeck REST API supports job import without any plugin:

```
POST /api/v*/project/{project}/jobs/import
X-Rundeck-Auth-Token: <token>
Content-Type: application/yaml
```

A future `runspec push --rundeck` command could automate the upload step
for CI/CD pipelines (deploy venv → `runspec push --rundeck` syncs all
jobs). This does not require any plugin — only an API token.

Design gate: implement file emit first; add `push` once file emit is in
production use and the venv path config story is settled.

---

## Emit output location

Both emits write to a directory relative to the `.git` root — the same
boundary marker used by `runspec init`:

| Flag | Output directory | File per runnable |
|---|---|---|
| `--ansible` | `{git_root}/library/` | `{runnable}.py` |
| `--rundeck` | `{git_root}/jobs/` | `{runnable}.yaml` |

`--output-dir <path>` overrides the default directory for both flags.

Both directories are safe to commit to the project repository — the
generated files are the deployment artefacts.

---

## Existing `emit` surface

`runspec emit` already supports:

```
runspec emit --json-schema
runspec emit --python-types
```

The two new flags extend this surface cleanly. The underlying data (arg
names, types, descriptions, defaults, choices) is already fully available
in the parsed `RunSpec` — no new TOML fields are required.

---

## Build order

1. `emit --rundeck` — current platform, most immediately useful.
   Validate type mapping and script step generation against real runnables.
2. `emit --ansible` — future platform target.
   Idempotency convention needs documenting in `docs/format.md` before
   or alongside implementation.
3. `push --rundeck` (API) — deferred until emit is in production use.

Both emits are Python-only initially; Node port follows once the Python
implementation is stable.

---

## Audit trail continuity

`[config.logging]` writes a JSON audit record on the managed host for
every invocation. In an Ansible playbook run or a Rundeck job execution,
this means per-tool invocation logs on each host — more granular than
Ansible task results or Rundeck execution logs, and consistent with what
`runspec serve` produces. No extra runnable author work required.
