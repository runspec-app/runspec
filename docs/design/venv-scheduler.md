# venv-based scheduler service (`runspec-scheduler`)

> **Status: shelved — standalone-bound.** The `runspec-scheduler` package is
> published and works standalone. A console integration to *delegate* Schedules to
> it shipped in runspec-console 0.162.0 and was **removed in 0.163.0** — see
> [Decision: console delegation removed](#decision-console-delegation-removed).
> The package is kept as the basis for a future **standalone, shared** scheduler,
> not wired into the console today.

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
| `install-service` | write + enable the per-venv systemd `--user` unit |

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
`sudo -iu <acct> runspec-scheduler install-service`. `install-service` is
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
> (worst on the CentOS 7 majority) and the shared-credential blast radius, and
> leans on the runs index for observability. `systemctl --user` timers become the
> v2 backend as the fleet reaches RHEL 9.

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
