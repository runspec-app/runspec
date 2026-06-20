# runspec-linux Changelog

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
