# Changelog

All notable changes to `runspec-scheduler` are documented here.

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
