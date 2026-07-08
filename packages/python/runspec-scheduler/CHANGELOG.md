# Changelog

All notable changes to `runspec-scheduler` are documented here.

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
