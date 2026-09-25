# Cron-backed shared scheduler (v1)

> **Status: shipping — the cron backend (`runspec-scheduler sync`) landed
> additively in `runspec-scheduler` 0.2.0** (`cronsync` module); the daemon
> (`run` / `install-service`) stays for now and is removed once the cron backend
> is validated in the wild. The chosen v1 execution model for a shared scheduler,
> superseding the long-running daemon in [`venv-scheduler.md`](venv-scheduler.md).
> The package's reusable core (config, timing, `RUNSPEC_ENV_FILE` credential
> mechanism) is kept; the **APScheduler + `systemctl --user` daemon is replaced by
> a translator that writes the OS user's crontab**, so there is no long-running
> process of ours to install, supervise, or upgrade.

## Problem (recap)

Console-driven schedules only fire while the desktop console is open and are
per-operator. Teams want **shared, always-on** schedules that run on a host
regardless of any console — "cron, but limited to runspec runnables installed in
one venv" ([`venv-scheduler.md`](venv-scheduler.md)).

The daemon answer (per-venv `systemctl --user` service) carried two costs that
this design removes:

1. **A process to own** — the `systemctl --user` + `XDG_RUNTIME_DIR` + D-Bus +
   lingering setup is fiddly, and it is *worst* on the fleet we target (see
   [Fleet reality](#fleet-reality-centos-7--rhel-9)).
2. **A shared-credential blast radius** — a shared daemon that holds every
   schedule's secrets is exactly the trust risk that got the console→daemon
   delegation removed in console 0.163.0.

## v1 decision

Ship the **smallest thing that validates the architecture in the wild**, then
regroup on usage feedback:

- **Backend: the invoking OS user's crontab only.** No root, no `/etc/cron.d`, no
  systemd. Uniform and reliable across the whole fleet during its migration.
- **No central credential store.** Secrets reach a runnable through the existing
  `RUNSPEC_ENV_FILE` read mechanism, from a per-schedule `0600` dotenv the
  operator/ops provisions. Distributing those files across the fleet is
  **explicitly an external process** in v1 (config management / secret sync).
- **Runnable actions only.** `prompt` / `runbook` / `eval` (agent turns) need an
  LLM/agent runtime and stay a console concern, matching the scheduler's existing
  scope.
- **Calendar schedules only** (5-field cron / `@nickname`). Arbitrary `every N`
  intervals are a **non-goal for v1** — cron cannot express them (see
  [Non-goals](#non-goals-for-v1)).

## Architecture — a translator, not a process

The scheduler stops being a daemon and becomes a **crontab translator**. A
schedule definition is rendered into a single marked crontab line whose command
is the venv's `runspec run …`; **cron is the executor**. Three pieces already
exist:

- **The cron primitive** — `runspec-linux-core.cron` (`list_cron` / `add_cron` /
  `remove_cron`) does `crontab -l` / `crontab -` read-modify-write as any user via
  the `run_as` escalation (`sudo -n -u` / `su`), with comment-above-job,
  idempotent add, remove-by-match, and newline-injection guards. That is exactly
  "install/list/remove a marked schedule line as user X".
- **The timing + config + env-file core** in `runspec-scheduler` (`When`,
  `parse_when`, the per-schedule `RUNSPEC_ENV_FILE` writer) — reused unchanged.
- **The runs index** (`runspec_runs.db`, `runspec` 0.57–0.58) — the observability
  channel, so the translator needs no feedback loop of its own (below).

The management surface is the same shape the `cron` runnable already exposes
(`add` / `list` / `remove` / `run-now`), invokable locally, over SSH, or as
`runspec serve` leaf tools — the "stdio interface to cron" framing. The daemon
verbs (`run`, `install-service`) and the systemd unit are dropped.

### The crontab line

Each schedule becomes one line (plus its marker comment), for example:

```cron
# runspec:sched:nightly-backup
0 2 * * *  RUNSPEC_ENV_FILE=$HOME/.config/runspec-scheduler/env/nightly-backup.env flock -n $HOME/.cache/runspec-scheduler/nightly-backup.lock /opt/venvs/ops/bin/runspec run backup --target prod >/dev/null 2>&1
```

- **Absolute venv binary** — cron runs with a minimal environment (no PATH, no
  venv activation, `cwd=$HOME`), so the command is the venv's own
  `{sys.prefix}/bin/runspec`.
- **`flock -n <lock>`** — overlap guard (see below).
- **`RUNSPEC_ENV_FILE=…` inline** — per-job credentials without leaking to other
  jobs (a bare `NAME=val` crontab line would apply to every following job); the
  runnable reads its own `0600` dotenv.
- **`>/dev/null 2>&1`** — cron's default is to mail stdout/stderr; we discard it
  and let runspec's own logging write the record (which also populates the runs
  index).

### Identity: a marker block, because user cron has no `.d`

Stock cron (cronie on EL7/EL9) has **no per-user drop-in directory** — a user's
crontab is a single monolithic file edited atomically via `crontab`. So "our own
file" isn't available; instead each managed line carries a
`# runspec:sched:<id>` marker comment (the same marker discipline as the SSH-key
and denylist work). CRUD reads the whole table, edits only our marked lines, and
writes it back — the operator's hand-written cron lines are always preserved. The
`cron` core's structured parse + comment-above-job + remove-by-match already does
this.

**Caveat (document it):** the whole-file read-modify-write is race-prone —
`crontab` has no cross-tool locking, so a concurrent `crontab -e` (or two of our
writes) can clobber. For a single controlling writer per user it is a non-issue;
worth a note and, later, a lock file around our own writes.

### Observability comes free from the runs index

Cron fires and forgets — normally the downside versus a daemon (no run cards, no
status). The runs index closes that loop: a cron-fired `runspec run X` writes its
`begin`/`finish` rows to `{venv}/logs/runspec_runs.db` via the core lifecycle
hook. So without any live process:

- **what ran / succeeded / how long** → the console History tab already reads it
  (locally and over SSH),
- **what's running now** → the live running-rows view,
- **why a log is missing** → the log-prune provenance note.

The scheduler contributes execution; the index contributes truth.

### Overlap detection

Cron will happily start a run while the previous one is still going. Two answers,
both without a daemon:

- **`flock -n <lock>`** on the line (shown above) — the classic cron idiom, zero
  new code; a still-running prior instance makes the new one exit immediately.
- Later, a `runspec run --skip-if-running` guard that consults the index's live
  running-rows before starting — more informative (it can record "skipped, prior
  still running"), reusing the liveness logic already built.

v1 uses `flock`.

## Credentials in v1 — `RUNSPEC_ENV_FILE`, owned by `run_as`, provisioned out of band

runspec has **one** dotenv-reading mechanism (`env.py`): `RUNSPEC_ENV_FILE`
points `parse()` at an absolute dotenv path, whose values merge into the
environment (real env still wins) and feed normal arg resolution. There is **no
reader-side permission check** — runspec reads whatever the path resolves to; the
`0600` + ownership guarantee is the *writer's* responsibility.

The scheduler already implements the correct writer: `add --cred-vars
'["JIRA_USER","JIRA_PASSWORD"]'` captures those named variables from its own
environment (only the non-secret names are arguments) and writes a **`0600`
dotenv owned by `run_as`**, written *as* `run_as` (`sudo -u`, secret on **stdin**
so it never lands in `/proc/<pid>/cmdline`), one file per schedule. On fire the
crontab line sets `RUNSPEC_ENV_FILE` to that file.

**What v1 does not solve:** distributing and rotating those `0600` files across
hundreds of hosts. That is an **external process** (config management, a secret
sync, or the console's per-host SSH tooling). runspec provides the *read* and the
correct *local write*; fleet-scale secret management is deliberately out of scope
until feedback says a governed central store is warranted. That is the "regroup"
trigger.

## Fleet reality: CentOS 7 → RHEL 9

Many fleets still run CentOS 7 alongside RHEL 9. This directly
argues for user cron over the tempting `systemctl --user` timer alternative:

| Approach | Root-free? | CentOS 7 (systemd 219) | RHEL 9 (systemd 252) |
|---|---|---|---|
| **user crontab** | yes | rock-solid (cronie) | rock-solid |
| `systemctl --user` timer | yes | fragile — D-Bus / `XDG_RUNTIME_DIR` / lingering weak on 219 | solid |
| `/etc/systemd/system` timer + `User=` | **no (root)** | solid | solid |

- Timer *units* exist on both, but the **non-root** path (`systemctl --user`)
  requires a per-user systemd instance + lingering, which is fragile on systemd
  219 (the exact D-Bus/lingering pain the daemon design hit) and only reliable on
  252. The robust timer path (`/etc/systemd/system` + `User=`) needs root.
- **User crontab is the only option that is identical and reliable on both**, and
  needs neither root nor D-Bus/lingering — decisive for a "validate in the wild"
  v1 spanning the migration.

**EL7 cronie caveats to design around:**

- **Do not rely on `CRON_TZ`.** Timezone handling in older cronie is unreliable;
  schedule in the host's **system timezone (or UTC)** and document it. (Verify on
  a real EL7 box before assuming anything richer.)
- **No timer/`RandomizedDelaySec` jitter** on 219 anyway; to spread fleet load,
  hand-randomize the schedule minute (the console already does this for its
  schedules — carry the convention over).

## Non-goals for v1

Deferred deliberately, each with a clear later home:

- **`every N` interval schedules** — cron is 5-field wall-clock at 1-minute
  granularity and cannot express arbitrary intervals. v1 accepts only
  cron-expressible schedules (`cron` / `@nickname`) and rejects `every` on the
  cron backend.
- **AI actions** (`prompt` / `runbook` / `eval`) — need the agent runtime.
- **Central / governed credential store** and **fleet-scale secret
  distribution** — external in v1.
- **`systemctl --user` timers** — the natural **v2**, adopted as RHEL 9 becomes
  the fleet majority (where they are reliable) and which *also* solves the
  `every`-interval gap (`OnUnitActiveSec`). A clean progression: cron backend
  now, add a systemd-user-timer backend behind the same translator later.
- **Cross-host orchestration engine** — per-host crontabs + writing marked lines
  over SSH to each target already gives fleet scheduling with no central process;
  the per-host runs index + History gives the fleet view. No workflow engine.

## Where it lives

Evolve `runspec-scheduler` rather than start a new package: keep the reusable
`config` / `timing` / `envfile` core and the CRUD verbs; **drop** the daemon
`run` verb, `install-service`, and the systemd unit; **add** a `sync`/`apply`
step that renders the config's `[[schedule]]` entries into marked crontab lines
via the `cron` core (installing `runspec-linux-core` as a dependency, or vendoring
the small cron helper to keep the dependency surface honest). Config stays
venv-scoped `runspec_scheduler.toml`:

```toml
[[schedule]]
id = "nightly-backup"
runnable = "backup"
args = { target = "prod" }
cron = "0 2 * * *"
run_as = "svc-ops"
cred_vars = ["JIRA_USER", "JIRA_PASSWORD"]
```

`sync` reconciles: add lines for new ids, replace changed ones, remove ours that
are no longer in the config — all keyed by the `runspec:sched:<id>` marker.

## Regroup criteria (what v1 is meant to teach)

Ship, then watch for the signals that justify the deferred work:

- Operators asking for `every N` / sub-daily intervals → prioritise the
  systemd-user-timer backend (and time it with the EL9 majority).
- Secret-distribution toil at fleet scale, or a request for audited shared
  secrets → design the governed central credential store.
- Timezone/DST confusion from system-TZ scheduling → revisit `CRON_TZ` support
  per host capability, or move interval/calendar handling to timers.

See also: [`venv-scheduler.md`](venv-scheduler.md) (the shelved daemon and the
credential/timing core this reuses).
