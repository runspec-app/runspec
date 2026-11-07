# venv-based scheduler service (`runspec-scheduler`)

> **Status: revived as the `daemon` backend in runspec-scheduler 0.5.0.** A venv
> now picks `[scheduler] backend = "cron"` (default, see
> [`cron-scheduler.md`](cron-scheduler.md)) or `"daemon"` (this design). The
> 0.5.0 daemon fires through `runspec exec`, holds an flock per venv, writes a
> heartbeat `status` reads, and fixes the 15 robustness issues listed in
> [The 0.5.0 daemon](#the-050-daemon). Console delegation stays removed — see
> [Decision: console delegation removed](#decision-console-delegation-removed).

## The 0.5.0 daemon

What changed from the 0.1–0.4 daemon, and why (each item has a process test in
`tests/test_daemon_process.py` that runs the real daemon and real `runspec exec`):

- **Fires go through `runspec exec --label sched:<id> --trigger schedule --lock
  <lock> [--timeout N]`**, the same command a cron line runs. The daemon starts it
  in its own session with stdin/stdout/stderr on `/dev/null` and only reaps the
  exit code; exec records the run (status, exit code, output) and records an
  overlap with a running fire as `skipped`. Capturing output in the daemon used to
  block on a background child holding stdout, hold big output in memory and fail
  on invalid UTF-8.
- **Timeouts.** `[scheduler] timeout` (default `6h`, `0` = none) or a schedule's
  own `timeout` goes to `exec --timeout`. Exec kills its direct child and exits
  124; the daemon then `killpg`s the fire's session. A backstop at timeout + 120 s
  sends SIGTERM, then SIGKILL 10 s later, if exec itself doesn't return. Limit: a
  `run_as` fire escalated through `sudo` runs as another account the daemon can't
  signal, so a child it leaves behind can outlive the timeout.
- **Missed fires are recorded.** `misfire_grace_time` is 5 min (APScheduler's
  default is 1 s) with `coalesce = true` and `max_instances = 1`. A fire missed
  past the grace — including the run times coalescing drops, which APScheduler
  never reports — becomes a `skipped` runs-index row under `sched:<id>` plus a
  `scheduler.fire_missed` audit event (`records.py`). A fire that can't start
  (runnable missing mid `pip install`, bad args, no `runspec` binary, an error in
  the scheduler) is a `failed` row.
- **`catch_up = true`** on a schedule: when the daemon starts and the schedule's
  last fire (heartbeat `last_fire`, else the runs index) is older than its
  previous due time, one coalesced fire runs at once, if within
  `catch_up_window` (default 1 d). Without it the gap is skipped.
- **Reloads diff the config** by a timing-only fingerprint (add, reschedule,
  remove; untouched jobs keep their next fire), are detected by (mtime ns, size,
  inode) or SIGHUP, and a config that fails to load keeps the last good jobs and
  sets `config_error`. A bad config at start exits 1.
- **One daemon per venv**: `run` takes an flock on `runspec_scheduler.lock` next
  to the config before `rs.parse`, so a second start exits 0 and leaves no run
  record.
- **Heartbeat** `{venv}/logs/scheduler_state.json` (atomic): pid, started,
  heartbeat, backend, config_error, jobs with next fire, running fires, last fire
  per schedule. `status` derives running / stale (age > 3 × interval) / stopped
  and checks `/proc/<pid>/cmdline` against pid reuse.
- **Process hygiene**: the daemon closes its own run record at start (it used to
  stay `running` for weeks), configures logging once, records `scheduler.daemon`
  started/stopped audit events, wraps the loop in try/finally (exit 1 on a crash
  so the keeper restarts it), treats SIGHUP as reload instead of dying, exits
  within a second on SIGTERM while fires keep running in their own sessions
  (`KillMode=process` in the unit), and fills in a standard `PATH` and
  `LANG=C.UTF-8` for fires while dropping inherited `RUNSPEC_*` variables.
- **`use cron|daemon`** switches a venv's backend; switching to the daemon first
  removes this venv's marked crontab lines so the two never both fire.

**0.6.0 — keepers.** `install-service --manager
auto|systemd-user|supervisor|cron|systemd` plans (and with `--write` sets up)
what keeps the daemon running; `auto` picks systemd `--user` when lingering is
on and systemd is newer than 219, else supervisord installed in the venv, else a
crontab watchdog running `run --detach` every 5 minutes. The `systemd` system
unit is only printed for root. `--remove` undoes every keeper and stops the
daemon; `status` lists the keepers. Logic in `runspec_scheduler/keeper.py`;
real-crontab and real-supervisord process tests in `tests/test_daemon_process.py`.
Console 0.380.0 adds it to Provision venv (*Shared schedules*: install, `use daemon`, `install-service --write` as the owner; the per-host pick installs the `[supervisor]` extra so a host without lingering gets supervisord) and has Deprovision run `install-service --remove --write` first, keeping the venv when the daemon won't stop.

## Problem

Console-driven schedules (the Schedules tab) only fire while the desktop
runspec-console is running, and they are **per-operator** (each operator's own
`runspec_schedules.toml`). Teams need **shared, always-on** schedules that run on
a host regardless of whether any console is open — "cron, but limited to runspec
runnables installed in one venv".

## The package

`runspec-scheduler` (dist), `runspec_scheduler` (import), `runspec-scheduler`
(runnable + entry point), env prefix `RUNSPEC_SCHEDULER_`, release tag prefix
`scheduler-v`. It lives under `packages/python/` because — unlike `runspec-room` —
it **is itself a runspec runnable** (ships `runspec.toml`, depends on `runspec`),
so the console can detect it (`runspec local`) and drive it over SSH. It is
`discoverable = false`, so it is hidden from the Forms tab / `runspec serve` MCP
surface (an internal control-plane helper) but still runnable directly.

It is a long-running per-venv daemon managed by `systemctl --user` (like
`runspec-room`), with management verbs layered on the same binary:

| verb | what it does |
|---|---|
| `run` | the daemon (systemd `ExecStart`): APScheduler jobs from the config, exec on fire, mtime hot-reload |
| `add` | append/replace a `[[schedule]]` by `--id`, then hot-reload |
| `remove` | delete a schedule (+ its credential env file), then hot-reload |
| `list` | print schedules as JSON |
| `run-now` | fire one schedule immediately |
| `install-service` | plan, or with `--write` set up, the keeper (systemd `--user`, supervisord in the venv, a cron watchdog, or a system unit printed for root); `--remove` undoes it |

**Config** is venv-scoped at `{sys.prefix}/runspec_scheduler.toml` (matching the
denylist pattern; `--config` / `$RUNSPEC_SCHEDULER_CONFIG` override). Entries are
**local-only** — no host/group targeting (the daemon only runs its own venv on
localhost). Writes are atomic (temp + `os.replace`) so the mtime-poll reader never
sees a half-written file. A rotating audit log lands at `{sys.prefix}/logs/scheduler.log`.

**Timing** (`cron` / `every`) is a deliberate, small **duplicate** of the pure
helpers in `runspec_console.sources.schedule` (`When`, `_parse_every`,
`parse_when`, `when_to_trigger`, `next_fire_time`) — the "Node mirrors Python"
parity pattern, so this daemon fires a schedule *identically* to the way the
console would. `tests/test_timing.py::test_parity_with_console` pins this (it
skips when runspec-console isn't installed).

**Multi-venv hosts** run one daemon per venv; `install-service` names the unit
`runspec-scheduler-<venv>.service`. The daemon excludes `runspec-scheduler` and
the reserved `runspec` from discovery (a scheduler never schedules itself).

**Service accounts + `systemctl --user`.** `systemctl --user` needs the account's
user session (`XDG_RUNTIME_DIR` + D-Bus), which a bare `sudo -u <acct>` doesn't
set up — it fails with *"Failed to connect to bus: No medium found."* The fix is
a **login shell** plus lingering: `sudo loginctl enable-linger <acct>` then
`sudo -iu <acct> runspec-scheduler install-service --manager systemd-user --write`. `install-service` is
resilient here: it always writes the unit file (a plain write, no bus), recovers
`XDG_RUNTIME_DIR` from `/run/user/<uid>` when lingering is already on (so plain
`sudo -u` still works), and on a bus failure returns an actionable `hint` with
the exact login-shell + lingering commands rather than a bare systemd error.

## Decision: console delegation removed

A first attempt integrated this daemon into the runspec-console Schedules tab
(console 0.162.0): a "Run from → Host's scheduler" toggle, the console reading
each host's schedules live via `runspec-scheduler list` and CRUD-ing them over
SSH (`runner="host"` schedules, source of truth = the host), and a credential
picker that shipped the operator's selected credentials to the host's
`RUNSPEC_ENV_FILE`. **This was removed in console 0.163.0.** Two reasons:

1. **Model mismatch.** runspec-console is deliberately *single-user* — one
   operator, one machine, personal state. "Shared, always-on, host-owned"
   schedules are a different ownership model; grafting them onto the console
   (host-as-source-of-truth, shared across all operators) fought that grain.
2. **Trust blast radius (the deciding factor).** Making the console a path that
   routes an *operator's* credentials onto a shared host for a shared daemon is a
   risk we don't want. runspec-console's adoption depends on corporate IT trusting
   it — a trust they can withdraw instantly. One mistake or one bad actor writing
   credentials to a shared resource could taint that trust for everyone. Keeping
   credentials sealed in the operator's own OS keychain — never shipped anywhere
   by the console — protects that posture.

So the console stays purely local (console-fired) schedules, and credential
handling in the console stays keychain-only. This package is retained as the
foundation for a **standalone** scheduler instead.

## Credentials in the daemon — `RUNSPEC_ENV_FILE`, owned by `run_as`

Independent of any console, the daemon can run a `run_as` runnable with
credentials via the core `RUNSPEC_ENV_FILE` mechanism (a tier-1 absolute-path
env-var override read by `parse()`, no reader-side permission check):

- `runspec-scheduler add --cred-vars '["JIRA_USER","JIRA_PASSWORD"]'` captures
  those **named** variables from the `add` process's *own environment* (the caller
  injects the values; only the non-secret names are arguments) and writes them to
  a **600 dotenv file owned by the `run_as` account** in that account's `$HOME`
  (`~/.config/runspec-scheduler/env/<id>.env`). It's written **as** `run_as`
  (`sudo -u <run_as>`, secret on **stdin**) — no shared world-writable dir, no
  `chown`/root, and the secret never lands in `/proc/<pid>/cmdline`.
- On fire, the daemon sets `RUNSPEC_ENV_FILE=<that file>` and escalates to
  `run_as`; the runnable reads its own 600 file. One file **per schedule** isolates
  credentials and makes deletion trivial.

This is the mechanism a future front-end (below) — or an operator scripting `add`
by hand — uses. It does **not** depend on the console, and the console no longer
drives it.

## Future direction — a standalone, shared scheduler

> **Chosen v1 execution model:** see [`cron-scheduler.md`](cron-scheduler.md) —
> a **user-crontab backend** (a translator, not a daemon) replaces the
> APScheduler + `systemctl --user` process below, reusing this package's config /
> timing / `RUNSPEC_ENV_FILE` core. It sidesteps the systemd/lingering fragility
> (worst on older systemd, such as CentOS 7) and the shared-credential blast radius, and
> leans on the runs index for observability. `systemctl --user` timers become the
> v2 backend once hosts run systemd 252+ (RHEL 9).

The natural home for "shared, always-on, cross-host" scheduling is a **standalone
service**, not the single-user console:

- Its **own governed, shared credential store** (audited, owned by the service /
  ops team) — never an individual operator's keychain. This is the crux: shared
  scheduling needs shared, governed secrets, separate from the console's per-user
  trust boundary.
- **Cross-host / cross-venv** targeting and a fleet view — a "shared, always-on
  console" in spirit, rather than a per-venv localhost daemon.
- Likely built *on* this package's daemon/config/timing/envfile core (all
  reusable), with a thin control plane on top.

Until then, `runspec-scheduler` is usable directly on a host (CLI + systemd) for
the single-venv, local case.

## Not in scope (for the current package)

Scheduling is **runnable-only** (the daemon runs runnables, not agent prompts or
runbooks — those need an LLM/agent loop). Config is **local-only** — no
host/group targeting; the daemon runs its own venv on localhost.
