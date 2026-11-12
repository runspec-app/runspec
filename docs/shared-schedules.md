# Shared schedules (runspec-scheduler)

`runspec-scheduler` runs the runnables in one venv on a timetable, on the host
itself. Nobody needs a console open. Anyone on the team can see these schedules,
start one now, and say they're handling a failure. Think of it as cron, but
only for runspec runnables, and every fire is recorded.

This page covers how it works, how to set it up, every setting it has, and what
to check when something looks wrong. For the console screens, see
[Automation → All schedules](console-automation.md#all-schedules-this-console-and-every-host).

## Console schedules or shared schedules?

| | Console schedules | Shared schedules |
|---|---|---|
| Where they live | Your console (`runspec_schedules.toml` in its config folder) | The host, in each venv (`{venv}/runspec_scheduler.toml`) |
| Fire when | Your console is open | Always, whether or not a console is open |
| Who sees them | You | Everyone whose console reaches the host, plus anyone on the host |
| Can run | Runnables on any host, prompts, runbooks, saved runs, evals | Runnables installed in that one venv |
| Credentials | Yours, from your keychain | The venv's schedule-only env file |

Use a console schedule for your own reminders and checks. Use a shared schedule
for jobs the team relies on: nightly cleanups, reports and health checks.

### Not in root-owned venvs

A venv that belongs to root (for example one holding `runspec-linux-root`) gets
no shared schedules. Every fire there would run as root, and the console never
becomes root to manage a daemon. Schedule those runnables from your console
instead: the console runs them with `sudo`, the way it runs them from Forms.
The Schedules tab lists root-owned venvs it left out, and Provision and Update
venv don't offer shared schedules for them.

If you already put `runspec-scheduler` in a root-owned venv, take it out on the
host, as root:

```bash
V=/opt/venvs/<root-venv>
sudo $V/bin/runspec-scheduler install-service --remove --write   # stop the daemon and its keeper
sudo $V/bin/runspec-scheduler use daemon                         # take its lines off root's crontab
sudo $V/bin/pip uninstall -y runspec-scheduler
```

## How a fire runs

Every fire, from either backend and from **Run now**, runs the same command:

```bash
{venv}/bin/runspec exec --label sched:<id> --trigger schedule:<cron|daemon|run-now> \
    --lock ~/.cache/runspec-scheduler/<venv>-<hash>/<id>.lock \
    --env-file {venv}/runspec_schedules.env [--timeout <seconds>] \
    <runnable> -- <args>
```

So each fire:

- **is one recorded run** in the venv's runs index, with its exit code and the
  last part of its stdout and stderr. Console History finds every fire of a
  schedule by searching `sched:<id>`;
- **never overlaps itself**. If the last fire is still running, the new one is
  recorded as *skipped* instead of starting a second copy. Each venv has its own
  lock folder (runspec-scheduler 0.7.3), so two venvs with the same schedule id
  don't block each other;
- **loads the schedule-only env file** on top of `.runspec_env` (see
  [Credentials](#credentials-the-schedule-only-env-file));
- **says what started it**. History's **Run by** column shows *cron*, *daemon*
  or *run now*.

## Two backends: cron or daemon

Each venv picks one backend. A venv never uses both, so a schedule can't fire twice.

| | `cron` (default) | `daemon` |
|---|---|---|
| What fires the schedules | Lines in the owner's crontab (`# runspec:sched:<id>`) | One `runspec-scheduler run` process per venv |
| Extra processes | None | The daemon, plus a [keeper](#keepers-what-keeps-the-daemon-running) |
| Cron expressions (`0 2 * * *`, `@daily`) | Yes | Yes |
| Intervals (`every = "15m"`) | No | Yes |
| A time zone other than the host's | No | Yes |
| `last` day of the month (package schedules) | No | Yes |
| Missed fires (host was down) | Lost, as with plain cron | Recorded as *skipped*; `catch_up` runs one on start |
| Default fire timeout | None | 6 hours |
| Picks up a config change | After `runspec-scheduler sync` (the console runs it for you) | Within a second or two, by itself |
| Health shown in the console | Per schedule | Per schedule, plus the daemon (running / stale / stopped, keeper, version) |

**Start with cron.** It has no moving parts and works on any host with a
crontab. Move a venv to the daemon when you need intervals, another time zone,
catch-up after a reboot, or fire timeouts.

To mix them on one host, use two venvs. For example, keep `ops` on cron and put
the interval checks in an `ops-daemon` venv.

## Setting it up

### From the console

- **New venv:** Settings → Jump Hosts → **Provision venv…**. Tick
  **Shared schedules** to install runspec-scheduler, move the venv to the daemon
  and set up its keeper. Leave it off and you can still add
  `runspec-scheduler` under Packages to use the cron backend.
  See [Provision a venv](console-hosts.md#provision-a-venv-from-the-console).
- **Existing venv:** **Update venv…**, then **Shared schedules**:
  - *Leave as they are*;
  - *Move to the daemon*, with a keeper;
  - *Back to crontab*.

  The preview says how many crontab entries would move. See
  [Update an existing venv](console-hosts.md#update-an-existing-venv).
- **Just the package:** install `runspec-scheduler` into the venv from the
  **Marketplace**. The venv stays on the cron backend.

Then add schedules from the **Schedules** tab: **New host schedule**. See
[Adding and editing host schedules](console-automation.md#adding-and-editing-host-schedules).

### From a shell

Run these as the venv owner, with the venv's own binaries:

```bash
/opt/venvs/ops/bin/pip install runspec-scheduler
/opt/venvs/ops/bin/runspec-scheduler add --id nightly-cleanup --runnable cleanup-tmp --cron '30 2 * * *'
/opt/venvs/ops/bin/runspec-scheduler sync          # cron backend: write the crontab lines
/opt/venvs/ops/bin/runspec-scheduler status        # check them
```

To use the daemon instead:

```bash
/opt/venvs/ops/bin/pip install 'runspec-scheduler[supervisor]'   # [supervisor] only if you'll use that keeper
/opt/venvs/ops/bin/runspec-scheduler use daemon                  # takes this venv's lines out of the crontab
/opt/venvs/ops/bin/runspec-scheduler install-service             # shows which keeper it would set up, changes nothing
/opt/venvs/ops/bin/runspec-scheduler install-service --write     # sets it up and starts the daemon
```

!!! note "Run it as the owner"
    The scheduler only runs as the account that owns the venv. Its config,
    crontab lines and daemon all belong to that account. For a venv owned by a
    service account, use `sudo -iu svc-ops /opt/venvs/ops/bin/runspec-scheduler …`.
    The console does the same for you, without a shell (`sudo -n -u <owner>
    {venv}/bin/python …`).

## Keepers: what keeps the daemon running

The daemon is an ordinary process. Something has to start it when the host
boots and start it again if it dies. That something is the **keeper**. A
cron-backend venv has no daemon, so it needs no keeper: cron itself is always
running.

| Keeper | Needs | Restarts a crashed daemon | Survives a reboot | Root needed |
|---|---|---|---|---|
| **systemd --user** (`systemd-user`) | Lingering on for the owner; systemd newer than 219 (not CentOS 7) | Within seconds | Yes | Once, to turn lingering on |
| **supervisord in the venv** (`supervisor`) | `runspec-scheduler[supervisor]` in the venv; a crontab | Within seconds; supervisord itself within 5 minutes | Yes, through `@reboot` | No |
| **Crontab watchdog** (`cron`) | A crontab | Within 5 minutes | Yes, through `@reboot` | No |
| **System unit** (`systemd`) | Root | Within seconds | Yes | Yes (only printed, root installs it) |

`install-service --manager auto` (the console's *keeper picked per host*) picks:

1. **systemd --user**, when lingering is on, systemd is newer than 219 and the
   user's systemd answers;
2. else **supervisord**, when it is installed in the venv;
3. else the **crontab watchdog**.

The result says which keeper it picked and why. Name one with
`--manager systemd-user|supervisor|cron`.

Nothing changes without `--write`. Without it, `install-service` prints the
plan: the files it would write, the crontab lines and commands, and any steps
root has to run.

A venv has one keeper. Installing a different one takes the old one down
first, from runspec-scheduler 0.10.4: it removes its crontab lines, disables its
systemd --user unit or shuts down its supervisord, and stops the daemon it
started. The new keeper then starts the daemon. Before 0.10.4 the old keeper
stayed, and the daemon it had started kept the venv lock. The new keeper's
daemon then exited at once, and systemd took that as finished.

### systemd --user

```ini
# ~/.config/systemd/user/runspec-scheduler-<venv>.service (the main lines)
[Service]
ExecStart={venv}/bin/runspec-scheduler run
Restart=on-failure
RestartSec=5
KillMode=process          # a restart leaves fires already running to finish
```

`install-service --write` writes this unit and runs `systemctl --user enable --now`.
systemd starts the daemon at boot and restarts it 5 seconds after it fails. Check it
with:

```bash
systemctl --user status runspec-scheduler-ops
journalctl --user -u runspec-scheduler-ops      # the daemon's log lines
```

A `--user` unit normally stops when the account logs out. **Lingering** keeps it
running with nobody logged in. Root turns it on once per account:

```bash
sudo loginctl enable-linger svc-ops
loginctl show-user svc-ops -p Linger            # Linger=yes
```

`systemctl --user` needs that account's own user manager. A plain
`sudo -u svc-ops` (which is how the console runs it) doesn't set one up. When
lingering is on, the manager runs all the time and `install-service` finds it in
`/run/user/<uid>` by itself. When lingering is off and the account has no login
session, there is no manager, so `install-service --manager systemd-user`
refuses before writing anything and says to turn lingering on (from 0.10.3).
Before 0.10.3 it wrote the unit, failed to enable it, and left the file behind.

### supervisord in the venv

This keeper needs no root and no system packages. It is two parts, each
watching the next:

```text
crontab (always running)
  └─ every 5 min + at boot: start {venv}/bin/supervisord        ← exits at once if already running
        └─ supervisord: runs runspec-scheduler run, restarts it when it dies
              └─ runspec-scheduler daemon: fires the schedules
```

- **supervisord** is pip-installed into the venv by
  `runspec-scheduler[supervisor]`. Its config is in `{venv}/supervisor/`:
  - `supervisord.conf`;
  - `conf.d/runspec-scheduler.conf`, the program block (`autorestart=true`);
  - its socket and pid file.
- **Two crontab lines** start supervisord at boot (`@reboot`) and every 5
  minutes (`*/5 * * * *`). supervisord refuses to start a second copy while one
  is running, so the 5-minute line does nothing unless supervisord itself has
  died.
- Each line has a tag on the line above it, so you can tell what it is:

  ```cron
  # runspec:scheduler-keeper:/opt/venvs/ops
  @reboot /opt/venvs/ops/bin/supervisord -c /opt/venvs/ops/supervisor/supervisord.conf >/dev/null 2>&1
  # runspec:scheduler-keeper:/opt/venvs/ops
  */5 * * * * /opt/venvs/ops/bin/supervisord -c /opt/venvs/ops/supervisor/supervisord.conf >/dev/null 2>&1
  ```

  Keeper lines carry a different tag from the cron backend's
  `# runspec:sched:<id>` lines, and each tag names its venv. That keeps the
  backend's lines and the keeper's lines separate, even with several venvs in
  one crontab.

To look at it by hand:

```bash
/opt/venvs/ops/bin/supervisorctl -c /opt/venvs/ops/supervisor/supervisord.conf status
/opt/venvs/ops/bin/supervisorctl -c /opt/venvs/ops/supervisor/supervisord.conf restart runspec-scheduler
```

Its own events go to `{venv}/logs/supervisord.log`. The daemon's startup errors
and crash tracebacks go to `{venv}/logs/scheduler.stdout.log`.

`--manager supervisor --system` prints a program block for the host's own
supervisord instead. An admin adds it to `/etc/supervisord.d/` and runs
`supervisorctl update`.

### Crontab watchdog

This is the simplest keeper: two tagged crontab lines that run
`runspec-scheduler run --detach`, at boot and every 5 minutes.

```cron
# runspec:scheduler-keeper:/opt/venvs/ops
@reboot /opt/venvs/ops/bin/runspec-scheduler run --detach >/dev/null 2>&1
# runspec:scheduler-keeper:/opt/venvs/ops
*/5 * * * * /opt/venvs/ops/bin/runspec-scheduler run --detach >/dev/null 2>&1
```

Only one daemon can run per venv, because it holds a lock on
`{venv}/runspec_scheduler.lock`. A start while one is running exits straight
away, so the 5-minute line does nothing until the daemon has died. A crashed
daemon is back within 5 minutes. Fires due in that gap are recorded as *skipped*,
or run once on start for a schedule with `catch_up`.

### Restarting and removing

After pip upgrades runspec-scheduler, the daemon keeps running the old code
until it restarts. `status` and the console then show *restart needed*.

```bash
runspec-scheduler restart --dry-run   # what it would do
runspec-scheduler restart             # restart through the keeper, wait for the first heartbeat
```

`restart` goes through the keeper:

- `systemctl --user restart`;
- `supervisorctl restart`;
- for the crontab watchdog (or no keeper), stop and then `run --detach`.

Fires already running carry on and are still recorded. Config changes never need
a restart. The console's **Restart** / **Start** button and **Update venv…**
(after it upgrades packages) run the same command.

Before you delete a venv, remove its keepers, so nothing tries to start a daemon
from a folder that is gone:

```bash
runspec-scheduler install-service --remove            # what it would remove
runspec-scheduler install-service --remove --write    # remove every keeper, stop the daemon
```

The console's **Deprovision…** does this first. For a cron-backend venv it also
takes the venv's `# runspec:sched:` lines out of the crontab.

## Schedules

A schedule is a `[[schedule]]` entry in `{venv}/runspec_scheduler.toml`. You
rarely edit the file by hand: the console's schedule dialog and
`runspec-scheduler add` write it for you. It looks like this:

```toml
[[schedule]]
id = "nightly-cleanup"
runnable = "cleanup-tmp"
enabled = true
timeout = "30m"
catch_up = true
when = { cron = "30 2 * * *", timezone = "Europe/London" }
args = { older-than = "7d" }

[[schedule]]
id = "tmp-usage"
runnable = "filesystem"
command = ["directory-size"]
when = { every = "15m" }
args = { path = "/tmp" }
```

| Field | What it does | Default |
|---|---|---|
| `id` | Names the schedule. It is the crontab marker and the History label (`sched:<id>`). Letters, digits, `.`, `_` and `-`, up to 64 characters, starting with a letter or digit. | (required) |
| `runnable` | A runnable installed in this venv. | (required) |
| `command` | The subcommand path for a runnable with verbs, e.g. `["directory-size"]` or `["admin", "users"]`. | none |
| `args` | Arguments for the runnable, by name. They are checked against its spec when you save. | none |
| `when.cron` | Five cron fields (`30 2 * * mon-fri`) or `@hourly`, `@daily`, `@midnight`, `@weekly`, `@monthly`, `@yearly`. | one of `cron` or `every` |
| `when.every` | An interval: a number and `s`, `m`, `h` or `d` (`30s`, `15m`, `1h`). Daemon only. | one of `cron` or `every` |
| `when.timezone` | An IANA zone (`Europe/London`, `UTC`) whose clock the schedule keeps. Blank means the venv's zone, else the host's. On cron, only the host's zone works. | the venv's zone |
| `enabled` | `false` pauses it. A paused schedule still runs with **Run now**. | `true` |
| `timeout` | Stops a fire that runs longer than this: seconds, or `90m`, `6h`, `1d`. `0` means no limit. | the venv's `timeout` on the daemon; none on cron |
| `catch_up` | Daemon only. When the daemon starts after this schedule's fire was due (host down, daemon stopped), run it once straight away. Several missed fires become one run. | `false` (missed fires are skipped, as with cron) |
| `run_as`, `become_method` | Fire as another account (`sudo` or `su`). The owner needs passwordless access to that account. | the venv owner |

**Clock changes.** When the clocks go back, a schedule at fixed times (no `*` in
its minute or hour, like `30 1 * * *`) fires once, as cron does. One with `*`
there (like `*/30 * * * *`) keeps firing through both copies of the hour. A time
in the hour the clocks skip fires straight after the jump.

**Time zones.** A crontab line can only fire on the host's clock. On a cron venv,
`add` refuses a schedule in another zone, and `sync` skips it (`status` says
why; the console shows *needs daemon*). To run a 09:00 Sydney job on a host
whose clock is UTC, either:

- move the venv to the daemon and set the schedule's zone; or
- set the venv's zone (`settings --timezone Australia/Sydney`) so every schedule
  without its own zone uses it.

### Schedules a package suggests

A runnable package can suggest schedules in its own `runspec.toml`. A venv it is
installed in can then run them without anyone writing the schedule:

```toml
[cleanup-tmp.meta.scheduler.nightly]       # schedule id: cleanup-tmp.nightly
description = "Remove files older than 7 days from /tmp"
at       = "02:30"           # or "midnight", "noon"
on       = "daily"           # or "weekdays", "weekends", "mon-fri", "sun", "mon,wed", "1", "last"
timezone = "local"           # "local" (the venv's zone, default), "UTC", or a zone name
spread   = "30m"             # each host picks its own fixed minute in 02:30-03:00
args     = {older-than = "7d"}
```

- Each suggestion has exactly one of `at` (with `on`), `every` or `cron`.
- It can also set `command`, `timeout`, `catch_up` and `description`.
- `spread` gives each host a fixed offset worked out from its name and the
  schedule id. That way 70 hosts don't all start at 02:30:00.
- `last` (the month's last day) needs the daemon.

The venv's **package schedules** setting says what happens to them:

| Setting | What happens |
|---|---|
| `offer` (default) | Listed as *offered*, off until someone turns one on |
| `auto` | On as soon as the package is installed |
| `off` | Ignored |

Turning one on (`runspec-scheduler enable --id cleanup-tmp.nightly`, or
**Turn on** in the console) writes only `enabled` for that id. So when a new
package version moves the time, the venv follows. If you change its time
(`add --id cleanup-tmp.nightly …`), your time sticks. `remove --id` drops your
changes and goes back to the package's time. Uninstalling the package drops its
schedules.

The daemon picks up installed, upgraded and removed packages once a minute. On
cron, run `sync` after pip. The Marketplace and Update venv… run it for you.

## Venv settings reference

The `[scheduler]` table at the top of `{venv}/runspec_scheduler.toml` holds the
venv's own settings. Change them with `runspec-scheduler settings`, or in the
console with **Venv settings** on the Schedules tab (or **Settings** on a daemon
strip). The daemon picks up changes by itself. On cron, the console runs `sync`
for you.

```toml
[scheduler]
backend = "daemon"
timeout = "6h"
misfire_grace = "5m"
catch_up_window = "1d"
timezone = "Australia/Sydney"
package_schedules = "offer"
```

| Setting | Console label | What it does | Default |
|---|---|---|---|
| `backend` | Update venv… → Shared schedules | `cron` or `daemon`. Change it with `runspec-scheduler use cron` or `use daemon`: `use cron` writes the crontab lines; `use daemon` removes them. | `cron` |
| `timeout` | Fire timeout | The longest a daemon fire may run before it is stopped. `0` means no limit. A schedule's own `timeout` wins. | `6h` |
| `misfire_grace` | Misfire grace | How late a daemon fire may still start, for example after the host was busy or the daemon was restarting. Later than this, it is recorded as *skipped*. | `5m` |
| `catch_up_window` | Catch-up window | How far back a `catch_up` schedule looks when the daemon starts. A fire missed longer ago than this isn't run. | `1d` |
| `timezone` | Time zone | The zone for schedules with no zone of their own, and for package schedules that say `local`. `settings --timezone host` goes back to the host's zone. | the host's zone |
| `package_schedules` | Package schedules | `offer`, `auto` or `off` (see above). | `offer` |

Durations are seconds or a number with `s`, `m`, `h` or `d`.

When a daemon fire times out, `runspec exec` stops it and records exit code 124.
The daemon then kills anything left in its process group. One caution: a
`run_as` fire that went through `sudo` can leave children running as that other
account.

## Credentials: the schedule-only env file

Host schedules never carry secrets. Put what the fires need in
`{venv}/runspec_schedules.env`:

```bash
# {venv}/runspec_schedules.env — loaded only by scheduled fires
JIRA_URL=https://jira.example.com
JIRA_PERSONAL_TOKEN=…
```

- Every fire loads it on top of `.runspec_env`, so a value here wins. This covers
  cron, daemon and **Run now** fires.
- Nothing else in the venv reads it: console runs, the agent and runs from a
  shell only see `.runspec_env`. A service account's token in this file never
  stands in for an operator who didn't pick their own credential.
- It must be mode `600` and owned by the account the fires run as. A file that
  others can write, or that belongs to another account, is refused: the fire is
  recorded as failed with exit code 78. A missing file is fine.
- Edit it from the console's **Env files** window (switch to *Schedules file*).
  When you pick a runnable in the schedule dialog, it lists the variables the
  runnable reads and whether this file sets them.

This keeps scheduled credentials away from interactive runs. It is not a hard
wall: if scheduled fires and interactive runs use the same account, that account
can read both files.

## Watching schedules and handling failures

On the host:

```bash
runspec-scheduler status         # every schedule: next fire, last runs, failures, who is on it
runspec-scheduler watch          # the same, redrawn every 5 seconds (Ctrl+C to stop)
runspec-scheduler status --json  # what the console reads
runspec-scheduler run-now --id nightly-cleanup --detach   # fire it now, in the background
runspec history jobs             # each scheduled job's state, and who is handling a failure
```

In the console, the **Schedules** tab has four cards: **All**, **This
console**, **Host cron** and **Host daemon**. They cover local and every
connected host in the active profile. Each row shows:

- the schedule's state, next fire and last run;
- the venv owner and the account the schedule runs as;
- **Run now**, plus **Edit**, **Pause** and **Remove** for host schedules.

Expand a row to see recent runs and the last run's output.

Because the schedules are shared, a failing one may be seen by several people.
Tick the failing rows, then:

- **I'm on it** claims them, with an optional note;
- **Dealt with…** marks them handled, with a note saying what was done;
- **Release** takes either mark off.

A mark lasts until the schedule next succeeds. It is stored on the host, so
everyone sees it. A claim with no update for 4 hours counts as a problem again.
See [Who is handling a failure](console-automation.md#who-is-handling-a-failure).

## Files on the host

| File | What it is |
|---|---|
| `{venv}/runspec_scheduler.toml` | The schedules and the `[scheduler]` settings |
| `{venv}/runspec_schedules.env` | Credentials for scheduled fires only |
| `{venv}/runspec_scheduler.lock` | Held by the running daemon, so only one runs per venv |
| `{venv}/runspec_scheduler_backups/` | A copy of the crontab from before each rewrite |
| `{venv}/logs/scheduler.log` | What the daemon did: starts, reloads, fires, warnings |
| `{venv}/logs/scheduler_state.json` | The daemon's heartbeat, every 5 seconds; `status` reads it |
| `{venv}/logs/runspec-scheduler.log` | A record of each `runspec-scheduler` command run |
| `{venv}/logs/scheduler.stdout.log` | The daemon's own output: startup errors and crash tracebacks (supervisord and crontab watchdog keepers) |
| `{venv}/logs/supervisord.log` | supervisord's events (supervisord keeper only) |
| `{venv}/supervisor/` | supervisord's config, socket and pid file (supervisord keeper only) |
| `~/.config/systemd/user/runspec-scheduler-<venv>.service` | The unit (systemd --user keeper only) |
| `~/.cache/runspec-scheduler/<venv>-<hash>/<id>.lock` | Each schedule's lock, in the home of the account it runs as |

The four log files rotate at midnight and keep 7 days. Each fire's own output is
in the venv's run logs, under the runnable's name. The venv's
[log cleanup](log-cleanup.md) policy covers those.

## Troubleshooting

**A daemon venv shows *scheduler stopped*.** Nothing is running the daemon.
Press **Start** in the console, or run `runspec-scheduler restart`. If it stops
again, its startup error is in `{venv}/logs/scheduler.stdout.log` (or
`journalctl --user -u runspec-scheduler-<venv>` under systemd). `status` lists
the keepers the venv has under `keeper`. *No keeper* means nothing will start
the daemon after a reboot: run `install-service --write`.

**A daemon venv shows *scheduler stale*.** The process is there but has stopped
writing heartbeats for more than three intervals, so it is probably stuck.
Restart it, and check `scheduler.log` for the last thing it did.

***restart needed*.** pip installed a different runspec-scheduler since the
daemon started. Run **Restart**. If the flag stays after a restart, an older
daemon that the keeper didn't start (for example one the cron watchdog started
before you switched to systemd --user) holds the venv lock, and each new daemon
quits at once. From 0.10.5 Restart stops that daemon first. On older versions
stop it yourself (`kill <pid>` as the venv owner; `status` shows the pid) and
restart.

**A cron schedule shows *not installed* or *stale line*.** The crontab doesn't
match the config. Press **Sync crontab** (or run `runspec-scheduler sync`).
*Legacy line* means a scheduler older than 0.3.0 wrote the line, so its fires
aren't recorded. A sync fixes that too.

***needs daemon*.** The schedule wants an interval, another time zone or the
`last` day, and the venv is on cron. Move the venv to the daemon with
**Update venv…**, or change the schedule.

**A fire shows *skipped*.** Either the previous fire of the same schedule was
still running, or the daemon was down (or later than `misfire_grace`) at the
time. Expand the row in the console to see which.

**A fire failed with exit code 78.** `runspec_schedules.env` exists but is
group- or world-writable, or owned by another account. Fix it with
`chmod 600` and `chown` to the account the fires run as.

**The console shows *can't read* for a venv.** The console reads each venv's
`status` as the venv owner with `sudo -n -u <owner>`. That needs passwordless
sudo from your SSH login to the owner.

***Failed to connect to bus: No medium found*, or *Failed to connect to user
scope bus … $XDG_RUNTIME_DIR not defined*.** `systemctl --user` found no user
manager for the account. Turn lingering on (`sudo loginctl enable-linger
<owner>`) and try again; from the console that is enough. Or pick the supervisord
or crontab-watchdog keeper, which don't need systemd.

**Restart says systemctl --user can't connect, but the venv uses another
keeper.** A systemd --user install that failed before runspec-scheduler 0.10.3
left its unit file behind, and restart took it for the keeper. Upgrade
runspec-scheduler, which only counts an enabled unit. `install-service --remove
--write` deletes the stray file, or delete
`~/.config/systemd/user/runspec-scheduler-<venv>.service` as the owner.

**CentOS 7.** Its systemd (219) can't run user services reliably, so `auto`
never picks systemd --user there. Use supervisord or the crontab watchdog.

**Scheduled runs count as agent runs in Analytics.** Before runspec-scheduler
0.10.1, crontab lines, daemon fires and Run now set `RUNSPEC_AGENT=1`, so each
fire's record said it was an agent run. Upgrade runspec-scheduler, then restart
the daemon, or on cron press **Sync crontab** so the lines are rewritten.
