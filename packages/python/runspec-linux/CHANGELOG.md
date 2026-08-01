# runspec-linux Changelog

## [0.7.0] — 2026-08-01

Enhancements for corporate application support, devops, and performance work.

### Added — filesystem (folded from runspec-fs)

- The **`fs`** runnable is now folded into the Linux pack (backed by the
  dependency-free `runspec-fs-core`): one verb per Filesystem MCP tool
  (`read-text-file` … `move-file`), reads `autonomous` and mutations `confirm`.
  `runspec serve` flattens the verbs into leaf `fs_<verb>` MCP tools, so a
  runspec-console agent gets local **and** remote filesystem access on every
  fleet host through the OS pack it already installs — no separate `runspec-fs`
  needed. Per-invocation `--run-as <user>` / `--become-method` are inherited on
  every verb (POSIX escalation, no directory jail).

### Added — new runnables

- **Perf tuning & analysis:** `get-sysctl`, `set-sysctl` (a privileged write —
  `confirm`, optional `--persist` to `/etc/sysctl.d`), `cpu-info` (governor /
  frequency / topology), `pressure` (kernel PSI stats).
- **App discovery & investigation:** `service-detail` (a unit's ExecStart binary,
  working dir, user, env files, unit-file path, state), `open-ports` (listening
  port → pid → process), `owning-package` (which package owns a path, dpkg/rpm),
  `failed-units`.
- **Network deep-diagnostics:** `trace-route`, `route-table`, `socket-stats`,
  `dns-config`.

### Added — `--run-as` / `--sudo` on the privileged runnables

- `restart-service`, `tail-log`, `search-log`, `journalctl`, `set-sysctl`,
  `service-detail`, and `open-ports` now take a mutually-exclusive
  **`--run-as <user>`** / **`--sudo`** pair (plus `--become-method sudo|su`) so
  they can run against root-owned / service-account-owned resources.
  Escalation is internal (`sudo -n -u` / `su`), governed by an `exclusive` group
  so the two flags can't be combined.

### Changed

- Requires `runspec >= 0.44.0` (leaf-tool inherited globals via `runspec serve`)
  and `runspec-linux-core >= 0.4.0`; adds a `runspec-fs-core >= 0.2.0` dependency.

## [0.6.2] — 2026-07-08

Pull in `runspec-linux-core >= 0.3.5` so `install-into-venv` now normalises the
venv tree to `755`-on-dirs / `644`-on-files after `pip install`, matching what
`create-venv` already does at build time. Fixes upgrades under `sudo` with a
`027` login umask landing `site-packages/runspec_linux/` at `750` and hiding
the runnables from `runspec local` for anyone but the venv owner. See the core
changelog. Bare `sudo pip install --upgrade` (not going through the runnable)
still needs a host-level fix — a manual `sudo chmod -R u=rwX,go=rX <venv>`
one-shot, or a `Defaults umask=0022,umask_override` drop-in under
`/etc/sudoers.d/`.

## [0.6.1] — 2026-07-07

Pull in `runspec-linux-core >= 0.3.4` so `create-venv` normalises the venv tree
to `755`-on-dirs / `644`-on-files after creation. Previously, under
`sudo -n -u <svc>` with a PAM-set login umask of `027`, the venv directory
landed at `750` and locked other consoles out — contrary to the shared-service-
account venv the runnable is built for. See the core changelog.

## [0.6.0] — 2026-06-22

Add a **`which`** runnable — locate a command on the PATH and return its full
path, like the `which` command (`autonomy = "autonomous"`, `output = "json"`). A
thin wrapper over the new `runspec_linux_core.which` helper (requires
`runspec-linux-core >= 0.3.3`). Handy for finding an interpreter/binary path
(e.g. `which python3`) to feed another runnable such as `create-venv`, without
hunting through `/usr/bin` in the path browser.

- Positional `name` accepts one command or several space-separated
  (`which "python3 pip3 git"`).
- `--all`/`-a` returns every match on the PATH, not just the first.
- `--path` overrides the search PATH (defaults to the host's `$PATH`).
- Prints `{path, all_found, results: [{name, found, path, paths}]}` and, mirroring
  the `which` command, exits non-zero when any name was not found.

## [0.5.0] — 2026-06-22

Add 8 cross-platform **web / TLS / API** runnables — thin wrappers over the new
`runspec-webops-core` (a new dependency). All are read-only network probes
(`autonomy = "autonomous"`, `output = "json"`) and use only the Python stdlib,
so the same runnables ship in `runspec-windows` too. They are a Python port of
the `runspec-webops` Node package (plus four extras), so a fleet that can't run
Node still gets them.

- **`cert-check`** — inspect a host's TLS certificate (days-to-expiry, issuer,
  SANs, fingerprint, trust). Exits `1` when expiring/expired, `2` when
  unreachable. Args: `--port`/`-p` (443), `--warn-days`/`-w` (30),
  `--timeout`/`-t` (10).
- **`cert-chain`** — print the full certificate chain (leaf → intermediates →
  root), flagging the self-signed root.
- **`tls-info`** — report the negotiated TLS protocol, cipher, and trust result.
- **`http-check`** — probe an HTTP(S) URL (status, response time, redirect
  target); `--expect-status`/`-e` exits non-zero on a mismatch; `--insecure`/`-k`
  skips TLS verification.
- **`http-headers`** — fetch response headers and flag missing security headers
  (HSTS, CSP, X-Frame-Options, …); `--method`/`-m` (HEAD/GET).
- **`redirect-trace`** — follow the redirect chain hop by hop with loop
  detection; `--max-hops`/`-n` (10).
- **`dns-lookup`** — resolve a hostname (A/AAAA + canonical name) or
  reverse-resolve an IP; `--reverse`/`-r` also PTRs each resolved address.
- **`api-spec`** — read an OpenAPI/Swagger spec (`source` = an http(s) URL or a
  local file) and condense its shape to bounded JSON, so an agent can write or
  update the runnables that call an internal API (and re-check when it drifts).
  Default is an overview (info, servers, every operation), narrowable with
  `--tag` / `--match`; `--operation`/`-o` (or `--path` + `--method`/`-X`) drills
  into one operation's params, request body and responses with `$ref`s resolved.
  OpenAPI 3.x and Swagger 2.0; JSON needs no extra, YAML needs
  `runspec-webops-core[yaml]`.

## [0.4.3] — 2026-06-21

- **`install-into-venv --venv` and `configure-pip --venv` are now marked
  `ui = "directory"`.** Both point at an existing venv *folder*, so the console's
  path picker opens a folder picker ("Use this folder") instead of a file picker
  — it was previously unclear the arg wanted the venv directory, not the
  `bin/python` inside it. (`install-into-venv --project` stays a generic `path`
  since it accepts either a project directory or a wheel/sdist file.)

## [0.4.2] — 2026-06-21

Add 3 runnables for managing Python virtualenvs on a fleet host — thin wrappers
over the new `runspec-linux-core` 0.3.2 helpers (dependency floor bumped to
`>=0.3.2`). All three are `autonomy = "confirm"`.

- **`create-venv`** — create a venv (always `--symlinks`) from a *specific*
  interpreter (`--python` is a browsable `path` arg → pick the remote binary, not
  the system default) at a free `--dest`. Creates a shared `<dest>/logs` dir mode
  `2775` and can `--group` (chgrp) the venv to a shared group. Flags:
  `--system-site-packages`, `--upgrade-deps`, `--clear`, `--prompt`.
- **`install-into-venv`** — `pip install` named `--package`(s) and/or a local
  `--project` (a `pyproject.toml` directory or wheel/sdist; `--editable` for
  `-e`) into a venv. Flags: `--upgrade`/`-U`, `--no-cache-dir`,
  `--force-reinstall`.
- **`configure-pip`** — write a `pip.conf` (`--index-url` + `--extra-index-url`,
  `--trusted-host`, `--cert`, `--timeout`, `--retries`, `--proxy`,
  `--require-virtualenv`) at `--scope user` (`~/.config/pip/pip.conf`) or
  `--scope venv` (`<venv>/pip.conf`) for private-PyPI access.

Unlike the package/power runnables, these declare **no `run_as`** in
`runspec.toml`. The service account that owns a shared venv is chosen per run, so
each takes a **`--run-as`** argument (and a `--become-method` of `sudo` or `su`)
and escalates internally — an empty `--run-as` runs as the SSH login user
(personal venv).

## [0.4.1] — 2026-06-20

Give `list-upgrades` `run_as = "root"`. It refreshes the package index
(`apt-get update` / `dnf check-update` / …) by default, which needs root — but it
had no `run_as`, so over the console it ran as the SSH login user and failed with
`Could not open lock file /var/lib/apt/lists/lock — Permission denied`. It now
escalates like the rest of the package family (works password-free with the
`enable-passwordless-sudo` drop-in). `--no-refresh` still skips the index refresh
and lists from the cache (now just faster, rather than a non-root workaround).

## [0.4.0] — 2026-06-20

Make the privileged package/power runnables escalate via runspec's `run_as`, and
add a one-time bootstrap runnable to enable it without a password. Bumps the
`runspec-linux-core` floor to `>=0.3.0`.

- **`run_as = "root"`** on `upgrade-packages`, `install-package`,
  `remove-package`, and `reboot-host`. The console's SSH executor now wraps these
  in `sudo -u root …` before they run, instead of failing with permission denied
  when the SSH login user isn't root. (`list-packages` / `list-upgrades` stay
  un-escalated — least privilege; `list-upgrades --no-refresh` works as any user.)
  A site with a service account can override `run_as` in its own spec.
- **New `enable-passwordless-sudo`** (confirm) — the one-time setup that makes the
  above work password-free. Takes a **`password`-typed** arg (delivered over the
  console's secret channel and piped to `sudo -S` — never on argv/ps/logs),
  optional `--user` (defaults to the login user), and `--scope`
  (`scoped` = the runspec venv runnables only; `all` = `NOPASSWD: ALL`). Installs
  a `visudo`-validated `/etc/sudoers.d/runspec`, rolling back on a bad result. It
  declares no `run_as` (it bootstraps root itself).

Setup flow: run `enable-passwordless-sudo` once per host (pick `all` for
agent-driven fleet management; `scoped` is enough for manual/CLI runs), then
`upgrade-packages` and friends run without a password.

## [0.3.0] — 2026-06-20

Add 6 runnables for managing a fleet's packages and power state from
runspec-console — thin wrappers over the new `runspec-linux-core` 0.2.0 helpers
(dependency floor bumped to `>=0.2.0`). They work across the common distro
families (apt/dnf/yum/zypper/pacman).

**Packages**

- `list-packages` (autonomous) — list installed packages, optional `--filter`
  substring and `--limit`.
- `list-upgrades` (autonomous) — list packages with an available upgrade;
  refreshes the index first, or pass `--no-refresh` to read the cache without
  root.
- `upgrade-packages` (confirm) — refresh the index and upgrade everything.
- `install-package` (confirm) — install one or more packages (`--package`,
  space-separated for several).
- `remove-package` (confirm) — remove one or more packages.

**Power**

- `reboot-host` (confirm) — reboot the host now, or schedule with
  `--delay-minutes`.

The read-only runnables are `autonomous`; the state-changing ones are `confirm`,
matching `restart-service` / `restart-container`. Missing package manager / a
non-root failure returns structured JSON (`{... false, "error": ...}`) and a
non-zero exit rather than a stack trace; package names are validated before they
reach the package manager.

## [0.2.0] — 2026-06-09

Split the pure logic out into a new dependency-free package,
**`runspec-linux-core`**, and make `runspec-linux` depend on it. Every runnable
module is now a thin wrapper: it parses args via `runspec`, calls the matching
`runspec_linux_core` function, and renders the JSON/exit behaviour. The helper
functions are re-exported from `runspec_linux` for back-compat
(`from runspec_linux import nc_send` and friends still work), and
`runspec_linux.nc_command.nc_send` is preserved.

Why: importing a helper (e.g. `nc_send`) previously required installing
`runspec-linux`, which put all 21 console scripts on PATH and surfaced all 21
runnables in `runspec local` / `runspec serve`. Code that only wants the
importable helpers can now depend on `runspec-linux-core` instead — it has no
`runspec` dependency, no `runspec.toml`, and no entry points, so it surfaces
nothing. No behaviour change for the runnables themselves; JSON output and exit
codes are unchanged.

## [0.1.3] — 2026-06-04

Switch `[config.logging]` to `store = "per-run"` so every runnable writes one
log file per invocation (`{runnable}.{utc-ts}.{run_id}.log`) instead of a single
shared rotating `{runnable}.log`. These runnables are meant to be installed into
shared venvs and run by multiple users (and in parallel); per-invocation files
are multi-writer safe — no in-process rotation and no contention over one file.
Use `runspec logs` to view, compact, and prune. The `rotate`/`keep` keys (inert
under per-run) are dropped. Bumps the `runspec` dependency floor to `>=0.27.0`,
where `store = "per-run"` was introduced.

## [0.1.2] — 2026-06-02

Lower the Python floor to 3.10 to match the core `runspec` library (no
3.11-only features are used). Bump the `runspec` dependency floor to
`>=0.23.0`.

## [0.1.1] — 2026-05-28

Enable `[config.logging]` for all 21 runnables. Each invocation now writes a
JSON audit record to `{venv}/logs/{runnable}.log` (rotates at 10 MB, keeps 14
backups) and emits a one-line run summary to stderr. The auto-added `--debug`
and `--no-summary` flags are available on every runnable. No code changes —
existing `print(json.dumps(...))` payloads are captured into the audit log via
the print-capture mechanism; stdout stays clean for pipe consumers.

## [0.1.0] — 2026-05-27

Initial release.

21 runnables covering Linux system administration:

**System monitoring** — `system-info`, `disk-usage`, `check-memory`, `list-processes`

**Services** — `check-service`, `list-services`, `restart-service`

**Logs** — `tail-log`, `search-log`, `journalctl`

**Network** — `ping-host`, `check-port`, `show-connections`

**Files** — `find-large-files`, `backup-files`

**Security** — `last-logins`, `who`

**Containers** — `list-containers`, `container-logs`, `restart-container`

**TCP interfaces** — `nc-command` (also exports `nc_send()` as a public Python API for wrapper runnables)

All read-only runnables are `autonomy = "autonomous"`. State-changing runnables (`restart-service`, `backup-files`, `restart-container`) are `autonomy = "confirm"`. Missing tools (`docker`, `systemctl`, `journalctl`) return a structured JSON error rather than a stack trace.
