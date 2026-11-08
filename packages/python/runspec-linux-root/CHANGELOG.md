# Changelog

All notable changes to `runspec-linux-root` are documented here.

## [0.3.1] — 2026-11-08

### Changed

- **`config-file allow` / `disallow` take several patterns at once**,
  comma- or space-separated (`config-file allow '/etc/alloy/*,/etc/default/alloy'`)
  or as a repeated `--pattern`. All are checked first, so one bad pattern adds
  none; the output lists what was added or removed, and the audit event names
  every pattern. Needs runspec-linux-core 0.17.0.

## [0.3.0] — 2026-11-08

### Added

- **`config-file`** (`allowed` / `read` / `write` / `edit` / `allow` /
  `disallow`): write config files as root without the full `filesystem` tool
  running as root. It only touches paths matching this venv's root-owned
  allowlist, `runspec_root_files.conf` (one pattern per line, `*` within a
  folder, `**` any depth; no file = nothing allowed). `allow` / `disallow` are
  `manual`, so only a person can widen it; `read`, `write` and `edit` need
  approval, `allowed` is autonomous. The folder is resolved through symlinks
  and `..` before matching, the file can't be a symlink, and accounts,
  sudoers, PAM, the SSH server and root's SSH keys are never written. Writes are
  atomic, back the old file up to `{venv}/root_file_backups/` (last 5 per
  file), keep an existing file's owner, group and mode unless `--owner`,
  `--group` or `--mode` are given, and print a diff (`--dry-run` to preview).
  `edit` needs each old text to appear exactly once. Text may hold
  `{{secret:<credential>}}` placeholders, filled in on the host from
  `RUNSPEC_SECRET_*` (runspec-console sends the credential) and masked in the
  output; a new file holding a secret starts `0600`. Audit events
  `root_file.written` / `root_file.allow` / `root_file.disallow`. Needs
  runspec-linux-core 0.16.0 and runspec-fs-core 0.6.0.

## [0.2.0] — 2026-11-07

### Added

- **`package-repo`** (`list` / `add` / `remove`): add a vendor's package
  repository and its signing key without a shell, the way install guides like
  Grafana Alloy's do it. One call takes the apt pair (`--apt-url`,
  `--apt-key`, `--apt-suite`, `--apt-components`) and the rpm pair
  (`--rpm-url`, `--rpm-key`, for dnf, yum and zypper); each host uses its own,
  so one call covers a mixed group of hosts. apt keys go to
  `/etc/apt/keyrings/<name>.asc` with `signed-by=`; rpm keys to
  `/etc/pki/rpm-gpg/` plus `rpm --import`, with `gpgcheck=1` and
  `repo_gpgcheck=1` (`--unsigned-metadata` turns the second off). The output
  lists every key's fingerprint, `--key-fingerprint` refuses a key file
  holding a key you didn't list, and an `http://` key needs one. `add`
  refreshes just that repository and undoes everything if the refresh fails;
  running it again with the same values changes nothing. `--replace`
  overwrites a same-named repository set up by hand; `--proxy` and
  `--ca-bundle` apply to the key download. `list` is autonomous, `add` and
  `remove` need approval. pacman hosts are refused.
- **`system-service`** (`start` / `stop` / `restart` / `reload` / `enable` /
  `disable`, `--now` with enable/disable): systemd service control as root,
  for system services such as `alloy` (runspec-linux's `restart-service` never
  runs as root). A failure includes the unit's last journal lines.
- Needs `runspec-linux-core >= 0.15.0`.

## [0.1.1] — 2026-10-01

- `enable-passwordless-sudo --scope scoped`: the help text and README now say
  the scoped grant also covers runspec-console and MCP-gateway runs, from
  runspec 0.70.0 (their env prelude runs on the venv's `bin/python`).
  `--scope all` is no longer needed for those. No code change.

## [0.1.0] — 2026-09-30

First release. The Linux tools that need root, split out of `runspec-linux`
(0.19.0) so that package never runs anything as root:
`list-upgrades`, `upgrade-packages`, `install-package`, `remove-package`,
`autoremove-packages`, `set-sysctl`, `reboot-host` and
`enable-passwordless-sudo`.

- Every tool declares `run_as = "root"` (except `enable-passwordless-sudo`,
  which runs as the invoker and escalates with the password it's given).
  `set-sysctl` loses its `--run-as`/`--sudo`/`--become-method` args: it always
  runs as root.
- Meant for a root-owned venv, so only root can change code that root runs.
- `enable-passwordless-sudo`'s `scoped` option is documented honestly: it covers
  direct `sudo <tool>` runs only, not runs from runspec-console or the MCP
  gateway.
- The helpers stay in `runspec-linux-core` (>= 0.13.0).
