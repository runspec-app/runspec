# runspec-linux-core Changelog

## [0.3.0] — 2026-06-20

Add `enable_passwordless_sudo()` (new `sudoers.py`) — the one-time bootstrap that
makes the `run_as = "root"` package/power runnables work without a password.

- Installs a validated drop-in at `/etc/sudoers.d/runspec`. Two scopes:
  `"scoped"` → `NOPASSWD` for the runspec venv's runnable scripts only
  (`{venv_bin}/`); `"all"` → `NOPASSWD: ALL`.
- The sudo password is read from **stdin via `sudo -S`** — never on argv, in the
  process table, shell history, or the audit log.
- Validates with `visudo -cf` before install *and* `visudo -c` after; rolls the
  drop-in back if the post-install check fails, so a typo can't lock sudo out.
- The target user name is validated against a strict pattern before it reaches
  the file (no rule injection); idempotent when an identical drop-in exists.
- Raises `ValueError` (bad scope/user), `ToolNotFoundError` (no `sudo`/`visudo`),
  or `CommandError` (auth failure / validation failure).

Note: the console's *agent* path wraps the command in `env(1)`, so a `"scoped"`
grant covers manual/CLI runs; agent-driven runs need `"all"`.

## [0.2.0] — 2026-06-20

Add package-manager and host-power helpers so a fleet can be patched and
rebooted through `runspec-linux`.

New in `packages.py` — detect the distro's manager (`apt-get`/`dnf`/`yum`/
`zypper`/`pacman`) and operate over it:

- `detect_manager()` — first supported manager on PATH (raises
  `ToolNotFoundError` if none).
- `list_packages(name_filter=None, limit=100)` — installed packages
  (name + version), filtered + sorted + capped; `total` is the pre-cap count.
- `list_upgrades(refresh=True)` — packages with an available upgrade
  (name/current/candidate). `refresh` updates the index first (needs root);
  `refresh=False` reads the cache as any user.
- `upgrade_packages()` — refresh the index and upgrade everything (needs root).
- `install_package(packages)` / `remove_package(packages)` — install/remove one
  or more space-separated packages (needs root).

New in `power.py`:

- `reboot_host(delay_minutes=0)` — reboot now or schedule with `shutdown -r +N`,
  falling back to `systemctl reboot` for an immediate reboot.

Each helper returns plain data and raises `ToolNotFoundError` (no manager/tool)
or `CommandError` (non-zero exit, e.g. run unprivileged), matching the existing
contract. Package names are validated against a conservative token shape before
reaching argv (rejects leading dashes and shell metacharacters), raising
`ValueError` on a bad name. The write commands run non-interactively
(`-y`/`--non-interactive`/`--noconfirm`, `DEBIAN_FRONTEND=noninteractive`) and
return a bounded tail of their output.

## [0.1.0] — 2026-06-09

Initial release.

The pure-Python logic core extracted from `runspec-linux`. Provides the
system-admin helper functions as plain importable functions — **no dependency on
`runspec`, no `runspec.toml`, no console-script entry points** — so a package can
`from runspec_linux_core import nc_send` (and the rest) without surfacing any
runnables in the venv or in `runspec local` / `runspec serve` discovery.

Each function returns plain data and *raises* on failure (`ToolNotFoundError`
when a required system tool is absent, `CommandError` when a subprocess reports
a non-zero exit); the runnable wrappers in `runspec-linux` catch these and render
the JSON/exit behaviour.

Exports:

- **System** — `system_info`, `disk_usage`, `check_memory`, `list_processes`
- **Services** — `check_service`, `list_services`, `restart_service`
- **Logs** — `tail_log`, `search_log`, `journalctl`
- **Network** — `ping_host`, `check_port`, `show_connections`
- **Files** — `find_large_files`, `backup_files`
- **Security** — `last_logins`, `who`
- **Containers** — `list_containers`, `container_logs`, `restart_container`
- **TCP** — `nc_send`
- **Errors** — `LinuxCoreError`, `ToolNotFoundError`, `CommandError`
