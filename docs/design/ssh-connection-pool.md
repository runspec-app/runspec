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
- `set_keepalive(keepalive)` — SSH application-layer keepalive
  (`keepalive@openssh.com`); keeps NAT/idle-disconnect away. Complemented by an
  OS-level `SO_KEEPALIVE` + probe schedule set on the transport socket in
  `executor._make_ssh_client` (`[ssh] tcp_keepalive` / `tcp_keepidle` /
  `tcp_keepintvl` / `tcp_keepcnt`), a backstop that lets the kernel reap a
  silently-dead peer. Each transport is also tagged with a per-host paramiko log
  channel (`paramiko.transport.<host>`) so Dev-tab SSH events name their host.
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
pool            = true   # false → legacy connect-per-call (escape hatch)
keepalive       = 30     # seconds; 0 disables
max_sessions    = 8      # max concurrent channels per pooled transport
idle_ttl        = 300    # seconds before an unused transport is reaped
connect_retries = 1      # inline retries on a *transient* handshake failure
                         #   (banner/session blip) before recording backoff

[refresh]
interval       = 30   # seconds between cycles
max_concurrent = 5    # max concurrent NEW handshakes across the fleet
jitter         = 3    # +/- seconds of per-host start jitter
backoff_base   = 5    # seconds; exponential per-host on failure
backoff_max    = 300  # seconds cap
```

Existing `[ssh]` connect/banner/auth timeouts and `host_key_checking` are
unchanged.

## Follow-up: key rotation (runspec-console 0.233.0)

Rotation (`bridge_config_ssh._do_rotate_ssh_key`) was listed above as a call
site to route through the pool, but shipped still using connect-per-call
`ssh_run` — **sequentially**, two fresh handshakes per host (push over the old
key, then verify the new key), with no retry and no progress logging. On a large
fleet this reproduced #104's symptoms *inside* rotation: a small, shifting subset
of hosts failing every run with "Error reading SSH protocol banner" /
"No existing session", plus a long, silent run.

0.233.0 addresses this without moving the *verify* off a genuinely fresh
connection (it must actually test the new key):

- **Transient retry.** A shared `executor.is_transient_ssh_error()` classifier
  distinguishes a retryable throttle/blip (banner, no-existing-session, reset,
  timeout, EOF-during-negotiation, administratively-prohibited) from a durable
  fault (auth failure, no route, or a *non-UTF-8* banner = a proxy answering the
  port — deliberately **not** retried). The pool retries the transient case
  inline (`[ssh] connect_retries`) before backoff; rotation retries each
  push/verify step (`[ssh] rotate_retries`, default 2) — but only a
  connection-level failure (`ssh_run` → `-1`), never a command that ran and
  returned non-zero.
- **Bounded concurrency.** Push+verify run across a `ThreadPoolExecutor` sized to
  `[ssh] max_concurrent` (each worker does its host's push then verify
  sequentially, so ≤ `max_concurrent` handshakes are ever in flight — same
  `MaxStartups` guard as the refresh cycle), with per-host start jitter
  (`[ssh] rotate_jitter`) applied only when hosts queue behind the bound.
- **Observability.** A `runspec:rotate_progress` event (per-host done/total) plus
  Dev-tab `ssh.rotate` log lines make a long rotation legible while it runs; the
  Settings → SSH rotation dialog renders the event as a live progress bar.

### Self-cleaning `authorized_keys`

Rotation used to *append* the new key and never remove the old one, so each
rotation grew a host's `authorized_keys` by one runspec key — and a rotation that
failed to commit still left its pushed-but-unused key on every host that
succeeded. The push is now **prune-and-append**, keyed on identifying which keys
the console itself placed:

- **Marker.** Generated public keys carry a stable comment
  (`ssh-ed25519 … runspec-console`, `_KEY_MARKER`). Matching by the leading base64
  is unsafe — every ed25519 key shares the `AAAAC3NzaC1lZDI1NTE5AAAAI` algorithm
  header — so a comment marker is the reliable discriminator.
- **Prune set.** `_build_rotation_push_cmd` builds one host-independent `sh`/`awk`
  command that drops any line which is marker-tagged *or* an exact blob-match to a
  key derived from the console's own backup files (`_stale_key_blobs` reads
  `*.pub.bak.*` and derives from `*.bak.*` privates — this also removes the
  pre-marker keys that carry no comment), **except** the current key and the new
  key, which are always kept. The operator's own keys are never touched (only
  lines that are plainly ours are dropped).
- **No lockout.** The current key is kept until the rotation commits (it's the key
  the live session authenticates with); on commit it becomes a backup, so the
  *next* rotation prunes it. A host converges to just the current + incoming
  runspec key. The rewrite is atomic — temp file in `~/.ssh`, a `test -s` guard so
  it can never truncate `authorized_keys` to empty, and `cat >` back onto the
  original file to preserve its perms / owner / SELinux context.

Verified by `test_rotate_key_cleanup.py`, which runs the generated command
through a real `sh`/`awk` against a seeded `authorized_keys`.

## Follow-up: ad-hoc targets (runspec-console 0.280.0)

The pool above is fleet-scoped — one long-lived transport per host in
`runspec_hosts.toml`. The agent's built-in file tools can also read from an
**ad-hoc** SSH host that is *not* in the fleet (`download_file` / `relay_file`
with `source_target`, optionally via a configured `source_via` jump). That path
(`bridge_transfer._open_source_conn`, `kind == "adhoc"`) never went through the
pool: it built a fresh `executor._make_ssh_client` and its `release` **closed**
the client, so every ad-hoc `download_file` / `relay_file` paid a full handshake.
This is the un-pooled path behind the "built-in tools feel slower than
Forms/agent runs" feedback (feedback item #13).

0.280.0 adds a parallel **`AdhocConnectionPool`** (`ssh_pool.py`) that gives
ad-hoc targets the same warm-transport reuse, without disturbing the fleet pool
or `run_remote` / Forms:

- **Keying / lifecycle.** Ad-hoc hosts aren't in a config file, so the sub-pool
  keys by the *resolved* `(jump, user, host, port, identity)` rather than a host
  name. Ad-hoc targets are unbounded in principle, so it is capped
  (`DEFAULT_ADHOC_MAX_CONNS = 8`) with **least-recently-used eviction** and is
  **idle-reaped every refresh cycle** alongside the fleet pool — an ad-hoc target
  can't leak a standing connection forever. It reuses the fleet pool's
  `PooledConnection` (channel semaphore, `is_alive`, keepalive).
- **Credentials, no stored secret.** An ad-hoc target may auth with a per-call
  password (a Credentials-tab secret resolved fresh at call time, or a typed
  one). The pool never holds it: the caller passes a `connect` closure that
  captures the secret, and the pool invokes it **only on a miss** (no live
  transport) and then discards it. A reused transport is already authenticated,
  so a repeat call touches no secret — nothing secret is retained on the pool
  (`test_adhoc_pool.py::test_password_not_retained_on_pool`).
- **Jump.** A via-jump target tunnels a `direct-tcpip` channel through the
  **fleet pool's warm jump transport** (`open_direct_tcpip`) rather than opening
  a fresh jump client; that channel is released via a `PooledConnection.on_close`
  hook when the target transport is closed/reaped.
- **Scope.** Only the built-in-tool path routes through the sub-pool
  (`_open_source_conn(..., pooled=True)`, the default). The relay/unsaved-host
  **browser** (`_get_adhoc_sftp` / `list_dir_unsaved`) passes `pooled=False`,
  keeping its own dialog-scoped `_adhoc_conns` cache and `close_adhoc` teardown
  exactly as before. Gated on `[ssh] pool` like the fleet pool (built off it, so
  `pool = false` ⇒ connect-per-call as before).

Verified by `test_adhoc_pool.py`: reuse (a second ad-hoc built-in call
handshakes once, not twice), reconnect on a dead transport, LRU eviction, idle
reap, teardown, the via-jump `open_direct_tcpip` tunnel + channel release, the
browse `pooled=False` opt-out, and no-secret-retained.

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
