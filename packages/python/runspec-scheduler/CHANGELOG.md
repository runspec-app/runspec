# Changelog

All notable changes to `runspec-scheduler` are documented here.

## [0.10.3] — 2026-11-12

### Fixed

- **systemd --user without lingering is refused before anything is written.**
  `install-service --manager systemd-user --write` on an account with lingering
  off and no login session wrote the unit, then failed at
  `systemctl --user daemon-reload` (*$XDG_RUNTIME_DIR not defined*) and left
  the file behind. It now stops first and says to ask root for
  `sudo loginctl enable-linger <owner>`, or to use supervisord or the crontab
  watchdog. An install that fails part-way for any other reason puts back the
  files it wrote (listed as `undone`).
- **Restart and status no longer take a never-enabled unit for the keeper.**
  The leftover unit file made `restart` run `systemctl --user restart` even
  after the console had fallen back to the crontab watchdog, so the daemon was
  never restarted onto the upgraded code. A systemd --user unit now counts only
  once `enable` has linked it into `default.target.wants`. `install-service
  --remove` deletes a stray unit without calling systemctl.
- The login-shell hint now also appears for systemd's *Failed to connect to
  user scope bus* wording.

## [0.10.2] — 2026-11-11

### Fixed

- **A rebranded copy knows its own name.** After `runspec rebrand
  runspec-scheduler mycorp-scheduler` the daemon read its version under the
  upstream name, found nothing, and so never said a restart was needed after an
  upgrade; the keeper hints told people to `pip install
  'runspec-scheduler[supervisor]'`, a package the private index may not carry.
  The scheduler now finds the distribution that provides `runspec_scheduler`
  (`distname.py`) and uses that name for its version and its hints, and
  `status` reports it as `package`. The command, the import package and the
  `[supervisor]` extra are unchanged.

## [0.10.1] — 2026-11-10

### Fixed

- **Fires are no longer recorded as agent runs.** Crontab lines, daemon fires
  and `run-now` set `RUNSPEC_AGENT=1`, so every fire's run record said
  `agent: true` and the console's Analytics and the agent's history search
  counted them as runs by the assistant. (History's *Run by* already showed
  cron / daemon from the trigger.) Lines now carry only `RUNSPEC_ENV_FILE`, plus
  `RUNSPEC_<R>_ARG_<FLAG>=0` for a default-true flag switched off, and start
  with the binary when there is nothing to set. The daemon and `run-now
  --detach` also drop a `RUNSPEC_AGENT` they inherit (an agent that started the
  daemon). A fire stays non-interactive: it has no terminal, so nothing prompts.
  Run `sync` (the console does it after a Marketplace install) to rewrite
  existing crontab lines.
- **`scheduler.stdout.log` no longer copies `scheduler.log`, and rotates.** The
  daemon printed every log line to stderr as well as to `scheduler.log`, so
  under the cron watchdog or supervisord (whose capture file is
  `scheduler.stdout.log`) each line was written twice, and under the watchdog
  that file never rotated. The daemon now echoes to stderr only at a terminal
  or when its stderr is systemd's journal (where you read it), so
  `scheduler.stdout.log` holds only startup errors and crash tracebacks. The
  daemon also rotates it at midnight like `scheduler.log`
  (`scheduler.stdout.log.YYYY-MM-DD`, newest 7 days kept): under the cron
  watchdog it reopens its own stdout/stderr; under the venv's supervisord it
  renames the files and sends supervisord `SIGUSR2` to reopen them, which also
  rotates `supervisord.log`. The supervisord configs `install-service` writes
  turn off supervisord's own size rotation for these (a root supervisord, which
  the daemon can't signal, keeps size rotation: 1 MB × 7). Re-run
  `install-service --write` (or the console's Update venv) to pick up the new
  supervisord config; without it, supervisord's size rotation stays on alongside.
  `runspec-scheduler.log` (the record of each verb run) already rotated at
  midnight with 7 kept, through runspec's logging defaults.

## [0.10.0] — 2026-11-09

### Added

- **Schedules from packages.** A runnable can offer schedules in its own
  runspec.toml, one table each: `[<runnable>.meta.scheduler.<name>]` (id
  `<runnable>.<name>`) with exactly one of `at` (`"02:30"`, `"midnight"`,
  `"noon"`) + `on` (`daily`, `weekdays`/`mon-fri`, `weekends`, days like
  `mon,wed` or `tue-thu`, a day of the month, `last`), `every` or `cron`, and
  optional `timezone`, `spread`, `args`, `command`, `timeout`, `catch_up`,
  `description`. Read live from the installed packages (with `include`
  resolved), never copied into the config; a broken table is reported in
  `status` and the daemon log without stopping anything else. `packages.py`.
- **Venv policy** `[scheduler] package_schedules`: `offer` (default; listed in
  `status` under *Offered by installed packages*, off until turned on), `auto`
  (on as soon as installed) or `off` (ignored). A `[[schedule]]` with a package
  schedule's id changes it: `enable`/`disable` write only `enabled` (so a new
  package release that moves the time is followed), `add` with the id sets this
  venv's own time (status shows the package's next to it) and writes only what
  differs from the package, so an offered schedule stays offered and its args
  still follow the package, `remove` puts it back. An entry left changing a package schedule that's gone is reported, not
  an error.
- **`enable` / `disable` verbs** (`--id`) for any schedule. `enable` on the
  cron backend refuses what cron can't fire.
- **The venv's zone** `[scheduler] timezone`: what a schedule with no zone of
  its own (or `"local"`) fires on — for a host whose clock runs on UTC in
  another country. `settings --timezone <zone>` sets it, `--timezone host`
  clears it, `settings --package-schedules` sets the policy. The config keeps
  each schedule's zone as written, so changing the venv's zone moves them all.
  On cron a venv zone other than the host's makes those schedules *needs the
  daemon* (status and `settings` say so).
- **`spread`** (with `at`): each host fires at a fixed minute inside the window
  (a hash of host name and schedule id), never past midnight; on cron it's
  baked into the crontab line at sync.
- The daemon looks at the installed packages once a minute and reloads when
  their schedules change (pip has no post-install hook). On cron run `sync`.
- `status` and `settings` report `timezone`, `package_schedules` and the host's
  zone; status rows from packages carry `origin`, `offered`, `description` and
  `package` (`words`, `when`, `source`, `overridden`).
- JSON schema: `meta.scheduler` tables are checked (`$defs/package_schedule`).

### Changed

- Config writers (`add`, `remove`, `use`, `settings`, `enable`, `disable`) now
  keep each `[[schedule]]` table as it is in the file, so a small entry that
  only turns a package schedule on survives every other write.

## [0.9.0] — 2026-11-09

### Added

- **Text at a terminal.** Every verb prints readable text when stdout is a
  terminal: `status` shows the venv, backend, daemon (pid, uptime, version,
  *restart needed*), keeper and settings, then one row per schedule (what it
  runs, when, next run, last run, state: ok / paused / failing / being handled
  by … / not in crontab / won't run: …) and, under *Needs attention*, each
  failing schedule's note and the last lines of its output; `list` is a table;
  `add`, `remove`, `sync`, `use`, `settings`, `run-now`, `restart` and
  `install-service` say what happened (a plan lists its steps, the files and,
  for a file root writes, its content). Errors go to stderr as one line. JSON
  is unchanged when stdout isn't a terminal (pipes, scripts, runspec-console
  over SSH), and the new global `--json` gives JSON at a terminal too.
  `render.py`; terminal detection asks `sys.__stdout__`, because runspec's
  stdout tee always reports `isatty() == False`.
- **`watch`** keeps the status on screen, redrawn every `--interval` seconds
  (default 5) until Ctrl+C; not at a terminal it prints one JSON status per
  line. It ends its own run record at once so a long watch doesn't fill the
  run log. `autonomy = "manual"`: it never returns, so only a person runs it.

### Fixed

- `list`, `remove` and `run-now` on a config that doesn't load now say why in
  one line (exit 2) instead of a traceback.

## [0.8.0] — 2026-11-09

### Fixed

- **The cron backend no longer ignores `timezone`.** A crontab line fires on the
  host's clock, so a schedule saved with `timezone = "Europe/London"` used to
  fire at London time on the daemon but at host time on cron. Now a schedule
  whose zone keeps a different clock from the host's (`timing.same_zone`:
  equal names, UTC aliases, or matching offsets for two years) is skipped by
  `sync` and shown in `status` with the reason *needs the daemon backend*, and
  `add` refuses it on a cron venv. **Upgrade note:** the next `sync` removes the
  crontab line of such a schedule; move it to the daemon (`use daemon`) or clear
  its timezone.
- **`@daily`, `@midnight`, `@hourly`, `@weekly`, `@monthly`, `@yearly` work on
  the daemon.** APScheduler 3 only takes 5 fields, so the daemon logged
  "invalid timing" and never scheduled them; they are now expanded
  (`timing.expand_cron`). An unknown nickname (`@reboot` included) is refused
  when the config loads.
- **No double fire when the clocks go back.** APScheduler 3 fired `30 1 * * *`
  in Europe/London at 01:30 BST and again at 01:30 GMT. A schedule at fixed
  times now fires once, as cron does; one with `*` in its minute or hour still
  runs through both copies of the hour.
- **Missed-fire counting in that hour.** The daemon stepped through fire times
  with wall-clock arithmetic (01:30 GMT + 1 s is 01:30:01 BST, an hour earlier),
  which could loop in the repeated hour. It now counts in real time
  (`timing.later`).
- **Schedule ids are checked.** `add` takes letters, digits, `.`, `_` and `-`
  (at most 64, starting with a letter or digit), the rule the console already
  used. An id already in a config that can't work (a `/`, `\`, a space or
  control character, `.`/`..`) is skipped by `sync` and the daemon with a reason
  instead of naming a stray file.

## [0.7.3] — 2026-11-07

### Fixed

- **Two venvs owned by one account no longer share a schedule's lock.** A
  fire's `runspec exec --lock` file was `~/.cache/runspec-scheduler/<id>.lock`,
  so when two venvs with the same owner each had a schedule called (say)
  `nightly`, whichever fired second was recorded as `skipped` ("still
  running"). The lock now sits in a per-venv folder:
  `~/.cache/runspec-scheduler/<venv>-<8 hex>/<id>.lock` (`cronsync.venv_tag`).
  Daemon and `run-now` fires pick it up straight away; crontab lines keep the
  old path until the next `runspec-scheduler sync` (the console's *Sync
  crontab*). Old and new lines are both still recognised as ours.
- **The per-schedule credential file is named per venv too**
  (`~/.config/runspec-scheduler/env/<venv>-<8 hex>-<id>.env`), so removing a
  schedule in one venv can't delete another venv's file of the same id.
  Existing files keep their stored path.

## [0.7.2] — 2026-11-07

### Added

- **`status` says who is handling a failing schedule.** Each schedule gains
  `attention` — `{state, failures, since, run_id, note}`, where `state` is
  clean / failed / handling / done for its failures since the last success —
  and each listed run carries its newest `note`. Reads 50 runs back so a long
  streak is seen whole. Needs runspec 0.83.0 for the notes (`runspec history
  claim/resolve/release`); on an older core `attention` is `null`.

## [0.7.1] — 2026-11-07

### Added

- **A schedule-only env file per venv**: `{venv}/runspec_schedules.env`. Every
  fire (cron line, daemon and `run-now`) passes it to
  `runspec exec --env-file`, so what it holds reaches scheduled fires and not
  the interactive runs in the venv (console Forms, the agent, a person on the
  host), which only read `.runspec_env`. Put a service account's credentials
  here, not in `.runspec_env`, so a run without its own credentials fails
  instead of quietly using them. It stacks over `.runspec_env` (shared
  settings still apply) and under the fire's own environment. A missing file
  adds nothing; make it mode 600, owned by the account the fires run as
  (`exec` refuses one that is writable by others or owned by another
  account). Needs runspec 0.82.0 (now the minimum).

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
