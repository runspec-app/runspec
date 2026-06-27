# How many SSH hosts can runspec-console handle?

**Short answer: there is no hard limit.** runspec-console never caps the number
of SSH hosts. You can list as many `[[host]]` entries in `runspec_hosts.toml` as
you like — they are simply read into a list (`hosts.py`). What actually governs
practical capacity is the connection-pool architecture and the background
refresh loop.

## What scales well: the SSH connection pool

The SSH connection pool (`ssh_pool.py`, issue #104) is purpose-built to keep host
count from turning into a connection storm. See
[`docs/design/ssh-connection-pool.md`](design/ssh-connection-pool.md) for the
full design.

- **One long-lived transport per host**, reused across refresh cycles and across
  every SSH operation in the app. Steady state is N idle, keepalive'd transports
  — not N×P fresh handshakes every cycle. New commands run over fresh *channels*
  on the warm transport, so you only pay a handshake when there is no live
  transport yet.
- **`max_concurrent` (default 5)** — a global semaphore that bounds the number of
  *new* handshakes across the entire fleet (`[ssh] max_concurrent`). Even with
  500 hosts, only 5 are mid-handshake at once. This is the knob that protects
  against tripping a bastion's `sshd MaxStartups` / per-source rate limits.
- **`max_sessions` (default 8)** — concurrent channels per transport, kept under
  sshd's typical `MaxSessions` of 10.
- **Per-host exponential backoff + jitter** on connect failure, so a dead host is
  not re-hammered and connects spread out instead of all firing on the same
  instant.

On the SSH side this design comfortably handles **dozens to low hundreds of
hosts** — the handshake rate is bounded by `max_concurrent` regardless of fleet
size.

## The refresh loop: a bounded worker pool

The refresh loop used to be the binding constraint, not SSH. Every cycle
(`[refresh] interval`, default 30 s), `Bridge._refresh_cycle`
(`bridge_internals.py`) ran two phases — a connectivity probe (Phase 1, which
*warms* the pooled transport) and runnable discovery (Phase 2, which *reuses* it)
— and the original implementation spawned **one thread per host per phase**, i.e.
roughly **2N** short-lived OS threads every cycle. That was fine at
dozens-to-hundreds of hosts but became the bottleneck at many hundreds / low
thousands: scheduler/memory pressure from the thread fan-out.

As of runspec-console **0.121.0** both phases fan out over a **bounded
`ThreadPoolExecutor`** instead (the [phase-2 worker
pool](design/refresh-cycle-thread-pool.md)). The refresh loop now holds a small
*constant* thread count regardless of fleet size, set by **`[refresh] workers`**
(default `max(16, 2 × [ssh] max_concurrent)`). Behaviour is identical for small
fleets — with `workers >= 2N` every host still runs concurrently, exactly as
before — and the cap only becomes visible once N exceeds `workers`. The remaining
steady-state cost at scale is resource use, not threads: roughly one socket +
~100 KB per transport (≈1000 FDs and ~100 MB at 1000 hosts).

> A hung host occupies one worker slot for at most the per-op SSH timeout
> (`connect`/`banner`/`auth` for the probe, `ssh_run` timeout for discovery), and
> per-host backoff makes known-dead hosts fail fast — so worst-case cycle latency
> is bounded by `ceil(N / workers) × timeout`, tunable via `workers`.

## Practical guidance

| Fleet size | Behaviour |
|---|---|
| Tens of hosts | Designed sweet spot. The pool keeps things calm; near-zero handshakes once warm. The worker pool runs every host concurrently (`workers >= 2N`). |
| Low hundreds | Works well. SSH stays bounded by `max_concurrent`; the refresh fan-out is bounded by `workers`. |
| Many hundreds / low thousands | Refresh threads stay constant at ~`workers`; SSH stays bounded by `max_concurrent`. The real pressure point is steady-state resource use (FDs/memory per transport), not the fan-out. |

**If you need to push capacity:**

- Raise `[ssh] max_concurrent` *cautiously* — it trades directly against the
  bastion's `MaxStartups` / rate limits. (The `workers` default scales with it.)
- Raise `[refresh] workers` if you want more discovery overlap than the derived
  default gives — though warm-transport discovery is itself bounded per host by
  `[ssh] max_sessions`, so a couple× `max_concurrent` is usually enough.
- Lengthen `[refresh] interval` to reduce how often the cycle runs at all.
- Beyond low thousands, the next lever is steady-state transport resource use
  (longer `idle_ttl` reaping, fewer hosts per console) rather than the fan-out,
  which no longer grows with fleet size.

### Tuning by fleet size

Defaults are tuned for tens of hosts. The table below is a starting point for
larger fleets — measure, then adjust. Every value is in `config.toml` and most
take effect on the next refresh cycle without a restart (the pool rebuilds when
`[ssh]` changes).

| Setting | Section | Default | ~100 hosts | ~1000+ hosts | Why |
|---|---|---|---|---|---|
| `pool` | `[ssh]` | `true` | `true` | `true` | Connection reuse is the whole point — never disable at scale. |
| `max_concurrent` | `[ssh]` | `5` | `8–10` | `15–25` | Global cap on *new* handshakes. Higher = faster recovery after a mass outage; raise only as far as the bastion's `MaxStartups` / rate limits tolerate. |
| `max_sessions` | `[ssh]` | `8` | `8` | `12–16` | Channels per transport. Raise only if rooms/long runs compete with discovery on the same host; keep below the server's `sshd MaxSessions`. |
| `keepalive` | `[ssh]` | `30` | `30` | `60–120` | At 1000 hosts a 30 s keepalive is ~33 packets/s leaving the console. Stretching it cuts bastion-side noise; don't disable over NAT/VPN. This is the SSH application-layer keepalive (`keepalive@openssh.com`) and the primary idle-disconnect defense. |
| `tcp_keepalive` | `[ssh]` | `true` | `true` | `true` | OS-level `SO_KEEPALIVE` on the transport socket — a backstop to `keepalive` that lets the kernel reap a silently-dead peer. Leave on; the SSH keepalive usually fires first. |
| `tcp_keepidle` / `tcp_keepintvl` / `tcp_keepcnt` | `[ssh]` | `60` / `15` / `3` | default | default | The OS keepalive probe schedule (idle before first probe / interval between probes / unanswered probes before the socket is declared dead). Only applied where the OS exposes it. |
| `idle_ttl` | `[ssh]` | `300` | `300` | `600` | How long an unused transport is kept before reaping. Longer is more forgiving on flaky links at the cost of holding more idle FDs. |
| `interval` | `[refresh]` | `30` | `30–45` | `60–120` | How often the cycle runs. Longer = less discovery chatter and fewer connects per minute. |
| `jitter` | `[refresh]` | `3` | `5` | `10–15` | Spreads per-host handshakes across the window so `max_concurrent` isn't the startup bottleneck. |
| `workers` | `[refresh]` | `max(16, 2×max_concurrent)` | default | `2–4 × max_concurrent` | Bounded thread count for the refresh fan-out. The default scales with `max_concurrent`; raise for more discovery overlap, but `[ssh] max_sessions` bounds per-host discovery anyway, so a couple× `max_concurrent` is usually plenty. |

At 1000+ hosts the refresh fan-out no longer grows with fleet size — it holds a
constant ~`workers` threads. The remaining real pressure point is steady-state
resource use (roughly one socket + ~100 KB per transport ⇒ ~1000 FDs and ~100 MB
at 1000 hosts). Raising `interval` and `max_concurrent` is the highest-leverage
config-only change; the bounded fan-out is the
[phase-2 refresh-cycle worker pool](design/refresh-cycle-thread-pool.md)
(shipped in 0.121.0).

## Relevant config (`config.toml`)

```toml
[ssh]
pool           = true   # reuse one transport per host (the whole point)
max_concurrent = 5      # max concurrent NEW handshakes across the fleet
max_sessions   = 8      # max concurrent channels per transport (< sshd MaxSessions)
keepalive      = 30     # s; SSH-layer transport keepalive (0 disables)
tcp_keepalive  = true   # OS SO_KEEPALIVE on the socket (backstop to keepalive)
tcp_keepidle   = 60     # s idle before the first OS keepalive probe
tcp_keepintvl  = 15     # s between OS keepalive probes
tcp_keepcnt    = 3      # unanswered probes before the socket is declared dead
idle_ttl       = 300    # s; reap a transport unused longer than this
backoff_base   = 5      # s; per-host exponential backoff base
backoff_max    = 300    # s; per-host backoff cap

[refresh]
interval = 30   # s between refresh cycles
jitter   = 3    # +/- s of per-host start jitter
workers  = 16   # bounded worker count for the refresh fan-out
                # (default max(16, 2 * [ssh] max_concurrent); explicit value wins)
```
