# runspec-console SSH: centralized connection pool (issue #104)

Status: **implemented** (runspec-console 0.13.0)
Branch: `claude/ssh-errors-runspec-8JjvN`
Supersedes the "Follow-up (not done here)" section of
[`paramiko-errors-in-terminal.md`](./paramiko-errors-in-terminal.md).

## Problem (root cause of the SSH error frequency)

`Bridge._refresh_cycle` runs every 30 s and, *concurrently for every host*,
opens brand-new SSH handshakes:

- **Phase 1 — connectivity probe:** `_check_connected` → `ssh_run("true")`
  = **1 handshake / host** (`bridge.py` `_check_connected`).
- **Phase 2 — discovery:** `discover_remote` → `ssh_run("runspec local
  --format json")` **per venv path** = **P handshakes / host**
  (`bridge.py` `_refresh_cycle`, `discovery.py` `discover_remote`).

Every `ssh_run` is connect → one command → `client.close()` — **zero reuse**
(`executor.py` `ssh_run`). So each host fires **1 + P fresh handshakes every
30 s**, all at once across the fleet, for the life of the app. The other SSH
paths (`_get_remote_history`, `_collect_host_records`, `_run_logs`,
`run_remote`, `_do_rotate_ssh_key`) each open their own ad-hoc connections too.

This concurrent-burst pattern trips bastion `sshd MaxStartups` (default
`10:30:100`), reverse-DNS-on-connect (`UseDNS`), and per-source rate limits —
the server drops/stalls new connections right before sending its banner.

### Evidence (captured 2026-06, corporate Windows host)

See [`errors-on-strart.md`](./errors-on-strart.md). Two interleaved modes:

1. **`UnicodeDecodeError` while reading the banner** — banner bytes are not
   valid UTF-8 (`0x8b` at pos 9 = gzip magic `1f 8b`; `0xb9`). Something on the
   path returned **binary / non-SSH data** — an HTTP CONNECT proxy / TLS
   middlebox / load-balancer answering instead of `sshd`. *Every line appears
   twice, interleaved* → **two transport threads failing at the same instant**
   = the concurrent burst.
2. **Classic banner timeout** (`socket.timeout` → `Error reading SSH protocol
   banner`) — throttled / slow handshakes.

### Incidental startup-noise findings

The #103 fix (`paramiko.propagate = False` + `_ParamikoDevHandler`) genuinely
keeps paramiko errors off the terminal — verified by reproducing the real
startup ordering. But two startup paths remain, because the **first refresh
cycle fires from `Bridge.__init__` before the window is attached**:

- The Bridge's own `logger.warning("dispatch … dropped: no window attached")`
  propagates to runspec-core's root stderr handler → leaks to the terminal.
- SSH errors during the pre-window window are **dropped entirely** —
  `_emit_ssh_log` no-ops when `_window is None`, so they never reach the Dev
  tab either.

## Decision

Build **one centralized SSH connection pool** that *every* SSH path in the
console flows through (persistent transports + keepalive, reused across cycles
and operations). Per-cycle reuse is the degenerate case of this; ad-hoc
connect-per-call remains available via `pool = false`.

### Options considered

| Option | Steady-state handshakes | Covers all SSH paths | Risk | Verdict |
|---|---|---|---|---|
| **A** Per-cycle reuse | 1 / host / 30 s | No (refresh only) | Low | Stepping stone; would need revisiting |
| **B** Persistent pool + keepalive (centralized) | ~0 (keepalives only) | Yes | Medium | **Chosen** |
| **C** Phased (A then B) | — | — | — | Rejected: explicit "revisit later" |

Rationale: SSH is central to the product and the goal is "spotless, don't
revisit." Only a centralized pool removes the every-30 s burst *and* the
ad-hoc connections in the other features. Risk is contained by a clean
abstraction, focused unit tests against a fake transport, and the `pool=false`
escape hatch.

## Design

### `SSHConnectionPool` (new — `runspec_console/ssh_pool.py`)

- Map `host_key → PooledConnection`, guarded by a **per-host lock** (not one
  global lock) so different hosts connect in parallel but one host never races
  itself.
- `host_key` = `(ssh_target, identity_file, frozenset(relevant ssh cfg))` so a
  config change forces a fresh connection.

**`PooledConnection`** wraps a paramiko `SSHClient` / `Transport` with:
- `set_keepalive(keepalive)` — keeps NAT/idle-disconnect away.
- `is_alive()` — cheap `transport.is_active()` + age / idle-TTL checks.
- a **per-transport channel semaphore** (`max_sessions`, default 8, < sshd's
  default `MaxSessions` 10) bounding concurrent channels on one transport.
- `last_used` timestamp for the idle reaper.

**API** (mirrors today's call shapes so call sites change minimally):
- `pool.run(host_key, command, timeout) -> (code, out, err)` — replaces
  `ssh_run`. Acquire-or-create transport, lazily reconnect if dead, run over a
  fresh channel, **return channel to pool (don't close transport)**.
- `pool.open_session(host_key) -> channel` — for streaming `run_remote`
  (long-running invocations); caller closes the *channel*, never the transport.
- `pool.close_host(host_key)` / `pool.close_all()` — teardown.

### Concurrency & backoff

- **Global handshake semaphore** caps concurrent *new connects* across the
  fleet (`[refresh] max_concurrent`, the storm cap). Reusing a warm transport
  does **not** take this semaphore.
- **Per-transport channel semaphore** caps concurrent *channels* per host
  (`MaxSessions` guard) — distinct from the handshake cap.
- **Per-host exponential backoff + jitter** on connect failure: a host in
  backoff fails fast (no handshake attempt) until its next window, so a dead
  bastion is not re-hammered every cycle / re-creates the storm.
- **Idle reaper** closes transports unused beyond `idle_ttl`; `close_all()` on
  app shutdown.

### Refresh cycle rework (`bridge.py`)

- **Drop the standalone `true` probe.** Connectivity = "did we obtain a live
  pooled transport for the host?" Discovery runs over that same transport, so
  it doubles as the probe — one connection serves both phases.
- Per-host jitter on cycle start so probes don't all fire on the same instant.
- Interval becomes configurable (`[refresh] interval`, default 30 s) instead of
  the hard-coded `time.sleep(30)`.

### Call sites routed through the pool

`ssh_run` (becomes a thin wrapper over `pool.run`), `run_remote`
(`pool.open_session`), `_get_remote_history`, `_collect_host_records`,
`_run_logs`, `_do_rotate_ssh_key`.

### Startup-noise fix

- Do **not** run the first refresh cycle from `Bridge.__init__`; start the
  refresh watcher only once `set_window` has attached the window (or gate the
  first cycle on window-ready). This removes the pre-window dispatch warnings.
- Buffer pre-window SSH log records and flush them to the Dev tab when the
  window attaches, instead of dropping them in `_emit_ssh_log`.

### Dev-tab visibility

The pool takes an ``on_event`` callback; the bridge wires it to the existing
``runspec:ssh`` Dev-tab channel (no frontend change — the ``ssh`` category and
renderer already exist). The pool emits one row per lifecycle decision: reuse /
new handshake / reap at DEBUG (shown only under ``--dev``, same gate as the
paramiko capture), reconnect at INFO and connect-backoff at WARNING (always
shown). This makes the storm fix observable — steady-state cycles read as
"reusing connection to …" rather than a wall of new handshakes.

### Non-UTF-8 banner handling

Fewer connections reduce the frequency directly. Where we surface SSH failures
(Dev tab / connection errors), detect the non-UTF-8 / non-`SSH-2.0` banner case
and render a clear one-line cause ("remote did not speak SSH — proxy or
middlebox may be intercepting the connection") rather than a raw traceback.

## Config (config.toml)

```toml
[ssh]
pool         = true   # false → legacy connect-per-call (escape hatch)
keepalive    = 30     # seconds; 0 disables
max_sessions = 8      # max concurrent channels per pooled transport
idle_ttl     = 300    # seconds before an unused transport is reaped

[refresh]
interval       = 30   # seconds between cycles
max_concurrent = 5    # max concurrent NEW handshakes across the fleet
jitter         = 3    # +/- seconds of per-host start jitter
backoff_base   = 5    # seconds; exponential per-host on failure
backoff_max    = 300  # seconds cap
```

Existing `[ssh]` connect/banner/auth timeouts and `host_key_checking` are
unchanged.

## Acceptance criteria (from #104)

- [x] ≤1 new handshake / host / cycle in steady state (pool: ~0 after warm-up).
      Verified by `test_refresh_cycle.py::test_cycle_opens_one_handshake_per_host_even_with_multiple_paths`.
- [x] Refresh interval and concurrency configurable (`[refresh] interval`,
      `max_concurrent`; `[ssh] max_sessions`).
- [x] Connectivity + discovery still correct; a host going offline is detected
      promptly (liveness check + next-cycle reconnect) —
      `test_cycle_marks_unreachable_host_disconnected`, `test_dead_transport_triggers_reconnect`.
- [x] Tests cover the reuse/pool path and the backoff/jitter behavior
      (`test_ssh_pool.py`: reuse, reconnect, channel/handshake bounds,
      exponential backoff, idle reap, teardown).

## Test plan

- Unit: `SSHConnectionPool` against a fake transport — reuse (second `run`
  opens a channel, not a transport), liveness-triggered reconnect, channel
  semaphore bound, idle reaper, `close_all`.
- Unit: per-host backoff schedule + jitter bounds (deterministic with injected
  clock / RNG).
- Unit: refresh cycle issues one connect per host per cycle and folds the probe
  into discovery.
- Unit: startup — no cycle before window; pre-window SSH logs buffered then
  flushed.
- Regression: existing console SSH tests still pass with `pool=true` default
  and with `pool=false`.
