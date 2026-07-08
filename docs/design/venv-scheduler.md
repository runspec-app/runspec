# venv-based scheduler service (`runspec-scheduler`)

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
| `list` | print schedules as JSON — what the console reads to display/verify remote schedules |
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
parity pattern, so a delegated schedule fires *identically* whether the console or
the host runs it. `tests/test_timing.py::test_parity_with_console` pins this.

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

## Console integration — source of truth = the host

Remote schedules are **shared across operators** and the **host is the source of
truth**. The console is a shared editor/viewer, not the owner:

- **Read.** Each refresh cycle, for every venv that has a `runspec-scheduler`
  (found in `_runnables_cache`), the bridge runs `runspec-scheduler list` over
  SSH and caches the result (`_remote_sched_cache`, keyed by `(host, group)`,
  with an `ok`/`unreachable` status). `get_schedules` merges these (tagged
  `runner="host"`) with the operator's own console schedules (tagged
  `runner="console"`). Remote schedules are **never** written to the operator's
  `runspec_schedules.toml`.
- **Write.** Create/update/delete of a `runner="host"` schedule invokes the
  scheduler's own `add`/`remove` verbs over the existing SSH executor
  (`invoke_runnable`, which already handles subcommands + credential injection).
  A short `Event`-gated wrapper (`_invoke_scheduler_sync`) makes the streaming
  call synchronous so the UI gets success/failure inline.
- **Display.** A **Runner** column badges each row Console vs Remote with a live
  status dot. The "Run from" toggle in the modal is enabled only when the chosen
  host has a `runspec-scheduler` in the **same venv** as the target runnable.

All new bridge methods are private, so the frozen JS API surface is unchanged.

## Secrets — `RUNSPEC_ENV_FILE`, owned by `run_as`

The console isn't present when a remote schedule fires, so selected credentials
must be **persisted on the host**. `runspec-scheduler` leans on the core
`RUNSPEC_ENV_FILE` mechanism (a tier-1 absolute-path env-var override read by
`parse()`, with no reader-side permission check):

1. In the modal, for a runnable that declares `run_as`, the operator picks
   credentials (reusing the Forms env-var derivation). The console passes the
   selected `credential_ids` to `invoke_runnable` (which injects the derived
   **values** into the remote `runspec-scheduler add` process's environment via
   the existing `env(1)`-through-sudo channel) and the derived **names** as
   `--cred-vars` (names aren't secret; values never touch argv).
2. `runspec-scheduler add` writes those vars to a **600 dotenv file owned by the
   `run_as` account**, in that account's own `$HOME`
   (`~/.config/runspec-scheduler/env/<id>.env`). It is written **as** `run_as`
   (`sudo -u <run_as>`, the same passwordless-sudo right that running the runnable
   already needs) with the secret content on **stdin** — so no shared
   world-writable directory and no `chown` (root) is required, and the secret
   never appears in `/proc/<pid>/cmdline`.
3. On fire, the daemon sets `RUNSPEC_ENV_FILE=<that file>` and escalates to
   `run_as`; the runnable reads its own 600 file. One file **per schedule** (not
   one shared file) isolates credentials, makes deletion trivial, and lets
   different schedules use different `run_as` accounts.

### Limitations (documented)

- A remote schedule for a **non-`run_as`** runnable runs as the daemon's own user
  with the daemon's environment (systemd `EnvironmentFile`) — no console
  credential injection; the credential picker is hidden.
- **Secrets now rest on the host** in a 600 file owned by the service account (vs
  the console keyring) — a deliberate trade-off for always-on unattended
  execution.
- Requires the daemon/SSH user to have passwordless `sudo -u <run_as>` — the same
  precondition as running that `run_as` runnable at all (bootstrapped by
  `runspec-linux enable-passwordless-sudo`).

## Not in scope

Scheduling is **runnable-only** (the daemon runs runnables, not agent prompts or
runbooks — those stay console-only, since they need the LLM/agent loop). Remote
schedules are single-host (no group/anyhost/selected fan-out — that's a console
targeting concern).
