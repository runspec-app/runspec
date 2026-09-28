# Changelog

All notable changes to `runspec-scheduler` are documented here.

## [0.3.0]

- **Crontab lines run through core's `runspec exec`** (needs `runspec >= 0.62.0`;
  `docs/design/runspec-exec.md`). A line is now
  `env RUNSPEC_AGENT=1 [RUNSPEC_ENV_FILE=…] {venv}/bin/runspec exec --label
  sched:<id> --trigger schedule --lock <home>/.cache/runspec-scheduler/<id>.lock
  <runnable> -- <args>`, so every fire is recorded in the runs index under the
  label `sched:<id>` with its **observed exit code** and **captured
  stdout/stderr** — including runnables without `[config.logging]` and failures
  before logging starts, which the old `>/dev/null 2>&1` line lost entirely. The
  bare `flock -n` guard is replaced by `exec --lock`, so an overlap skip is a
  **recorded `skipped` run** instead of a silent exit. Re-run `sync` to upgrade
  existing lines; `status` flags un-upgraded ones as `line: "legacy"`.
- **New `status` verb** (read-only JSON): per schedule, the config entry, whether
  its crontab line is installed (`exec` vs `legacy`; `null` when the account's
  crontab can't be read without a prompt), next run, the recent runs from the
  runs index (`--runs`, default 5), and the last finished run's captured output +
  result + the runnable's own ERROR messages. Also lists **stale** installed ids
  no longer in the config. This is what runspec-console's Shared schedules view
  reads.
- **`run-now` fires through `runspec exec`** too, so a manual fire is recorded
  under the same `sched:<id>` label (its JSON result shape is unchanged).
- **Fix:** `sync` no longer crashes with a traceback when the `crontab` (or
  `sudo`) binary isn't installed — it's reported as an error for that account.
- **Schedule a runnable's subcommand (verb).** A schedule gains an optional
  `command` path — `command = ["list-directory"]` in the TOML,
  `runspec-scheduler add … --command list-directory` (space-separated for a
  nested verb, e.g. `--command "admin users"`). Previously a runnable with verbs
  (e.g. `filesystem`) couldn't be scheduled at all: only named args were passed,
  so every fire failed with "requires a command". `add` now **validates** the
  command path and `--args` against the runnable's spec — a missing required
  verb, an unknown verb, or an arg that verb doesn't take is refused with the
  valid choices listed, so a bad schedule never reaches a crontab. `sync` and
  `status` apply the same check to hand-edited configs (skipped / reported as
  *not installable* with the reason). **Args are now translated faithfully against the runnable's spec** (new
  `invocation.py`, `plan_invocation`, using core's own inference) instead of
  blindly: a `false` flag emitted `--no-<flag>`, which runspec rejects (so
  **every schedule with a flag set to false failed on every fire**) — now a
  `false` flag is omitted when that's its default, and a default-`true` flag
  switched off goes on the env channel (`RUNSPEC_<R>_ARG_<NAME>=0`, readable as
  false from runspec 0.62); a list was passed as one Python-repr string — now
  it repeats the flag (`--tags a --tags b`); positional args are passed by name
  (runspec ≥ 0.62 accepts that); a `rest` list goes after `--`. What argv can't
  express is refused at `add` with the reason — a list for a single-value arg, a
  nested table, a non-boolean flag value, and **`password` args** (secrets would
  sit in the config and crontab; use `--cred-vars`). The argv is built in the documented order —
  the runnable's own args, then the verb path, then the verb's args — on the
  crontab line, `run-now` and the legacy daemon alike. `status`/`list` include
  `command`.

## [0.2.0]

- **Cron backend (`sync`) — the v1 shared-scheduler execution model**
  (`docs/design/cron-scheduler.md`). Instead of the long-running daemon, each
  enabled **cron-timed** schedule becomes a marked line in its `run_as` account's
  own crontab, and the OS `cron` runs it — so there is no process of ours to
  supervise, and cron runs each line *as* its `run_as` account (the escalation is
  at crontab-write time, not per fire). `runspec-scheduler sync` reconciles the
  crontab to the config; `--dry-run` previews it. New `cronsync` module:
  - the line runs the runnable's own venv binary directly (so the core logging
    hook still records the run in the per-venv runs index — that is the
    observability channel), under `flock -n` for overlap safety, with only
    non-secret provenance + the `RUNSPEC_ENV_FILE` path via `env(1)`;
  - reconcile is declarative and marker-scoped (`# runspec:sched:<id>`): it strips
    the blocks we previously wrote and appends the current set, leaving the
    operator's own crontab lines untouched, and cleans up an account emptied of
    schedules via a machine-local managed-users state file;
  - only cron-timed schedules install here — `every` (interval) schedules,
    disabled ones, and runnables not installed in this venv are reported skipped
    (interval scheduling is the deferred systemd-timer backend).
- The `run` daemon + `install-service` verbs remain for now; they will be removed
  once the cron backend is validated in the wild.

## [0.1.1]

- `install-service`: handle the `systemctl --user` "Failed to connect to bus: No
  medium found" case that hits service accounts run under a bare `sudo -u`. The
  unit file is always written (a plain write, no bus needed); when lingering is
  already enabled the call now recovers `XDG_RUNTIME_DIR` from `/run/user/<uid>`
  on its own, and on a genuine bus failure it returns an actionable `hint`
  (enable lingering + re-run from a login shell: `sudo -iu <acct>
  runspec-scheduler install-service`) instead of a bare systemd error.
- README / `deploy/runspec-scheduler.service` / design doc: document the
  login-shell (`sudo -iu`) + `loginctl enable-linger` requirement for running the
  daemon as a service account.

## [0.1.0]

Initial release.

- A long-running per-venv scheduler daemon (`systemctl --user`) that runs runnables
  installed in its own venv on a cron/interval cadence — independent of any desktop
  console.
- Itself a runspec runnable (`discoverable = false`) with verbs: `run` (daemon),
  `add`, `remove`, `list`, `run-now`, `install-service`.
- Venv-scoped config at `{sys.prefix}/runspec_scheduler.toml` (override with
  `--config` / `$RUNSPEC_SCHEDULER_CONFIG`); atomic writes; mtime hot-reload.
- Timing (`cron` / `every`) mirrors runspec-console's schedule semantics exactly,
  pinned by a parity test.
- Rotating audit log at `{sys.prefix}/logs/scheduler.log`.
- Credential support for `run_as` runnables via `RUNSPEC_ENV_FILE`: a 600 env file
  owned by the `run_as` account (written with the secret on stdin, never argv); the
  daemon points `RUNSPEC_ENV_FILE` at it and escalates to `run_as` when firing.
