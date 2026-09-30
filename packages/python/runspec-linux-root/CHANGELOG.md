# Changelog

All notable changes to `runspec-linux-root` are documented here.

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
