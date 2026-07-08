# runspec-scheduler

**Cron, but limited to runspec runnables installed in one venv.**

A long-running per-venv scheduler daemon (`systemctl --user`) that runs runnables
installed in *its own venv* on a cron/interval cadence — independent of any desktop
console. It is itself a runspec runnable, so `runspec-console` can detect it and
manage its schedules over SSH, but `discoverable = false` keeps it out of the Forms
tab / `runspec serve` MCP surface.

## Why

A console-driven schedule only fires while the desktop console is open, and it's
per-operator. `runspec-scheduler` gives you **shared, always-on** schedules that live
on the host and run whether or not a console is connected.

## Install

```bash
# Into the same venv whose runnables you want to schedule:
pip install runspec-scheduler

# Install + enable the systemd --user service for this venv (as yourself):
runspec-scheduler install-service
loginctl enable-linger "$USER"     # survive logout
```

`install-service` writes `~/.config/systemd/user/runspec-scheduler-<venv>.service`
(one daemon per venv) and runs `systemctl --user enable --now`. To do it by hand, see
`deploy/runspec-scheduler.service`.

### Running the daemon as a service account

`systemctl --user` needs the account's own user session (`XDG_RUNTIME_DIR` +
D-Bus). A bare `sudo -u <acct>` doesn't set that up, so systemctl fails with
*"Failed to connect to bus: No medium found."* Run from a **login shell** and
enable lingering:

```bash
sudo loginctl enable-linger serviceacc              # persistent user systemd, survives logout
sudo -iu serviceacc runspec-scheduler install-service   # -i = login shell (sets up the session)
sudo -iu serviceacc systemctl --user status runspec-scheduler-<venv>
```

The `-i` (login shell) is what wires up `XDG_RUNTIME_DIR`, D-Bus, and `HOME` for
the account. If lingering is already on, `install-service` also recovers
`XDG_RUNTIME_DIR` from `/run/user/<uid>` on its own; otherwise it returns a hint
with the exact commands above (the unit file is written either way, so you can
finish with a manual `systemctl --user enable --now`).

## Use

```bash
runspec-scheduler add --id nightly --runnable backup --cron '0 2 * * *'
runspec-scheduler add --id poll   --runnable healthcheck --every 15m --args '{"level":"warn"}'
runspec-scheduler list
runspec-scheduler run-now --id poll
runspec-scheduler remove --id poll
```

- Exactly one of `--cron` (5-field cron) or `--every` (`15m` / `1h` / `1d`).
- `--args` is a JSON object passed to the target runnable.
- Adding/removing a schedule hot-reloads the running daemon (mtime poll, ≤ 5s).

## Config

Schedules live in `{venv}/runspec_scheduler.toml` (override with `--config` or
`$RUNSPEC_SCHEDULER_CONFIG`). The daemon keeps a rotating audit log at
`{venv}/logs/scheduler.log`; live logs via `journalctl --user -u runspec-scheduler-<venv> -f`.

## Credentials (`run_as` runnables)

`runspec-console` can attach service-account credentials to a remote schedule. For a
runnable that declares `run_as`, the selected credentials are written to a **600 env
file owned by the `run_as` account** in that account's home; the daemon points
`RUNSPEC_ENV_FILE` at it and escalates to `run_as` when firing. Credentials are never
placed on a command line. This requires the daemon/SSH user to have passwordless
`sudo -u <run_as>` (the same precondition as running that `run_as` runnable at all).

Runnables **without** `run_as` run as the daemon's own user with the daemon's
environment (systemd `EnvironmentFile`) — no console credential injection.
