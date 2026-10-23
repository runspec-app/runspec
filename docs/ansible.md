# Ansible modules

`runspec emit --ansible` generates an [Ansible](https://www.ansible.com/) module
from each installed runnable, so `runspec.toml` stays the single source of truth
for an Ansible / **Ansible Automation Platform (AAP)** fleet instead of a
hand-maintained module that drifts whenever an arg changes.

It is the sibling of [`runspec emit --rundeck`](rundeck.md): same command, same
per-leaf model, same secret handling — a different output artefact.

- **One module per leaf command.** A plain runnable emits one module; a runnable
  with subcommands flattens to one module per fully-pathed leaf — `cron_add.py`,
  `cron_list.py` — like `runspec serve`.
- Modules land in `{git_root}/library/{module}.py` (or `--output-dir`), safe to
  commit as deployment artefacts. `library/` is on Ansible's default module path.
- Generated with the standard library alone — no runtime dependency added to the
  core.

## Quick start

```bash
# From your project (the venv where the runnables are installed):
runspec emit --ansible --venv /opt/app/.venv
# Wrote library/scan.py
# Wrote library/deploy_start.py
# ...
```

Then use the module in a playbook — a task named for the module:

```yaml
- hosts: web
  tasks:
    - name: Scan the web root
      scan:
        directory: /var/www
        older_than: 7
        format: json
      register: result
```

## What's in a module

Each generated module is a standard Ansible module built on
`ansible.module_utils.basic.AnsibleModule`. It carries:

- **`DOCUMENTATION`** — module name, `short_description`, and an `options` block
  for every arg. This drives the **AAP survey/job form** automatically (field
  labels, types, choices, defaults), plus `ansible-doc` and IDE support.
- **`EXAMPLES`** and **`RETURN`** blocks (`changed`, `rc`, `stdout`, `stderr`,
  and a parsed `result` when the runnable emits a JSON object).
- An **`argument_spec`** built from the args, and a small constant body that
  rebuilds the CLI invocation from `module.params` and shells out to the
  runnable — named args, `flag` args (added only when true), positionals (in
  `position` order), and a `rest` arg word-split after `--`.

### Type mapping

| runspec type | Ansible `argument_spec` |
|---|---|
| `str`, `path` | `str` / `path` |
| `int`, `float` | `int` / `float` |
| `flag` | `bool` |
| `choice` | `str` + `choices: [...]` |
| `rest` | `list`, `elements: str` |
| `password` | `str`, `no_log: true` (see below) |

Required args set `required: true`; defaults pass through as native values.

### Secrets

A `password` arg becomes a `no_log` parameter (Ansible masks it in output) and is
passed to the runnable through the runspec secret channel — the module sets
`RUNSPEC_<RUNNABLE>_ARG_<NAME>` in the subprocess environment rather than putting
it on the command line. Supply it from Ansible Vault or a lookup:

```yaml
- name: Deploy
  deploy_start:
    env: prod
    target: web-01
    token: "{{ lookup('env', 'DEPLOY_TOKEN') }}"   # or a vaulted var
```

### Idempotency

Ansible tasks report `changed: true/false`. A runnable that prints a `changed`
key in its JSON output (`output = "json"`) drives it directly:

```python
print(json.dumps({"changed": False, "result": "already up to date"}))
```

A runnable that doesn't emit `changed` reports `changed: true` on a zero exit.
`check_mode` (`--check`) short-circuits to `changed: false` without running —
conservative (assumes nothing would have changed).

### Autonomy

The module injects `RUNSPEC_AUTONOMY=autonomous` into the subprocess environment,
so a runnable declared `autonomy = "confirm"` runs in a non-interactive playbook
without any change to its `runspec.toml`. It also sets `RUNSPEC_LABEL=ansible`,
so the run shows the label `ansible` in `runspec history` and the console's
History tab (modules emitted before runspec 0.76.0 don't; re-emit them).

## Per-runnable settings

The only Ansible-specific setting is the **venv** whose `bin/` holds the runnable
binary (the module's invocation path). Resolve it with `--venv` (project-wide) or
a `meta.ansible` pass-through table:

```toml
[config.meta.ansible]
venv = "/opt/app/.venv"

[deploy]
description = "Deploy the app"
require-command = true

[deploy.meta.ansible]
venv = "/opt/deploy/.venv"   # overrides the project default for this runnable
```

Precedence: `--venv` flag → `[<runnable>.meta.ansible]` → `[config.meta.ansible]`.
With no venv resolved, modules invoke the bare binary name (relying on `PATH`) and
the command prints a one-line note.

## Command reference

| Flag | Meaning |
|---|---|
| `--ansible` | Select the Ansible target |
| `--venv <path>` | Venv `bin/` for the module invocation (project-wide) |
| `-d, --output-dir <dir>` | Where to write (default `{git_root}/library`) |
| `-r, --runnable <name>` | Emit only this runnable |
| `--check` | Verify emitted files are up to date; write nothing. Exit 1 if stale |

### Keeping modules fresh in CI

Commit `library/` and guard drift the same way `runspec stubs --check` does:

```bash
runspec emit --ansible --venv /opt/app/.venv --check
# ✗  library is out of date — run 'runspec emit --ansible'   (exit 1)
```

## Rundeck too

Migrating between platforms? The same runnables emit **both** — run in Rundeck
today (`runspec emit --rundeck`, see [Rundeck jobs](rundeck.md)) and generate
Ansible modules for AAP tomorrow, with no change to the runnable.

## Not covered (yet)

- **`run_as`** — the module invokes the venv binary directly; escalate with the
  playbook's `become:` if the runnable needs a different user.
- **Group constraints** (`exclusive`, `exactly-one`, …) have no Ansible
  `argument_spec` equivalent beyond `mutually_exclusive`/`required_one_of`; they
  are still enforced by the runnable at runtime.
