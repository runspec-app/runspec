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

## The actual soft ceiling: refresh-loop thread fan-out

The binding constraint is not SSH — it is the refresh loop's **thread fan-out**.
Every cycle (`[refresh] interval`, default 30 s), `Bridge._refresh_cycle`
(`bridge.py`) spawns:

1. one thread per SSH host for connectivity probing (Phase 1, which *warms* the
   pooled transport), then
2. one thread per host for runnable discovery (Phase 2, which *reuses* that warm
   transport).

So each cycle briefly creates roughly **2N OS threads**. That is fine at
dozens-to-hundreds of hosts, but at **many hundreds / low thousands** the
thread-per-host-per-cycle pattern — not the SSH layer — becomes the bottleneck:
scheduler/memory pressure from the thread fan-out, plus N persistent paramiko
transports each holding file descriptors.

## Practical guidance

| Fleet size | Behaviour |
|---|---|
| Tens of hosts | Designed sweet spot. The pool keeps things calm; near-zero handshakes once warm. |
| Low hundreds | Works well. SSH stays bounded by `max_concurrent`; watch refresh-cycle thread count. |
| Many hundreds / low thousands | The per-cycle thread fan-out (~2N threads) becomes the bottleneck, not SSH. |

**If you need to push capacity:**

- Raise `[ssh] max_concurrent` *cautiously* — it trades directly against the
  bastion's `MaxStartups` / rate limits.
- Lengthen `[refresh] interval` to reduce how often the thread fan-out happens.
- Beyond low thousands, the refresh loop would need reworking (e.g. a bounded
  worker pool instead of thread-per-host-per-cycle) rather than any SSH-side
  change.

## Relevant config (`config.toml`)

```toml
[ssh]
pool           = true   # reuse one transport per host (the whole point)
max_concurrent = 5      # max concurrent NEW handshakes across the fleet
max_sessions   = 8      # max concurrent channels per transport (< sshd MaxSessions)
keepalive      = 30     # s; transport keepalive (0 disables)
idle_ttl       = 300    # s; reap a transport unused longer than this
backoff_base   = 5      # s; per-host exponential backoff base
backoff_max    = 300    # s; per-host backoff cap

[refresh]
interval = 30   # s between refresh cycles
jitter   = 3    # +/- s of per-host start jitter
```
