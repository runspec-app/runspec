# runspec-scheduler

**Cron, but limited to runspec runnables installed in one venv.**

Shared, always-on schedules for the runnables installed in *one venv*, independent
of any desktop console. Each venv picks a backend:

- **`cron`** (default): `sync` writes marked lines into the user crontab. Nothing
  else runs.
- **`daemon`**: a long-running `runspec-scheduler run` per venv fires the
  schedules itself. It adds `every` intervals, `catch_up`, a heartbeat that
  `status` reads, and a missed fire is recorded instead of lost.

Either way every fire runs `runspec exec --label sched:<id> --lock …`, so it is
one recorded run (exit code + captured output) in the venv's History. It is
itself a runspec runnable, so `runspec-console` can detect it, but
`discoverable = false` keeps it out of the Forms tab / `runspec serve` MCP surface.

## Why

A console-driven schedule only fires while the desktop console is open, and it's
per-operator. `runspec-scheduler` gives you **shared, always-on** schedules that live
on the host and run whether or not a console is connected.

## Install

```bash
# Into the same venv whose runnables you want to schedule, as the venv owner:
pip install runspec-scheduler
runspec-scheduler use daemon                  # skip this to stay on host cron
runspec-scheduler install-service             # shows the keeper it would set up
runspec-scheduler install-service --write     # sets it up and starts the daemon
```

The daemon runs as the venv owner, one per venv. Something has to start it at
boot and restart it when it dies: the **keeper**. `install-service` picks one
(`--manager auto`) or takes the one you name:

| `--manager` | needs | what it sets up |
|---|---|---|
| `systemd-user` | lingering on, systemd newer than 219 | `~/.config/systemd/user/runspec-scheduler-<venv>.service`, enabled with `systemctl --user enable --now` |
| `supervisor` | `pip install 'runspec-scheduler[supervisor]'` | supervisord inside the venv (`{venv}/supervisor/`), started at boot and checked every 5 minutes from your crontab |
| `cron` | crontab | `run --detach` at boot and every 5 minutes; a second start is a no-op, so a crashed daemon is back within 5 minutes |
| `systemd` | root, once | a system unit with `User=<owner>`; only printed (root runs the steps it lists), never written to `/etc` |

`auto` takes `systemd-user` when lingering is on and the `--user` manager
answers, else `supervisor` when supervisord is in the venv, else `cron`, and
says why. `--manager supervisor --system` prints a program block for the host's
own supervisord instead.

Nothing changes without `--write`: the result lists the files, crontab lines,
commands and the steps root has to run. Crontab lines are tagged
`# runspec:scheduler-keeper:<venv>`, separate from the cron backend's lines.

```bash
runspec-scheduler install-service --remove            # what removing would do
runspec-scheduler install-service --remove --write    # remove every keeper, stop the daemon
```

Run `--remove --write` before deleting a venv, so no keeper restarts a daemon
from a folder that is gone. `status` lists the keepers under `keeper`.

After upgrading runspec-scheduler, restart the daemon so it runs the new code
(`status` shows `restart_needed` until you do). `restart` goes through the
keeper the venv has: `systemctl --user restart`, `supervisorctl restart`, or
for the cron watchdog (or no keeper) stop + `run --detach`. Fires already
running carry on and are still recorded. A system unit only root can restart.

```bash
runspec-scheduler restart --dry-run    # what it would do
runspec-scheduler restart              # restart, wait for the first heartbeat
```

### Running the daemon as a service account

`systemctl --user` needs the account's own user session (`XDG_RUNTIME_DIR` +
D-Bus). A bare `sudo -u <acct>` doesn't set that up, so systemctl fails with
*"Failed to connect to bus: No medium found."* Run from a **login shell** and
ask root to enable lingering:

```bash
sudo loginctl enable-linger serviceacc
sudo -iu serviceacc runspec-scheduler install-service --manager systemd-user --write
sudo -iu serviceacc systemctl --user status runspec-scheduler-<venv>
```

If lingering is already on, `install-service` recovers `XDG_RUNTIME_DIR` from
`/run/user/<uid>` on its own; otherwise it returns a hint with the commands
above. Without root, use `--manager supervisor` or `--manager cron`.

## Use

```bash
runspec-scheduler add --id nightly --runnable backup --cron '0 2 * * *'
runspec-scheduler add --id poll   --runnable healthcheck --every 15m --args '{"level":"warn"}'   # daemon only
runspec-scheduler add --id report --runnable report --cron '0 6 * * *' --timeout 30m --catch-up
runspec-scheduler list
runspec-scheduler status
runspec-scheduler watch             # status kept on screen, redrawn every 5 s (Ctrl+C)
runspec-scheduler run-now --id poll
runspec-scheduler run-now --id nightly --detach   # start it in the background and return
runspec-scheduler remove --id poll
runspec-scheduler enable --id cleanup-tmp.nightly   # turn on a schedule a package offers
runspec-scheduler disable --id nightly              # pause one (or hide an offered one)
runspec-scheduler use daemon        # or: use cron
```

- At a terminal every verb prints text: a sentence on what happened, and the
  schedules as a table (`status` adds what's failing, who is on it and the last
  lines of its output). Piped, from a script or from runspec-console it prints
  JSON; `--json` gives JSON at a terminal too.
- Exactly one of `--cron` (5 fields, or `@hourly` / `@daily` / `@midnight` /
  `@weekly` / `@monthly` / `@yearly`) or `--every` (`15m` / `1h` / `1d`; daemon
  backend only).
- `--timezone` (an IANA name like `Europe/London`) fires the schedule on that
  zone's clock; blank means the venv's zone (`settings --timezone`), else the
  host's. A crontab line can only fire on the host's clock, so on the **cron**
  backend a schedule in another zone is refused by `add` and skipped by `sync`
  (`status` says why): use the daemon for it.
- When the clocks go back, a schedule at fixed times (no `*` in its minute or
  hour, e.g. `30 1 * * *`) fires once, as cron does; one with `*` there (e.g.
  `*/30 * * * *`) keeps firing through both copies of the hour. A time in the
  hour the clocks skip fires straight after the jump.
- `--id` is letters, digits, `.`, `_` and `-` (at most 64), starting with a
  letter or digit.
- `--args` is a JSON object passed to the target runnable.
- `--timeout` stops a fire that runs longer (daemon default `6h`; `0` = no limit).
- `--catch-up` runs one fire straight away when the daemon starts after that
  schedule's fire was due (it was down, the host rebooted). Without it a missed
  fire is recorded as `skipped`.
- The daemon picks up config changes within a few seconds, or at once on
  `SIGHUP`. On the cron backend run `sync` after editing.
- `run-now --detach` starts the fire as a scheduled fire would run it (the
  schedule's `run_as` on cron, the venv owner on the daemon) in its own session,
  so it carries on after you log out; its run is recorded like any other fire.
- Credentials for scheduled fires go in `{venv}/runspec_schedules.env` (mode
  600, owned by the account the fires run as), not `.runspec_env`: every fire
  loads it on top of `.runspec_env`, and interactive runs in the venv never
  see it. The console's Env files window edits both.
- Every fire's run carries a trigger saying what started it:
  `schedule:cron`, `schedule:daemon` or `schedule:run-now`.

## Schedules from packages

A runnable can offer schedules in its own `runspec.toml`, so a venv it's
installed in can run it without anyone writing the schedule:

```toml
[cleanup-tmp.meta.scheduler.nightly]       # schedule id: cleanup-tmp.nightly
description = "Remove files older than 7 days from /tmp"
at       = "02:30"           # or "midnight", "noon"
on       = "daily"           # or "weekdays", "mon-fri", "weekends", "sun", "mon,wed", "tue-thu", "1", "last"
timezone = "local"           # "local" (default), "UTC", or e.g. "Europe/London"
spread   = "30m"             # each host picks a fixed minute in 02:30-03:00
args     = {older-than = "7d"}

[http-check.meta.scheduler.poll]
every = "5m"                 # intervals need the daemon backend
args  = {url = "https://example.com/health"}
```

- Exactly one of `at` (with `on`), `every` or `cron` (5 fields or `@daily`
  etc.). Also `command` (for a runnable with verbs), `timeout`, `catch_up` and
  `description`. `args` are checked against the runnable's spec like `add`'s;
  leave site values and secrets to `runspec_schedules.env`.
- `timezone = "local"` means the venv's zone (`settings --timezone
  Australia/Sydney`), else the host's. Use `"UTC"` for a job that must run at
  the same moment on every host, or a zone name for one tied to a business's
  clock. On the cron backend anything off the host's clock needs the daemon.
- `spread` gives each host a fixed offset (a hash of host name and schedule
  id), so 70 hosts don't all start at 02:30:00. It never moves a fire past
  midnight.
- `last` (the month's last day) needs the daemon backend; cron has no such day.

What the venv does with them is its policy:

```bash
runspec-scheduler settings --package-schedules offer   # default: listed, off until enabled
runspec-scheduler settings --package-schedules auto    # on as soon as the package is installed
runspec-scheduler settings --package-schedules off     # ignored
```

`status` lists offered ones under *Offered by installed packages*;
`enable --id <id>` turns one on and `disable --id <id>` hides it. Either writes
only `enabled` for that id, so when the package moves its time in a new
release the venv follows. Change a package schedule's time with `add --id
<id> …` (that venv's time then stays, and `status` shows what the package
says next to it); `remove --id <id>` puts it back to the package's.
Uninstalling the package drops its schedules. The daemon looks for installed,
upgraded or removed packages once a minute; on cron run `sync` after `pip
install`.

## Config

Schedules live in `{venv}/runspec_scheduler.toml` (override with `--config` or
`$RUNSPEC_SCHEDULER_CONFIG`):

```
[scheduler]
backend = "daemon"        # or "cron" (default)
timeout = "6h"            # default per fire; 0 = no limit
misfire_grace = "5m"      # a fire later than this is recorded as missed
catch_up_window = "1d"    # how far back catch_up looks
timezone = "Australia/Sydney"   # schedules with no zone of their own; default the host's
package_schedules = "offer"     # or "auto" / "off"

[[schedule]]
id = "report"
runnable = "report"
cron = "0 6 * * *"
timeout = "30m"
catch_up = true
```

Change the `[scheduler]` settings with `settings` (the daemon reloads them by
itself; `use` switches the backend):

```bash
runspec-scheduler settings                                  # show them
runspec-scheduler settings --timeout 2h --misfire-grace 10m # change two
runspec-scheduler settings --timezone Australia/Sydney      # the venv's zone (--timezone host = the host's)
```

The daemon writes a heartbeat to `{venv}/logs/scheduler_state.json` and a
rotating log to `{venv}/logs/scheduler.log`; `runspec-scheduler status` reads
both.

Log files in `{venv}/logs/`, each rotated at midnight with 7 days kept:

| File | Written by | Holds |
|---|---|---|
| `scheduler.log` | the daemon | what the daemon did (starts, reloads, fires, warnings) |
| `runspec-scheduler.log` | runspec | a record of each `runspec-scheduler` verb run (`status`, `sync`, …) |
| `scheduler.stdout.log` | the cron watchdog or supervisord | the daemon's stdout/stderr: startup errors and crash tracebacks only |
| `supervisord.log` | the venv's supervisord | supervisord's own events (supervisor keeper only) |

The daemon prints its log lines to stderr only at a terminal or under systemd
(read them with `journalctl --user -u runspec-scheduler-<venv>`); under the other
keepers they are in `scheduler.log` only. It rotates `scheduler.stdout.log` and
`supervisord.log` itself, since neither keeper rotates by day. Only one daemon runs per venv (an flock on `runspec_scheduler.lock`).

A fire that times out is stopped by `runspec exec`, then the daemon kills what is
left of its process group. For a `run_as` fire escalated through `sudo`, children
running as that other account can outlive the timeout.

## Credentials (`run_as` runnables)

`runspec-console` can attach service-account credentials to a remote schedule. For a
runnable that declares `run_as`, the selected credentials are written to a **600 env
file owned by the `run_as` account** in that account's home; the daemon points
`RUNSPEC_ENV_FILE` at it and escalates to `run_as` when firing. Credentials are never
placed on a command line. This requires the daemon/SSH user to have passwordless
`sudo -u <run_as>` (the same precondition as running that `run_as` runnable at all).

Runnables **without** `run_as` run as the daemon's own user with the daemon's
environment (systemd `EnvironmentFile`) — no console credential injection.
