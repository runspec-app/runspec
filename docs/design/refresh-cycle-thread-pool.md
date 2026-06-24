# runspec-console: bounded refresh-cycle worker pool (phase 2)

Status: **proposed** (not implemented)
Follows up on [`ssh-connection-pool.md`](./ssh-connection-pool.md) (issue #104,
implemented) and the soft-ceiling analysis in
[`../console-ssh-capacity.md`](../console-ssh-capacity.md).

## Problem

The SSH connection pool removed the per-cycle *handshake* storm: steady state is
N warm, keepalive'd transports, and the global `[ssh] max_concurrent` semaphore
bounds new handshakes regardless of fleet size. After that change the binding
constraint on fleet size is no longer SSH — it is the **thread fan-out** in
`Bridge._refresh_cycle` (`bridge.py`).

Every refresh cycle (`[refresh] interval`, default 30 s) the loop creates **one
OS thread per host, twice**:

```python
# bridge.py  _refresh_cycle  (~8290, ~8331)
# Phase 1 — connectivity probe (warms the pooled transport)
probe_threads = [threading.Thread(target=probe, args=(h,), daemon=True)
                 for h in all_hosts if h.get("ssh")]
for t in probe_threads: t.start()
for t in probe_threads: t.join()
self._dispatch("runspec:hosts_updated", {})

# Phase 2 — runnables discovery (reuses the warm transport)
disc_threads = [threading.Thread(target=discover, args=(h,), daemon=True)
                for h in all_hosts]
for t in disc_threads: t.start()
for t in disc_threads: t.join()
```

So each cycle briefly spawns ~**2N** daemon threads. That is fine at tens-to-low
hundreds of hosts, but at many hundreds / low thousands it becomes the
bottleneck: scheduler and memory pressure from spinning up and tearing down ~2N
threads every `interval`. The threads themselves do very little real work — the
pool's `max_concurrent` semaphore already serialises the only expensive part (new
handshakes), so most of those 2N threads spend the cycle **blocked on the
semaphore or on warm-transport I/O**. We are paying full thread-creation cost for
work that a small bounded pool could carry.

## Goal

Cap the number of OS threads the refresh loop uses to a small constant that does
**not** grow with fleet size, while preserving every observable behaviour of the
current cycle:

- Phase ordering: connectivity completes (and `runspec:hosts_updated` fires)
  before discovery runs, because discovery skips hosts the probe marked
  disconnected (`discover` reads `_connected_cache`, `bridge.py` ~8311).
- Per-host start jitter to spread connects (`[refresh] jitter`).
- Dedup of discovered runnables, `_runnables_cache` update +
  `runspec:runnables_updated`, catalog republish, and `pool.reap()` at the end.
- No behaviour change for small fleets (the default must leave tens-of-hosts
  installs identical in practice).

## Design

Replace thread-per-host-per-cycle with a **bounded `ThreadPoolExecutor`** that
fans the same two phases out across a fixed worker count.

### Worker pool

- One pool sized from a new `[refresh] workers` knob (`refresh_settings` in
  `ssh_pool.py`), default derived rather than fixed — e.g.
  `max(16, 2 * [ssh] max_concurrent)`. Rationale: the real concurrency limit on
  the expensive path is already `max_concurrent` (handshakes); a worker count a
  small multiple above it keeps enough runners to overlap warm-transport channel
  work (discovery over an already-open transport) without ever creating N
  threads. Warm-transport discovery is itself bounded per host by
  `[ssh] max_sessions`, so extra workers beyond a couple× `max_concurrent` buy
  little.
- Lifetime: **per-cycle** executor created inside `_refresh_cycle` and shut down
  at the end (simplest; the cost we are removing is N thread *spawns*, and a
  fixed `workers` pool is a constant spawn count). A persistent pool owned by the
  Bridge is a possible later optimisation but adds lifecycle/teardown surface for
  little gain — start per-cycle.

### Phase 1 — connectivity

```python
from concurrent.futures import ThreadPoolExecutor
settings = refresh_settings(self.get_config().get("refresh"))
workers = settings["workers"]
ssh_hosts = [h for h in all_hosts if h.get("ssh")]
with ThreadPoolExecutor(max_workers=workers) as ex:
    list(ex.map(self._probe_one, ssh_hosts))   # _probe_one keeps the jitter + cache write
self._dispatch("runspec:hosts_updated", {})
```

`probe`/`discover` move to small bound methods (`_probe_one`, `_discover_one`) so
they can be submitted; the jitter `sleep` stays at the top of `_probe_one`.
Jitter still matters — with a bounded pool the pool size caps *runner*
concurrency, but `max_concurrent` already caps handshakes; jitter keeps the
within-window arrival smooth. (At large fleets jitter can be reduced since the
pool naturally rate-limits; see the capacity doc's tuning table.)

### Phase 2 — discovery

Same shape over `all_hosts`; the per-host body is unchanged
(`_discover_one` reads `_connected_cache`, runs `discover_remote`/`discover_local`
per venv path under `lock2`, appends to `discovered`). The dedup, cache write,
`runspec:runnables_updated`, `_publish_catalogs`, and `pool.reap()` tail is
unchanged.

### Hung-host containment (important)

Thread-per-host has one property the bounded pool loses: a hung host occupies
*its own* thread and cannot starve the others. With a fixed worker pool, up to
`workers` hung hosts can occupy every slot and stall the whole cycle. Mitigations
(all already partly in place):

- **Per-op SSH timeouts** bound any single task: `connect_timeout` /
  `banner_timeout` / `auth_timeout` for the probe, and `ssh_run`'s `timeout` for
  discovery. A task cannot block longer than these.
- **Per-host backoff** (`BackoffError`) makes known-dead hosts fail *fast* in
  Phase 1, so they don't consume a worker slot for the full timeout on every
  cycle.
- Optionally wrap each `future.result(timeout=...)` with a per-task wall-clock
  ceiling so a pathological hang (timeout not honoured by a wedged socket) frees
  the slot for the next host; on timeout, mark the host disconnected for the
  cycle and move on.

This keeps worst-case cycle latency bounded by
`ceil(N / workers) * max_task_timeout` rather than `max_task_timeout` — an
acceptable trade for the constant thread count, and tunable via `workers`.

## Config

```toml
[refresh]
interval = 30   # unchanged
jitter   = 3    # unchanged
workers  = 16   # NEW: bounded worker count for the refresh fan-out
                #      (default: max(16, 2 * [ssh] max_concurrent))
```

`refresh_settings` gains `workers` with the derived default. No other config
changes; `[ssh]` knobs are untouched.

## Back-compat & rollout

- Behaviour is identical for small fleets (with `workers >= 2N` the pool runs
  every host concurrently, exactly as today). The change is only visible as a
  thread-count ceiling once N exceeds `workers`.
- No persisted-state or schema change; this is purely an internal refactor of
  `_refresh_cycle`. Config Sync is unaffected.
- A safety hatch: if `workers` is set absurdly high it degenerates to today's
  behaviour, so the change cannot regress existing tuned installs.

## Acceptance criteria

- At 1000 simulated hosts the refresh loop holds a constant ~`workers` threads
  (plus the pool's own), not ~2N.
- Cycle output (connected cache, deduped runnables cache, dispatched events,
  catalog republish, reap) is byte-for-byte equivalent to the thread-per-host
  implementation for a fixed host set.
- A single hung host delays at most its own worker slot, not the entire cycle
  beyond the documented `ceil(N/workers) * timeout` bound.

## Testing

- Unit: `refresh_settings` resolves `workers` (explicit, derived default,
  garbage → default). 
- A `_refresh_cycle` test with a stub pool/`discover_*` asserting: phase ordering
  (hosts_updated before discovery), dedup + cache equivalence vs. the old path,
  and that no more than `workers` tasks run concurrently (instrument a counting
  barrier in the stubbed `_probe_one`/`_discover_one`).
- A hung-host test: one stubbed host blocks past its timeout; assert the cycle
  still completes and the host is marked disconnected.

## Out of scope

- Async/`asyncio` rewrite of the SSH layer (paramiko is blocking; the pool +
  bounded executor is the pragmatic shape).
- A persistent Bridge-owned worker pool (possible later; per-cycle pool first).
- Incremental / event-driven discovery (only re-discovering hosts whose venvs
  changed) — a larger redesign, tracked separately if the bounded pool proves
  insufficient beyond low thousands.
