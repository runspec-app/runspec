# Changelog

All notable changes to `runspec-scheduler` are documented here.

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
