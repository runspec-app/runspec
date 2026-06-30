# Design: console-driven local Ansible deployment (`runspec-playbook`)

## Problem

A team deploys `runspec-*` runnable packages to a fleet today with **GitLab CI →
Nexus → Ansible**: CI packages and publishes the runnables to a Nexus PyPI
index; Ansible playbooks (run from CI or a control node, over SSH, against an
inventory of hosts) create venvs on each host and `pip install` the packages
into them.

That means **two host lists and two SSH stacks**: the Ansible inventory, plus
the runspec-console's own `runspec_hosts.toml` and its standing paramiko
connections to the same fleet. When a host is added, removed, or renamed, both
must change. Deployment also lives outside the console even though the console
already reaches every one of those hosts.

This design keeps GitLab CI for **build + publish** (its strength) and moves the
**deploy step into the runspec-console**, using the host list and SSH the
console already has.

## Approach: run the playbook *locally on the target host*

The naive version — "wrap `ansible-playbook` in a runnable and let Ansible SSH
to the fleet" — reintroduces a second SSH stack: Ansible would open its own
OpenSSH connections (separate from the console's paramiko transports, blind to
the console's connection state, credentials, and per-host denylist). It also
needs an inventory generated from `runspec_hosts.toml`.

Instead, run Ansible with **`connection: local`**. The console reaches the host
over its own SSH (the host in `runspec_hosts.toml`) and invokes a runnable
there; that runnable runs the playbook against `localhost`. **Ansible opens no
SSH of its own.** This is the well-established `ansible-pull` pattern — each node
runs playbooks against itself — except the console *pushes the trigger* instead
of a cron job pulling.

```
runspec-console ──SSH──▶ host ──▶ run-playbook ──▶ ansible-runner (connection: local) ──▶ playbook
        (runspec_hosts.toml)                                            (acts on localhost — no SSH)
```

### Why this is the right shape

- **One connection model.** Only the console's SSH is involved. Connection
  state, profiles, the per-host denylist, and the audit trail all keep applying
  at the runnable level — Ansible inherits none of that responsibility.
- **One host list.** `runspec_hosts.toml` is the single source of truth.
  Ansible's "inventory" is just `localhost`; there is no remote inventory to
  generate or drift.
- **Full playbook richness.** Unlike the native `create-venv`/`install-into-venv`
  runnables (which cover only venv + pip), a playbook brings idempotent modules,
  handlers, templates, roles, `when:`, `become:`, and `--check`/`--diff`. This
  is why teams whose playbooks do more than venv+pip want it.
- **Idempotency for free.** Ansible's `pip`/`file`/`package` modules are
  idempotent, so re-running a deploy is safe — closing the gap that `create-venv`
  has (it fails if the venv exists unless `--clear`, which is destructive).

### The constraint that decides whether it fits

`connection: local` means **each host only knows about itself.** Anything that
coordinates *across* hosts will not work this way:

- `delegate_to` / `run_once` / reading another host's `hostvars`
- `serial:` rolling deploys across the fleet ("drain the LB → deploy → re-add")

So this model fits **per-host playbooks** (provisioning/config of the box they
run on) — the common case for config management. A fleet whose playbooks
orchestrate multi-host sequences needs a real Ansible control node for *those*
playbooks and would run them the classic way (a hybrid). `runspec-playbook` is for
the per-host case.

## The runnable

`runspec-playbook` ships one runnable, **`run-playbook`** (`autonomy = "confirm"`,
`output = "json"`):

| Arg | | Purpose |
|---|---|---|
| `package` | positional 1 | Package to deploy; supports extras (`myapp[postgres,redis]`). |
| `version` | positional 2 | Exact version; composed into `<package>==<version>`. |
| `--playbook` | choice | Which bundled playbook to run (default `deploy`). |
| `--check` / `-C` | flag | Dry run — Ansible check mode. |
| `--extra-var k=v` | repeatable str | Extra Ansible variables, merged after the `deploy_*` vars. |
| `--tags` / `--skip-tags` | str | Ansible tag filters. |
| `--verbosity` / `-v` | int 0–4 | Ansible verbosity. |

The package-name + version requirement from the brief is met by the two
positionals: extras are carried through verbatim and the version is pinned, so
`run-playbook 'myapp[postgres]' 1.4.2` installs exactly `myapp[postgres]==1.4.2`.
The runnable always passes `deploy_package`, `deploy_version`, and
`deploy_package_spec` to the playbook as extra-vars.

### Why `ansible-runner`, not `ansible-playbook`

The runnable drives **`ansible-runner`** — the programmatic runner AWX/AAP
themselves use — rather than shelling out to the `ansible-playbook` CLI and
parsing stdout. `ansible-runner` returns structured results (`status`, `rc`,
per-result `stats`), which map cleanly onto the runnable's JSON output and the
repo's documented Ansible `changed` convention (see
`emit-ansible-rundeck.md` → "Idempotency convention"). The bundled `playbooks/`
tree is copied into a throwaway private data dir per run, so artefacts never
litter the host.

### Privilege

Privilege escalation is the playbook's job via `become:` — the runnable runs as
the SSH user and does not declare `run_as`. The existing
`enable-passwordless-sudo` bootstrap (runspec-linux) covers passwordless sudo
on the host. This is simpler and more capable than per-runnable `run_as` for
playbook-shaped work.

## Secrets — the important part

The brief asks how to pass "many secrets as env vars for the playbook" — the
Nexus credentials that are GitLab CI/CD variables today — and weighs a packaged
`.env`. The recommendation:

### Do **not** package a `.env`

A wheel/sdist on the index is readable by anyone with read access; package data
is not encrypted; and it would land on every host's disk in `site-packages` (and
possibly in build logs). Bake only **non-secret** config into a playbook
variable (an index host, a default venv path) — never a credential.

### Use the console's Credentials → env-var channel

The runspec-console already stores named credentials with the **secret value in
the OS keychain** (Windows Credential Manager / macOS Keychain / Secret Service
via `keyring`); only metadata is written to disk. At run time it injects them as
**plain environment variables derived from the credential's label + kind**, and
`run-playbook` forwards the environment to the playbook. Tasks read them with
`lookup('env', 'NAME')`. Secrets never touch the package, the command line, or
the audit log.

Name the credential so the convention yields the names the playbook expects:

| Credential label | Kind | Env vars injected |
|---|---|---|
| `Nexus` | token | `NEXUS_TOKEN`, `NEXUS_PAT` |
| `Nexus` | userpass | `NEXUS_USERNAME`, `NEXUS_USER`, `NEXUS_PASSWORD` |
| `Deploy Key` | token | `DEPLOY_KEY_TOKEN`, `DEPLOY_KEY_PAT` |

**Many secrets:** the console supports **multiple credentials per run** — the
Forms run dialog's credential section lets the operator select several, each
contributing its env vars, all forwarded to the playbook. These are the GitLab
CI/CD variables you have today, now sourced from the keychain instead of a CI
settings page. On the **agent** path, in-scope credentials auto-apply.

### pip auth detail

A `NEXUS_TOKEN` env var does not by itself authenticate pip — pip reads
credentials from the index URL, `~/.netrc`, or its own keyring. So the playbook
composes the authenticated index URL from the injected vars (e.g.
`https://$NEXUS_USERNAME:$NEXUS_TOKEN@nexus/.../simple` set as `PIP_INDEX_URL`
on the install task), as the bundled `deploy.yml` illustrates. Choosing this
mechanism up front is the one integration detail to settle.

### If a file-based secret is unavoidable

Use **Ansible Vault** (encrypted), not a plain `.env` — and even then the vault
password should arrive via an injected env var (e.g. an
`ANSIBLE_VAULT_PASSWORD_FILE` pointing at a tiny script that echoes it), not be
packaged.

## How the console drives it

- **Forms (humans):** pick the host (or group), fill `package`/`version`, select
  the credential(s); the run dialog gates on the runnable's `confirm` autonomy.
- **Host-group fan-out:** target a host **group** and the console runs
  `run-playbook` on every host in the group **in parallel behind one grouped
  confirm** (most-restrictive autonomy wins), each host executing its own local
  playbook. One host list, fanned out — no Ansible-side parallelism needed.
- **Unattended:** a **schedule** with `action = "runnable"` runs `run-playbook`
  with no operator present — *scheduling is the authorization* — fully audited,
  honouring the working-hours rota.
- **Audit:** each host writes a per-run audit record (the runnable declares
  `[config.logging]`), and the JSON result carries `status`/`rc`/`changed`/
  `stats` — more granular than a CI deployment log, and consistent with the
  console's history/analytics.

## Relationship to `runspec emit --ansible`

The planned `runspec emit --ansible` (see `emit-ansible-rundeck.md`) goes the
**opposite** direction: it generates Ansible *modules* from `runspec.toml` for
teams migrating **to** Ansible Automation Platform. `runspec-playbook` moves the
other way — away from Ansible-as-orchestrator toward **console-as-orchestrator**,
with Ansible as the local execution engine. Both coexist because the runnable
stays the source of truth either way.

## Packaging and reuse

`runspec-playbook` in this repo is a **reference example** of the documented
corporate-wrapper pattern. To adopt it, a team:

1. Replaces `runspec_playbook/playbooks/deploy.yml` with their existing per-host
   playbooks (keeping `hosts: all` — the inventory is `localhost`), and vendors
   any required collections under a `collections/` dir so an install needs **no
   Galaxy/PyPI reachable at run time** (matching the project's air-gap ethos).
2. Renames the package and publishes it to their internal index from the
   **existing GitLab CI** — CI keeps building and publishing; the console just
   deploys.
3. Installs it (plus `ansible-core`) into the same venv as runspec on each host
   — e.g. via the console's own `install-into-venv` runnable, so the console
   bootstraps its own deployment tooling — and `run-playbook` then surfaces in
   the console alongside the rest of the fleet's runnables.

## Status

Shipped as an example package: `packages/python/runspec-playbook` (one runnable,
`run-playbook`; `ansible-runner` + `ansible-core` runtime deps; tests stub
Ansible so they run without it). Not wired into the console's own dependency set
— it is meant to be copied into a private package, per the pattern above.
