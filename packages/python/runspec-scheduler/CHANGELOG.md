# Changelog

All notable changes to `runspec-scheduler` are documented here.

## [0.7.0] — 2026-11-07

### Added

- **`restart`**: restart (or start) the daemon through the keeper the venv
  has — `systemctl --user restart`, `supervisorctl restart` (or starting
  supervisord when it is down), or stop + `run --detach` for the cron watchdog
  or a venv with no keeper (which it warns about). Run as the venv owner; a
  system unit is left to root. Waits up to `--wait` seconds (15) for the new
  daemon's first heartbeat; `--dry-run` prints the plan. Fires already running
  carry on and are still recorded. Config changes never need it.
- **`settings`**: show the `[scheduler]` settings, or change `--timeout`,
  `--misfire-grace` and `--catch-up-window`. Values are validated as the daemon
  reads them before anything is written; the schedules, backend and other keys
  are kept, and a running daemon reloads the change by itself.
- **`status` reports the settings and when a restart is needed.** The payload
  gains `settings` (backend + the three durations, in seconds) and `log` (the
  daemon's log file). The daemon records the runspec-scheduler version it
  started with in its heartbeat; `daemon.version` shows it and
  `daemon.restart_needed` is true when the installed version differs (an
  upgrade the running daemon hasn't loaded).
- **`run-now --detach`**: start one schedule's fire in the background and
  return its pid. It runs as a scheduled fire would (the schedule's `run_as`
  on the cron backend, escalated with `sudo -n` / `su` after a check that the
  escalation works; the venv owner on the daemon backend), in its own session
  with no stdio, so an SSH caller (the console's *Run now*) can hang up without
  stopping it. A fire that ends within 1.5 s reports its exit code. The run is
  recorded like any other fire, and `--lock` still makes a second one a
  recorded skip.

### Changed

- **Fires say which backend started them.** The `runspec exec --trigger` a fire
  passes is now `schedule:cron`, `schedule:daemon` or `schedule:run-now`
  (was `schedule` for all), so the runs index and the console's History can
  tell them apart. Cron lines pick up the new value on their next `sync`, the
  daemon after a restart; skipped and failed daemon rows use
  `schedule:daemon`.

## [0.6.2] — 2026-11-07

### Fixed

- **A venv no longer removes other venvs' schedules from a shared crontab.**
  When several venvs belong to one account, they all write to that account's
  crontab. `sync`, `use daemon` and `status` used to treat every
  `# runspec:sched:` block as their own, so syncing one venv, or moving it to
  the daemon, deleted the other venvs' cron schedules, and `status` listed them
  as stale. A block now counts as this venv's only when its job line runs from
  this venv's `bin/` folder; other venvs' blocks are left exactly as they are.
  `use daemon` also reports only the schedules it actually removed.

## [0.6.1] — 2026-11-07

Safer crontab writes.

### Fixed

- **A stray marker no longer deletes your next crontab line.** `sync`, `use`
  and `install-service` remove a `# runspec:sched:<id>` (or keeper) tag together
  with the line under it. That line is now removed only when it is really ours:
  a cron job that carries the schedule's lock path or `--label sched:<id>` (a
  keeper line: the venv folder). A tag left on its own used to take the
  operator's next line with it.

### Changed

- **The crontab is backed up before each rewrite.** The account's crontab as it
  was is saved to `{venv}/runspec_scheduler_backups/crontab-<account>-<time>.txt`
  (owner-only, newest 10 per account kept). A backup that can't be written
  doesn't stop the write.
- **An unchanged crontab isn't rewritten.** When reconciling changes nothing,
  `crontab -` isn't run, which also narrows the window in which a `crontab -e`
  saved at the same moment could be overwritten.

## [0.6.0] — 2026-11-07

Keepers: what starts the daemon at boot and restarts it when it dies.

### Added

- **`install-service --manager auto|systemd-user|supervisor|cron|systemd`.**
  `auto` takes `systemd-user` when lingering is on, systemd is newer than 219
  and the `--user` bus answers; else `supervisor` when supervisord is installed
  in the venv; else a crontab watchdog. The result says why.
  - `supervisor`: supervisord inside the venv (config under `{venv}/supervisor/`),
    started by `@reboot` and every-5-minutes crontab lines. New
    `[supervisor]` extra. `--system` prints a program block for the host's own
    supervisord (`user=<owner>`) instead.
  - `cron`: `run --detach` at boot and every 5 minutes.
  - `systemd`: a system unit with `User=<owner>`, staged next to the venv and
    printed with the steps root runs once; `/etc` is never written.
- **`install-service --remove`** removes every keeper this venv has (or just
  `--manager`'s) and stops the daemon, so deleting a venv can't leave a keeper
  restarting a daemon from a missing folder.
- **`run --detach`** starts the daemon in its own session and returns; output
  goes to `logs/scheduler.stdout.log`. The parent takes the venv lock and hands
  it to the child, so a watchdog running it every few minutes never starts two.
- **`status` reports the keeper**: a `keeper` list of what keeps this venv's
  daemon running (`manager`, where, its crontab lines, supervisord's pid).

### Changed

- **`install-service` changes nothing without `--write`.** It prints the plan
  (files, crontab lines, commands, `root_steps`) as JSON. The old `--enable` /
  `--no-enable` flag is gone: `--manager systemd-user --write` is the old
  default. It refuses to set up a keeper for anyone but the venv owner (except
  the root-installed `systemd` unit), and warns when the venv belongs to root.
- Keeper crontab lines are tagged `# runspec:scheduler-keeper:<venv path>`,
  apart from the cron backend's `# runspec:sched:` lines.

### Fixed

- `runspec-scheduler --config X run` now takes the venv lock early like
  `run --config X` (the early check took `X` for the command).

## [0.5.0] — 2026-11-07

The daemon is back, as a second backend beside host cron. Each venv picks one:
`[scheduler] backend = "cron"` (the default, unchanged) or `"daemon"`.

### Added

- **`[scheduler]` table** in `runspec_scheduler.toml`: `backend` (`cron` /
  `daemon`), `timeout` (default for every fire, `6h`; `0` = no limit),
  `misfire_grace` (default `5m`) and `catch_up_window` (default `1d`).
  Durations take seconds or `90m` / `6h` / `1d`. `write_schedules` keeps the
  table.
- **`use cron|daemon`** switches a venv's backend. Switching to the daemon first
  removes this venv's marked crontab lines (and refuses to switch if it can't);
  switching back to cron writes them with `sync`. The JSON result says whether
  the daemon is still running.
- **Per-schedule `timeout`** (`add --timeout 30m`) and **`catch_up`**
  (`add --catch-up`). With `catch_up = true`, a daemon that starts after a fire
  was due (down, rebooting, paused) runs one coalesced fire straight away, if the
  missed fire is within `catch_up_window`. Without it, missed fires are skipped
  and recorded.
- **`status` reports the daemon**: `backend` plus a `daemon` block (`state` =
  running / stale / stopped, pid, uptime, heartbeat age, `config_error`, running
  fires, fire count). On the daemon backend each schedule's `next_run` comes from
  the daemon and `installed` means the daemon holds the job. A crontab line left
  behind on a daemon venv shows as stale with reason `backend is daemon`.
- **Heartbeat** at `{venv}/logs/scheduler_state.json`, written atomically every
  few seconds (`run --reload-interval`).
- **One daemon per venv**: `run` takes an flock on `runspec_scheduler.lock` next
  to the config before anything else. A second `run` prints who holds it and
  exits 0, without leaving a run record.

### Changed

- **Every daemon fire goes through `runspec exec --label sched:<id> --trigger
  schedule --lock …`**, the same command a cron line runs. Each fire is one
  recorded run with its observed exit code and captured output, and an overlap
  with a still-running fire is a recorded `skipped` run. The daemon doesn't
  capture output any more (it only reaps the exit code).
- `every` schedules need the daemon backend; `add --every` on a cron venv now
  says so and points at `use daemon`. `sync` on a daemon venv writes no lines and
  removes any this venv left behind (its result says `backend: daemon`).
- A cron line gets `--timeout` only when its schedule sets one; cron fires keep
  no default limit.
- The systemd unit template sets `KillMode=process`, so restarting the daemon
  leaves running fires to finish.
- Removed `runner.run_runnable` and `runner.provenance_env` (unused since fires
  go through `runspec exec`).

### Fixed

The 15 issues from the robustness review, each with a test that starts the real
daemon and the real `runspec exec` (`tests/test_daemon_process.py`):

1. A fire that started more than 1 second late was skipped with only a log line.
   `misfire_grace_time` is now 5 minutes, and a fire missed past it is recorded
   as a `skipped` run plus a `scheduler.fire_missed` audit event, including the
   run times APScheduler's coalescing drops.
2. A fire due while the previous one was still running was dropped silently. It
   now reaches `runspec exec --lock`, which records it as `skipped`.
3. Every config change removed and re-added all jobs, restarting `every`
   intervals. Reloads now diff the config and leave unchanged jobs alone.
4. Each fire re-read the config, so a half-written file dropped the fire. Fires
   use the last config that loaded cleanly.
5. An error resolving the runnable (mid `pip install`) left no record. Every fire
   is wrapped; a fire that can't start is a `failed` run (127 runnable missing,
   2 bad args, 126 can't execute, 70 scheduler error).
6. A hung runnable held a worker forever. Fires time out after 6 hours by
   default: `runspec exec` stops the run, and the daemon then kills the run's
   whole process group, with a backstop that kills the group itself if exec
   doesn't return. The heartbeat lists running fires.
7. Captured output kept a fire open while a background child held stdout, and
   held a chatty runnable's output in memory. Fires run with stdin/stdout/stderr
   on `/dev/null`; exec captures the output into the run's log.
8. Invalid UTF-8 output made a finished run look failed. Fixed by 7.
9. A config that failed to parse at start left a daemon with no jobs. `run` now
   exits 1; a config that breaks later keeps the last good jobs and shows
   `config_error`.
10. The daemon's own run record stayed `running` for weeks, and its log lines
    appeared twice. It closes its run record at start, configures logging once,
    and records `scheduler.daemon` started / stopped audit events.
11. Config changes were detected by mtime alone. The daemon now compares mtime
    (ns), size and inode, and SIGHUP reloads at once.
12. SIGHUP killed the daemon. It now reloads.
13. Stopping waited for every running fire. Each fire runs in its own session and
    survives a daemon stop; the daemon exits within a second.
14. An unexpected error in the loop exited without cleaning up. The loop is
    wrapped in try/finally and exits 1 so the keeper restarts it.
15. A keeper's thin environment broke runnables that call `/usr/sbin/…` tools.
    The daemon fills in a standard `PATH` and `LANG=C.UTF-8` when missing and
    drops inherited `RUNSPEC_*` variables from fires.

### Known limit

- A `run_as` fire escalated through `sudo` runs as another account. When it
  times out, the daemon can't signal that account's processes, so a child the
  runnable left behind may outlive the timeout. `runspec exec` still stops the
  run itself.

## [0.4.1] — 2026-09-30

### Changed

- The runner's escalated fire now passes its variables through `build_exec_argv`,
  as the SPEC env frame on stdin, instead of `sudo … env KEY=val`. It needs
  runspec >= 0.67.0. These variables were never secret: provenance, a flag's
  `0`, and the path of the `RUNSPEC_ENV_FILE`; the credentials themselves live
  in that file. This follows the new rule that no value goes on a command line,
  and it avoids the new `DeprecationWarning`.

## [0.4.0]

- **`status` sees runs still in the logs inbox** (runspec >= 0.66,
  `docs/design/venv-logs-inbox.md`).
  - A schedule whose crontab belongs to an account other than the venv owner
    records each run in the venv's logs inbox until the owner sweeps it. Its
    runs now show straight away instead of waiting for the sweep.
  - Each run carries `os_user` (kernel-attested) and `pending`.
  - An inbox row counts only when `os_user` is the schedule's own account, so a
    file anyone dropped in the inbox labelled `sched:<id>` is ignored.
  - `last_output` reads a pending run's log from the inbox.
  - On an older core it falls back to the index alone.

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
