# runspec-playbook Changelog

## [0.2.0] — 2026-11-07

### Added

- **Run a playbook from a git repo the console sends.** `--repo <name>` names a
  repo in the runspec-console's Repos tab (console 0.389.0+) and `--playbook`
  becomes a path inside it (`site.yml`, `web/patch.yml`). The console sends the
  checkout with the run: `RUNSPEC_PLAYBOOK_SOURCE = stdin:<length>:<sha256>`
  in the env frame, then the payload on stdin (`source.py`). The repo is checked
  against its content hash, written to the run's temporary folder and removed
  afterwards; the header never reaches Ansible's environment. The result adds
  `repo`, `commit` and `dirty`.
- The one-host inventory uses the console's name for the host and puts it in
  its console group and profiles, so `hosts: <group>`, `group_vars/` and
  `host_vars/` work.

### Changed

- `package` and `version` are optional (only the bundled `deploy` playbook
  needs them, and it says so). `--playbook` is a plain string, no longer a
  choice of bundled names.
- `--repo` without a sent repo, a sent repo with another name, or a playbook
  path that isn't in the repo exits 2 with a plain reason.

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
