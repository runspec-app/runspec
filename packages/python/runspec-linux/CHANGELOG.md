# runspec-linux Changelog

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
