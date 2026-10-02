# runspec-playbook

Run **bundled Ansible playbooks locally** on a host the runspec-console is
already connected to — no second SSH stack, one host list.

```
runspec-console ──SSH──▶ host ──▶ run-playbook ──▶ ansible-runner (connection: local) ──▶ your playbook
        (runspec_hosts.toml)                                          (acts on localhost — no SSH)
```

This is the `ansible-pull` pattern, driven by the console instead of cron. The
console reaches each host over its own SSH connection and runs `run-playbook`
there; Ansible then executes the playbook against `localhost`, so it opens no
SSH of its own. Everything the console already owns per host keeps applying —
connection, credentials, denylist, audit — while Ansible supplies the playbook
richness (idempotent modules, handlers, templates, `become:`, `--check`).

> **When this fits:** your playbooks are **per-host** (provisioning/config of
> the box they run on). Local execution means each host only knows about
> itself, so anything that coordinates *across* hosts — `delegate_to`,
> `run_once`, reading another host's `hostvars`, `serial:` rolling deploys —
> needs a real Ansible control node and is out of scope here.

**Deployment is the worked example, not the limit.** The mechanism runs *any*
per-host playbook — patching, config enforcement, an audit/report pass, a
restart — and the shipped `run-playbook` runnable is just the deploy-shaped one
(package + version). Add a `patch.yml` / `audit.yml` under `playbooks/` and a
matching runnable (or a generic playbook-name + extra-vars runnable) the same
way; see "Making it your own" below.

## The runnable

| Arg | | Description |
|---|---|---|
| `package` | positional 1 | Package to deploy; supports extras, e.g. `myapp` or `myapp[postgres,redis]`. |
| `version` | positional 2 | Exact version; composed into `<package>==<version>`. |
| `--playbook` | choice | Which bundled playbook to run (default `deploy`). |
| `--check` / `-C` | flag | Dry run — Ansible check mode (report, don't apply). |
| `--extra-var k=v` | repeatable | Extra Ansible variables, merged after the `deploy_*` vars. |
| `--tags` / `--skip-tags` | str | Comma-separated Ansible tag filters. |
| `--verbosity` / `-v` | int 0–4 | Ansible verbosity. |

```bash
run-playbook myapp 1.4.2
run-playbook 'myapp[postgres]' 1.4.2 --check
run-playbook myapp 1.4.2 --extra-var venv_path=/opt/myapp/venv -v
```

The runnable always passes these variables to the playbook: `deploy_package`,
`deploy_version`, `deploy_package_spec`. It returns JSON
(`status`, `rc`, `changed`, `stats`) — and follows the Ansible `changed`
convention so it reads cleanly in the console history/analytics.

## Secrets

**Do not package a `.env` of secrets.** A wheel/sdist on your index is readable
by anyone with read access, package data is not encrypted, and it would land on
every host's disk in `site-packages`. Bake only **non-secret** config (an index
host, a default venv path) into a playbook variable.

Instead use the console's existing **Credentials → env var** channel. Secrets
live in the OS keychain (Windows Credential Manager / macOS Keychain / Secret
Service); only metadata is stored on disk. At run time the console injects them
as plain environment variables derived from the credential's label + kind, and
this runnable forwards the environment to the playbook. Read them in tasks with
`lookup('env', 'NAME')`.

Name the credential so the convention yields the names your playbook expects:

| Credential label | Kind | Env vars injected |
|---|---|---|
| `Nexus` | token | `NEXUS_TOKEN`, `NEXUS_PAT` |
| `Nexus` | userpass | `NEXUS_USERNAME`, `NEXUS_USER`, `NEXUS_PASSWORD` |
| `Deploy Key` | token | `DEPLOY_KEY_TOKEN`, `DEPLOY_KEY_PAT` |

Select one or more credentials in the run dialog (the **Forms** tab) — these are
the GitLab CI/CD variables you have today, now sourced from the keychain instead
of a CI settings page. For an unattended run, a **schedule** with
`action = "runnable"` runs `run-playbook` with no operator present (scheduling
*is* the authorization); in-scope credentials auto-apply on the agent path.

> A file-based secret is only acceptable if it is **Ansible Vault** (encrypted)
> — and the vault password itself should still arrive via an injected env var
> (e.g. `ANSIBLE_VAULT_PASSWORD_FILE` pointing at a tiny script that echoes it),
> not be packaged.

## Making it your own

This package is a reference. To adopt it:

1. Replace `runspec_playbook/playbooks/deploy.yml` with your existing per-host
   playbooks (keep `hosts: all` — the inventory is `localhost`). Vendor any
   required collections under a `collections/` dir for an index-only / air-gapped
   install rather than fetching from Galaxy at run time.
2. Rename the package and publish it to your internal index from your existing
   GitLab CI — CI keeps building and publishing; the console just deploys.
3. Install it (and `ansible-core`) into the same venv as runspec on each host
   (e.g. from the console's Marketplace tab), so `run-playbook`
   surfaces in the console alongside your other runnables.

See `docs/design/console-ansible-deploy.md` for the full design and rationale.

## Requirements

`runspec`, `ansible-core`, and `ansible-runner` on the host. Zero of these need
to be reachable at run time once installed.
