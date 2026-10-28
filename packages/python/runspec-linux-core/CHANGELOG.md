# runspec-linux-core Changelog

## [0.14.1] — 2026-10-28

### Fixed

- **runspec-linux 0.19.0 and older load again.** They import `create_venv`,
  `install_into_venv`, `configure_pip` and `share_venv_logs` from this package
  at start-up, so upgrading runspec-linux-core to 0.14.0 on its own (for
  example by installing runspec-linux-root, which only asks for >= 0.13.0)
  broke every runspec-linux runnable with `cannot import name
  'configure_pip'`. The four names are back as stand-ins: importing works,
  calling one raises `LinuxCoreError` saying venv provisioning moved to
  runspec-console. They are not in `__all__`. Upgrading runspec-linux to
  0.20.0 is still the real fix.

## [0.14.0] — 2026-10-02

### Removed

- The `venvs` module: `create_venv`, `install_into_venv`, `configure_pip` and
  `share_venv_logs`. Venv provisioning now lives in runspec-console
  (`runspec_console.provision`, 0.312.0); on the command line use
  `python -m venv`, `pip install` and `runspec logs share --inbox`.
  runspec-linux 0.19.0 still imports these helpers, so upgrade to
  runspec-linux 0.20.0 at the same time.

## [0.13.1] — 2026-10-01

- `sudoers` docstring: the *scoped* grant (`{venv_bin}/`) now also covers
  console and MCP-gateway runs. From runspec 0.70.0 their env prelude runs on
  the venv's `bin/python`, which the grant matches. No code change.

## [0.13.0] — 2026-09-30

### Changed

- **`build_become` does nothing when the target is the account the process
  already runs as** (compared by uid; new `become.is_current_user`). A service
  account running its own venv's tools with `--run-as <itself>` used to get
  `sudo -n -u <itself> …`, which fails without a sudoers rule nobody has. This
  applies to every `run_as` helper: logs, services, containers, investigate,
  venvs and cron.
- `sudoers.py`: the `scoped` note now says it covers direct
  `sudo <venv>/bin/<tool>` runs only. The console and the MCP gateway run tools
  through the env-frame `sh` prelude (runspec 0.67.0), which a scoped rule
  doesn't match. The old note described `env(1)`, which runspec no longer uses.
- The root-only helpers (packages, `reboot_host`, `set_sysctl`,
  `enable_passwordless_sudo`) stay here. Their runnables moved to the new
  `runspec-linux-root` package.

## [0.12.0] — 2026-09-29

### Added

- **The logs inbox** (runspec >= 0.66, `docs/design/venv-logs-inbox.md`).
  - `create_venv(..., inbox=True)` (the default) creates `<venv>/logs/inbox`
    as the venv owner: `mkdir -m 0700`, then `chmod 1777`. Every other
    account's runs (operators, root) land there and reach the venv's history
    with no group admin.
  - With no `group`, `logs/` itself is now owner-only (`0755`); with a `group`
    it's still `2775`.
  - `inbox=False` keeps the pre-inbox layout (`logs/` `2775`) and writes a
    `logs/.no-inbox` opt-out marker.
  - An inbox path that's a symlink is refused.
- **`share_venv_logs(..., inbox=True)`** (the default) sets up the inbox on an
  existing venv. It changes `logs/`'s own mode only when `group` is given (the
  group share) or `inbox=False` (the old behaviour).

### Changed

- **`install_into_venv` no longer forces `logs/` back to `2775`.** The
  permission sweep already skips `logs/`, and the forced restore undid an
  owner-only `logs/` + inbox layout. Result dicts gain `inbox` (and
  `logs_mode` for `create_venv`); `share_venv_logs`'s `mode` is `""` when
  `logs/` was left alone.

## [0.11.0] — 2026-09-26

### Added

- **`list_packages(..., reason=True)`** adds `installed` to each row:
  `"manual"` (installed by name) or `"auto"` (pulled in as a dependency). That
  flag is what autoremove goes by, so a manual package is never autoremoved
  however old it is. Read from `apt-mark showmanual`, `pacman -Qqe`, `dnf
  repoquery --userinstalled` or `zypper packages --userinstalled`; `null` on
  yum or when that query fails. Off by default, so a plain listing still runs
  one query.
- **`list_autoremovable(purge=False)`** lists what `autoremove_packages` would
  remove without removing anything, and needs no root. Refused on yum, which
  can't list candidates without removing them.

## [0.10.0] — 2026-09-26

### Added

- **`autoremove_packages(purge=False, dry_run=False)`** removes packages
  nothing depends on any more: `apt-get autoremove` (with `--purge`), dnf/yum
  `autoremove`, zypper's `--unneeded` packages, or pacman orphans (`-Qdt`). It
  lists the candidates first and returns them as `packages` (name + version),
  so a caller sees exactly what went; `dry_run` stops after the list, and
  nothing runs when the list is empty. On Debian/Ubuntu apt keeps the running
  kernel and the newest ones, which makes this the safe way to clear old
  kernels out of a full `/boot`. yum can't list candidates without removing
  them, so `dry_run` is refused there and a real run returns `packages: null`.
- **`remove_package(packages, purge=False)`**: `purge` also deletes the
  packages' configuration files (`apt-get purge`, `pacman -Rn`). dnf, yum and
  zypper have no separate purge mode, so there it stays a plain remove. The
  result gains a `purged` field saying whether purge applied.

## [0.9.0] — 2026-09-26

### Added

- **`open_ports` flags owners it couldn't see.** `ss -p` only reports other
  users' processes to root, so an unprivileged probe returned most listeners
  with an empty `processes` list, which is easy to misread as "nothing owns this
  port". When the probe didn't run as root and any owner is missing, the
  result now carries a `hint` ("N of M listeners have no owning process …
  Re-run with --sudo"). No hint as root, via `run_as="root"`, or when every
  owner is known.
- **Docker socket permission errors name the fix.** `list_containers`,
  `container_logs` and `restart_container` turn an unescalated "permission
  denied … docker.sock" into a `CommandError` that says to re-run with
  `--sudo`, use `--run-as` with a docker-group user, or add the user to the
  docker group. `container_logs` now raises on that error instead of returning
  the daemon's refusal as log lines. Escalated failures keep the existing
  passwordless-sudo message.

## [0.8.0] — 2026-09-26

### Added

- **`block_devices(include_loop=False)`** — the disk inventory behind the new
  `block-devices` runnable. Runs `lsblk --json` and returns a tree: each disk
  with its partitions, LVM volumes and crypt/RAID layers under `children`, with
  size, `rotational` (the kernel's SSD-vs-spinning flag), transport, model,
  serial, filesystem, label, UUID and mount points. Normalises both the typed JSON
  of util-linux >= 2.33 and the all-strings output of older versions, and falls
  back from the `MOUNTPOINTS` column (>= 2.37) to `MOUNTPOINT`. Loop devices are
  hidden unless `include_loop`.
- **`network_interfaces(physical_only=False)`** — the NIC inventory behind the new
  `network-interfaces` runnable, from `ip -json address` plus `/sys/class/net`:
  state, MAC, MTU, bridge/bond `master`, and IPv4/IPv6 addresses (`dynamic` for
  DHCP/SLAAC), plus link speed and driver for physical cards. `physical` marks an
  interface backed by hardware; `physical_only` drops loopback, bridges, bonds,
  VLANs, veth pairs and tunnels, which clutter a container or Kubernetes host.
- **`find_large_files_report(search_path, min_mb, limit, *, cross_mounts=False)`**
  — `find_large_files` as a report: `{path, min_mb, files, match_count,
  skipped_mounts?}`, where `match_count` counts matches before `limit`.

### Changed

- **`find_large_files` stays on one filesystem.** It recursed across every mount
  below `search_path`, so a scan of `/` walked `/proc` and ranked `/proc/kcore`
  (a ~128 TiB virtual file) as the largest file on the host. It now behaves like
  `find -xdev`. `cross_mounts=True` opts back into other mounted filesystems (a
  separate disk, a network share), but kernel virtual filesystems (`proc`,
  `sysfs`, `devtmpfs`, `cgroup2` …) are never scanned. They are recognised from
  `/proc/self/mountinfo`, then `/proc/mounts`, then the `/proc` / `/sys` / `/dev`
  paths. The guard is `runspec_linux_core._mounts`, a byte-for-byte twin of
  `runspec_fs_core._mounts` (each core stays dependency-free) kept identical by a
  parity test.
- **A bad `search_path` is an error.** A missing path raises `FileNotFoundError`
  and a file raises `NotADirectoryError`; both used to return an empty list,
  indistinguishable from "no large files". A path on a virtual filesystem raises
  `ValueError`.

## [0.7.0] — 2026-09-25

### Added

- **`share_venv_logs(venv, *, group=None, run_as=None, become_method="sudo")`** —
  the repair path for a venv **not** built by `create-venv --group` (hand-made or
  legacy). Sets `<venv>/logs` to `2775` (setgid + group-writable) so new files —
  including the per-venv runs index `runspec_runs.db` and the audit-log files —
  inherit the directory's group and can be written across a shared console group,
  and, with `group`, `chgrp`s the `logs/` **directory** to it. The chgrp is on the
  directory only (not `-R`): `logs/` already holds per-run files owned by *other*
  users (a `run_as="root"` runnable leaves root-owned logs there) which a non-root
  caller can't chgrp, and the setgid bit means only new files need the group. This
  is exactly what `create_venv` does at build time, applied to an existing venv
  without recreating it. Raises `ValueError` for a bad group name or a `<venv>`
  that isn't a directory, `CommandError` on a failed step. Backs the new
  `share-venv-logs` runnable in `runspec-linux`.

### Fixed

- **`install_into_venv` regression (0.6.1): `chmod: … Operation not permitted` on
  a `--symlinks` venv's `bin/python*`.** 0.6.1 replaced the `chmod -R` normalise
  sweep with `find … -exec chmod {} +` to prune the `logs/` dir. But `find -exec
  chmod` **dereferences a symlink argument**, and a venv built with `--symlinks`
  (what `create_venv` always does) has `bin/python`, `bin/python3`,
  `bin/python3.X` pointing at the **root-owned system interpreter outside the
  venv** — so the sweep tried to `chmod /usr/bin/python3.X` and failed for a
  non-root caller (or, under sudo, would have silently repermissioned the system
  Python). `chmod -R` skipped symlinks for free; the `find` sweep now prunes them
  explicitly too (`-type l -prune`), matching the old behaviour. A symlink's own
  mode is unused on Linux, so nothing is lost. The `logs/` prune from 0.6.1 is
  unchanged. No API change.

## [0.6.1] — 2026-09-17

### Fixed

- **`install_into_venv` no longer fails with `chmod: … Operation not permitted`
  on a venv with root-owned logs.** The post-install permission-normalisation
  step ran a blanket `chmod -R u=rwX,go=rX <venv>` over the *whole* tree —
  including `<venv>/logs`, the group-shared (`2775`) audit directory. A
  `run_as="root"` runnable (`list-upgrades` / `upgrade-packages`) leaves
  **root-owned** per-run log files there, so a non-root caller's `chmod` hit them
  with "Operation not permitted" and the install reported failure. The sweep now
  uses `find <venv> -path <venv>/logs -prune -o -exec chmod u=rwX,go=rX {} +`,
  which skips the `logs` subtree entirely (its `2775` mode is left intact and
  still restored afterwards as a guard). Site-packages normalisation is
  unchanged. No API change.

## [0.6.0] — 2026-08-24

New `runspec_linux_core.cron` module — pure helpers for managing a user's
crontab: `list_cron`, `add_cron`, `remove_cron`, `clear_cron`, plus the
`validate_schedule` shape check. Each takes the same keyword-only `run_as` /
`become_method` escalation as the container/venv helpers and wraps its
`crontab` subprocess with `build_become`, so "run as `bob`" reads/writes
`bob`'s own crontab (`sudo -n -u bob crontab -l` / `crontab -`); with no
`run_as` the caller's crontab is used. Editing is a read-modify-write (crontab
has no append/delete verbs): the whole new table is piped to `crontab -`.
`list_cron` returns structured job records (schedule, command, an attached
`# comment`, `special` for `@nickname` schedules) plus environment
assignments; a user with no crontab is reported `exists: false`, not an error.
`add_cron` is idempotent (an identical `schedule command` line is left as-is
unless `allow_duplicate`); `remove_cron` selects by command `match` or job
`index` and drops a preceding `# comment` line with the job. Schedules,
commands, and comments are guarded against newline injection. Failures raise
`ToolNotFoundError` (no `crontab`), `CommandError` (non-zero subprocess, with
the passwordless-sudo hint), or `ValueError` (bad input).


## [0.5.0] — 2026-08-07

The Docker container helpers now take the same per-invocation `run_as` /
`become_method` escalation as the log/service helpers, since the Docker daemon
socket is typically root- or `docker`-group-owned. `list_containers`,
`container_logs`, and `restart_container` gain keyword-only `run_as` /
`become_method` params and wrap their `docker` subprocess with `build_become`;
a failed escalated command raises `CommandError` with the passwordless-sudo hint
(`container_logs` no longer returns the sudo error as log lines when `run_as` is
set). No behaviour change when `run_as` is unset.


## [0.4.0] — 2026-08-01

New shared privilege-escalation module `runspec_linux_core.become` — the single
source of truth for runspec-linux's per-invocation `run_as` escalation
(mechanism B). It exposes `build_become(argv, run_as, method)` (`sudo -n -u` /
`su -c`), `validate_user`, `check_become_error` (the passwordless-sudo hint),
`escalation_supported`, `current_user`, and `BecomeError` (a `CommandError`).
`venvs.py` now delegates to it instead of its own private `_become`/`_check`
copy — byte-identical command output, no behaviour change beyond invalid
run-as/method now raising `BecomeError` (a `CommandError`) rather than a bare
`ValueError`. This module also backs the new `--run-as` / `--sudo` mutually
exclusive args on the privileged `runspec-linux` runnables.

New system-admin helpers for corporate app-support and performance work:

- `perf.py` — `get_sysctl` / `set_sysctl` (read/tune kernel params, optional
  `/etc/sysctl.d` persist), `cpu_info` (governor/frequency/topology),
  `pressure` (PSI `/proc/pressure/*`).
- `investigate.py` — `service_detail` (a unit's ExecStart/env/paths/state),
  `open_ports` (listening port → pid → process), `owning_package` (which
  package owns a path, dpkg/rpm), `failed_units` (`systemctl --failed`).
- `network.py` additions — `trace_route`, `route_table`, `socket_stats`,
  `dns_config`.

The privileged helpers (`set_sysctl` persist, `service_detail`, `open_ports`,
and the log/service helpers) take `run_as` / `become_method` params and wrap
their subprocesses with `build_become`, so a caller can run them as root or a
specific service account.

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
