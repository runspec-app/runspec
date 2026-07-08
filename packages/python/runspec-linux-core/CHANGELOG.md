# runspec-linux-core Changelog

## [0.3.5] — 2026-07-08

Apply the same umask-neutralising treatment to `install_into_venv` that `0.3.4`
added to `create_venv`. After the `pip install`, the venv tree is now
normalised with `chmod -R u=rwX,go=rX <venv>` so pip's new directories are
`755` regardless of the caller's umask.

The trigger was a `sudo pip install --upgrade runspec-linux` under a host
where sudo/PAM had switched to a `027` login umask: pip's new
`site-packages/runspec_linux/` and `site-packages/runspec_linux_core/`
directories landed at `750`, and `runspec local` (walking `.dist-info` via
`importlib.metadata`) stopped seeing the packs for any user but the owner.
The equivalent bug for the venv root was fixed at build time in `0.3.4`; this
release closes the same gap at upgrade time.

If `<venv>/logs` exists (created by `create_venv`), its `2775` setgid mode is
restored after the wide chmod — the `test -d` guard skips the restore for a
venv built some other way. When `run_as` is set the new steps escalate too.

## [0.3.4] — 2026-07-07

Fix `create_venv` producing an unreadable `750` venv directory when the caller's
umask is `027` (typical under `sudo -n -u <svc>` with a PAM-set login umask).
`python -m venv` honours the current umask, so a shared service-account venv
was landing owner-only instead of the `755` a stock `python -m venv <dest>`
produces under the default `022` umask, locking every non-owner console out —
contrary to the "shared between several consoles" use case the helper is built
for.

After the venv-creation step, `create_venv` now runs
`chmod -R u=rwX,go=rX <dest>` to normalise the tree to `755` on directories and
`644` on files while preserving the `+x` on real executables in `bin/` (capital
`X` only sets execute on directories and files that already have some execute
bit). The subsequent `chmod 2775 <dest>/logs` still gets the last word on the
logs directory, and `chgrp -R <group>` (when passed) still runs. When `run_as`
is set the new chmod runs as that user too.

## [0.3.3] — 2026-06-22

Add `commands.py` — the pure logic behind the new `runspec-linux` `which`
runnable:

- **`which(names, *, all_matches=False, path=None)`** — locate executables on the
  search PATH, like the `which` command. `names` is a single command or a list
  (a string is split on whitespace, so `"python3 pip3"` resolves both).
  `all_matches` returns every match across the PATH (not just the first that
  would run); `path` overrides the search path (defaults to `$PATH`, falling back
  to `os.defpath`). A name containing a path separator is checked directly rather
  than searched. Returns `{path, all_found, results: [{name, found, path,
  paths}]}` and raises `ValueError` when no name is given.

## [0.3.2] — 2026-06-21

Add `venvs.py` — the pure logic behind three new `runspec-linux` runnables for
Python virtualenv management:

- **`create_venv(python, dest, *, run_as=None, become_method="sudo", group=None,
  system_site_packages=False, upgrade_deps=False, clear=False, prompt=None)`** —
  build a venv at `dest` from a *specific* interpreter (no system default
  fallback; `python` must be an executable file), always with `--symlinks`. Then
  creates `<dest>/logs` mode `2775` (setgid, so audit logs inherit the group) and
  optionally `chgrp -R`s the venv to a shared `group` — the shape a service
  account needs for a venv shared between several consoles.
- **`install_into_venv(venv, packages="", *, project=None, editable=False,
  upgrade=False, no_cache_dir=False, force_reinstall=False, run_as=None,
  become_method="sudo")`** — `pip install` named packages and/or a local `project`
  (a `pyproject.toml` directory or a wheel/sdist; `editable` for `-e`) into the
  venv's bundled python. Package names reuse the shared injection guard.
- **`configure_pip(index_url, *, extra_index_url=None, trusted_host=None,
  cert=None, timeout=None, retries=None, proxy=None, require_virtualenv=False,
  scope="user", venv=None, run_as=None, become_method="sudo")`** — write a
  validated `pip.conf` (`[global]`) at user (`~/.config/pip/pip.conf`) or venv
  (`<venv>/pip.conf`) scope for private-PyPI access.

All three take a **runtime** `run_as` user (the service account is chosen
per-invocation, so escalation is internal — `sudo -n -u <user>` or, via
`become_method`, `su` — not a declarative `run_as`) and raise `ValueError` on bad
input / `CommandError` on a failed step.

## [0.3.1] — 2026-06-20

Fix `enable_passwordless_sudo` failing with "visudo not installed" (and the same
latent bug in `reboot_host`'s `shutdown` lookup). The console runs runnables over
a non-login, non-interactive SSH shell whose `PATH` usually omits `/usr/sbin` and
`/sbin`, so `shutil.which()` couldn't find these sbin-resident admin tools even
when installed.

New `_paths.which()` searches `PATH` first, then the standard sbin/bin dirs
(`/usr/local/sbin`, `/usr/sbin`, `/sbin`, `/usr/local/bin`, `/usr/bin`, `/bin`),
and returns an absolute path. `sudoers.py` and `power.py` resolve and invoke
`visudo` / `shutdown` / `systemctl` through it, so they work regardless of `PATH`.

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
