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
runspec-scheduler run-now --id poll
runspec-scheduler remove --id poll
runspec-scheduler use daemon        # or: use cron
```

- Exactly one of `--cron` (5-field cron) or `--every` (`15m` / `1h` / `1d`;
  daemon backend only).
- `--args` is a JSON object passed to the target runnable.
- `--timeout` stops a fire that runs longer (daemon default `6h`; `0` = no limit).
- `--catch-up` runs one fire straight away when the daemon starts after that
  schedule's fire was due (it was down, the host rebooted). Without it a missed
  fire is recorded as `skipped`.
- The daemon picks up config changes within a few seconds, or at once on
  `SIGHUP`. On the cron backend run `sync` after editing.

## Config

Schedules live in `{venv}/runspec_scheduler.toml` (override with `--config` or
`$RUNSPEC_SCHEDULER_CONFIG`):

```
[scheduler]
backend = "daemon"        # or "cron" (default)
timeout = "6h"            # default per fire; 0 = no limit
misfire_grace = "5m"      # a fire later than this is recorded as missed
catch_up_window = "1d"    # how far back catch_up looks

[[schedule]]
id = "report"
runnable = "report"
cron = "0 6 * * *"
timeout = "30m"
catch_up = true
```

Change the `[scheduler]` durations with `settings` (the daemon reloads them by
itself; `use` switches the backend):

```bash
runspec-scheduler settings                                  # show them
runspec-scheduler settings --timeout 2h --misfire-grace 10m # change two
```

The daemon writes a heartbeat to `{venv}/logs/scheduler_state.json` and a
rotating log to `{venv}/logs/scheduler.log`; `runspec-scheduler status` reads
both. Only one daemon runs per venv (an flock on `runspec_scheduler.lock`).

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
