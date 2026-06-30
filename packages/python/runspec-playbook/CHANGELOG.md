# runspec-playbook Changelog

## [0.1.0] — 2026-06-30

Initial release.

One runnable, **`run-playbook`** (confirm), that deploys a Python package by
running a **bundled Ansible playbook locally** on the target host:

- The runspec-console reaches the host over its own SSH connection (the host
  from `runspec_hosts.toml`) and invokes `run-playbook` there. The playbook runs
  against `localhost` (`ansible_connection=local`), so **Ansible uses no SSH of
  its own** — the `ansible-pull` pattern, driven by the console instead of cron.
- Arguments: positional `package` (supports `name[extras]`) and `version`
  (composed into `name[extras]==version`), a `playbook` choice over the bundled
  playbooks, `--check` (Ansible check-mode dry run), repeatable `--extra-var
  key=value`, `--tags`/`--skip-tags`, and `-v/--verbosity`.
- Secrets (Nexus credentials, etc.) are passed as **environment variables**
  injected by the console from the OS keychain — never packaged, never on the
  command line, never in the audit log. The playbook reads them with
  `lookup('env', 'NAME')`.
- Driven by `ansible-runner` (the programmatic runner AWX/AAP use): returns
  structured JSON (`status`, `rc`, `changed`, per-result `stats`) into the
  console audit record.

Built to be copied into a private corporate package: depend on it (or vendor
the wrapper), drop your own per-host playbooks under `runspec_playbook/playbooks/`,
and publish to your internal index. See `docs/design/console-ansible-deploy.md`.
