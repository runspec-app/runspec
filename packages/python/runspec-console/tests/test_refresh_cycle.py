"""
test_refresh_cycle.py — the #104 refresh rework at the Bridge level:

  * one host costs at most ONE handshake per cycle (probe folded into the warm
    transport that discovery reuses) — down from probe + per-path handshakes;
  * the refresh watcher does NOT run before the window attaches (the startup
    terminal-noise fix), and set_window starts it and flushes buffered SSH logs.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import patch

from runspec_console.bridge import Bridge
from runspec_console.ssh_pool import SSHConnectionPool, pool_settings, refresh_settings


# ── fake paramiko client (counts handshakes) ──────────────────────────────────


class _Chan:
    def recv_exit_status(self):
        return 0


class _Stream:
    def __init__(self, data):
        self._data = data
        self.channel = _Chan()

    def read(self):
        return self._data


class _Transport:
    def is_active(self):
        return True

    def set_keepalive(self, n):
        pass


class _Client:
    def __init__(self, discovery_json):
        self._t = _Transport()
        self._out = json.dumps(discovery_json).encode()

    def get_transport(self):
        return self._t

    def exec_command(self, command, timeout=None):
        return object(), _Stream(self._out), _Stream(b"")

    def close(self):
        self._t.is_active = lambda: False


def _make_bridge():
    with (
        patch("runspec_console.hosts.load_hosts", return_value=[]),
        patch.object(Bridge, "_start_refresh_watcher"),
    ):
        return Bridge()


def _pool_with_counter(discovery_json):
    count = {"n": 0}

    def make_client(target, idf, cfg):
        count["n"] += 1
        return _Client(discovery_json)

    pool = SSHConnectionPool(settings=pool_settings({}), make_client=make_client)
    return pool, count


# ── one handshake per host per cycle ──────────────────────────────────────────


def test_cycle_opens_one_handshake_per_host_even_with_multiple_paths():
    b = _make_bridge()
    # two venv paths on one remote host → old code = 1 probe + 2 discovery = 3
    # handshakes; pooled = 1.
    host = {
        "name": "prod-1",
        "ssh": "deploy@prod-1",
        "runspec_paths": ["/v/a/bin/runspec", "/v/b/bin/runspec"],
    }
    pool, count = _pool_with_counter([])
    b._ssh_pool = pool
    b._hosts = [host]

    with (
        patch.object(b, "_reload_hosts"),
        patch.object(b, "_local_host_entry", return_value={"name": "local"}),
        patch.object(b, "_dispatch"),
        patch.object(b, "get_config", return_value={}),
        # force jitter 0 so the cycle doesn't sleep
        patch(
            "runspec_console.ssh_pool.refresh_settings",
            return_value={"interval": 30, "jitter": 0, "workers": 8},
        ),
    ):
        b._refresh_cycle()

    assert count["n"] == 1  # one transport for the host, reused across paths
    assert b._connected_cache["prod-1"] is True


def test_cycle_marks_unreachable_host_disconnected():
    b = _make_bridge()

    def boom(target, idf, cfg):
        raise OSError("Error reading SSH protocol banner")

    pool = SSHConnectionPool(settings=pool_settings({}), make_client=boom)
    b._ssh_pool = pool
    b._hosts = [
        {"name": "prod-1", "ssh": "deploy@prod-1", "runspec_paths": ["/v/bin/runspec"]}
    ]

    with (
        patch.object(b, "_reload_hosts"),
        patch.object(b, "_local_host_entry", return_value={"name": "local"}),
        patch.object(b, "_dispatch"),
        patch.object(b, "get_config", return_value={}),
        patch(
            "runspec_console.ssh_pool.refresh_settings",
            return_value={"interval": 30, "jitter": 0, "workers": 8},
        ),
    ):
        b._refresh_cycle()

    assert b._connected_cache["prod-1"] is False


# ── startup ordering ──────────────────────────────────────────────────────────


def test_refresh_does_not_start_before_window():
    started = {"n": 0}

    def fake_start(self):
        started["n"] += 1

    with (
        patch("runspec_console.hosts.load_hosts", return_value=[]),
        patch.object(Bridge, "_start_refresh_watcher", fake_start),
    ):
        b = Bridge()
    # __init__ must NOT start the refresh watcher (that's what leaked pre-window
    # dispatch warnings to the terminal).
    assert started["n"] == 0
    assert b._refresh_started is False

    # attaching the window starts it exactly once
    class _Win:
        def evaluate_js(self, js):
            pass

    with patch.object(Bridge, "_start_refresh_watcher", fake_start):
        b.set_window(_Win())
        b.set_window(_Win())  # idempotent
    assert started["n"] == 1
    assert b._refresh_started is True


def test_pool_debug_events_gated_behind_dev_mode():
    """Chatty DEBUG pool rows (reuse / reap) only reach the Dev tab under --dev;
    INFO/WARNING (reconnect / backoff) always do."""
    b = _make_bridge()
    forwarded = []
    with patch.object(b, "_emit_ssh_log", side_effect=forwarded.append):
        b._debug = False
        b._on_pool_event({"level": "DEBUG", "logger": "ssh.pool", "message": "reuse"})
        b._on_pool_event(
            {"level": "WARNING", "logger": "ssh.pool", "message": "backoff"}
        )
        assert [e["message"] for e in forwarded] == ["backoff"]  # DEBUG dropped

        b._debug = True
        b._on_pool_event({"level": "DEBUG", "logger": "ssh.pool", "message": "reuse2"})
        assert forwarded[-1]["message"] == "reuse2"  # now shown


def test_set_window_flushes_buffered_ssh_logs():
    with (
        patch("runspec_console.hosts.load_hosts", return_value=[]),
        patch.object(Bridge, "_start_refresh_watcher"),
    ):
        b = Bridge()
    # simulate pre-window SSH errors captured during the first connects
    b._emit_ssh_log({"level": "ERROR", "message": "banner timeout"})
    assert len(b._pre_window_ssh_logs) == 1

    dispatched = []

    class _Win:
        def evaluate_js(self, js):
            dispatched.append(js)

    b.set_window(_Win())
    assert b._pre_window_ssh_logs == []  # drained (cleared synchronously)
    # Dispatch is async now — delivered by the single ui-dispatch thread — so
    # poll briefly for the flushed buffer to reach the (fake) window.
    import time

    for _ in range(200):
        if any("runspec:ssh" in js for js in dispatched):
            break
        time.sleep(0.01)
    assert any("runspec:ssh" in js for js in dispatched)


# ── phase-2: bounded worker pool ──────────────────────────────────────────────


def test_refresh_settings_workers_resolution():
    # explicit positive override wins
    assert refresh_settings({"workers": 4})["workers"] == 4
    # derived default = max(16, 2 * max_concurrent)
    assert refresh_settings({}, max_concurrent=5)["workers"] == 16
    assert refresh_settings({}, max_concurrent=20)["workers"] == 40
    # garbage / non-positive → derived default
    assert refresh_settings({"workers": "nope"}, max_concurrent=5)["workers"] == 16
    assert refresh_settings({"workers": 0}, max_concurrent=10)["workers"] == 20


def _run_cycle(b, *, workers=4, force=False, discovery_interval=300, connected=None):
    """Drive one _refresh_cycle with discovery returning one runnable per host,
    capturing the cache size at every runnables_updated and every discovery_state
    detail. Returns (sizes, states); the hosts discovered this cycle land on
    ``b.last_discovered``. ``connected`` maps host → probe result (default True)."""
    connected = connected or {}
    sizes: list[int] = []
    states: list[dict] = []

    def snap(ev, detail):
        if ev == "runspec:runnables_updated":
            with b._lock:
                sizes.append(len(b._runnables_cache))
        elif ev == "runspec:discovery_state":
            states.append(dict(detail))

    def fake_discover(h, **_kw):
        discovered.append(h["name"])
        return [{"host": h["name"], "name": f"r-{h['name']}"}]

    def fake_probe(h, **_kw):
        # The probe phase marks a host connected; discovery only runs for
        # connected hosts, so the fake must record the result like the real one.
        with b._lock:
            b._connected_cache[h["name"]] = connected.get(h["name"], True)

    discovered: list[str] = []
    with (
        patch.object(b, "_reload_hosts"),
        patch.object(b, "_local_host_entry", return_value={"name": "local"}),
        patch.object(b, "_dispatch", side_effect=snap),
        patch.object(b, "get_config", return_value={}),
        patch.object(b, "_publish_catalogs"),
        patch.object(b, "_discover_one", side_effect=fake_discover),
        patch.object(b, "_probe_one", side_effect=fake_probe),
        patch(
            "runspec_console.ssh_pool.refresh_settings",
            return_value={
                "interval": 30,
                "jitter": 0,
                "workers": workers,
                "discovery_interval": discovery_interval,
            },
        ),
    ):
        b._refresh_cycle(force=force)
    b.last_discovered = discovered  # type: ignore[attr-defined]
    return sizes, states


def test_refresh_cycle_populates_cache_incrementally_and_marks_ready():
    b = _make_bridge()
    b._hosts = [
        {"name": "h1", "ssh": "u@h1", "runspec_paths": ["/v/bin/runspec"]},
        {"name": "h2", "ssh": "u@h2", "runspec_paths": ["/v/bin/runspec"]},
    ]
    assert b._discovery_ready is False  # blind until the first cycle finishes

    sizes, states = _run_cycle(b)

    # all_hosts = local + h1 + h2 → 3 hosts, each contributing one runnable.
    # Cache grows monotonically as hosts complete (incremental splice), never
    # emptying mid-cycle, ending with every host present.
    assert sizes == sorted(sizes)  # non-decreasing
    assert sizes[-1] == 3
    assert {r["host"] for r in b._runnables_cache} == {"local", "h1", "h2"}

    # Readiness: a 0-of-N kickoff, per-host progress, then ready=True at the end.
    assert states[0] == {"done": 0, "total": 3, "ready": False}
    assert states[-1] == {"done": 3, "total": 3, "ready": True}
    assert [s["done"] for s in states] == sorted(s["done"] for s in states)
    assert b._discovery_ready is True
    assert b.discovery_state() == {"ready": True, "done": 3, "total": 3}


def test_refresh_cycle_prunes_a_removed_host():
    b = _make_bridge()
    b._hosts = [
        {"name": "h1", "ssh": "u@h1", "runspec_paths": ["/v/bin/runspec"]},
        {"name": "h2", "ssh": "u@h2", "runspec_paths": ["/v/bin/runspec"]},
    ]
    _run_cycle(b)
    assert {r["host"] for r in b._runnables_cache} == {"local", "h1", "h2"}

    # h2 leaves the fleet — the canonical end-of-cycle assign drops its entries.
    b._hosts = [{"name": "h1", "ssh": "u@h1", "runspec_paths": ["/v/bin/runspec"]}]
    _run_cycle(b)
    assert {r["host"] for r in b._runnables_cache} == {"local", "h1"}


def test_refresh_cycle_bounds_concurrency_and_orders_phases():
    """The fan-out never runs more than `workers` tasks at once, and the whole
    probe phase (+ hosts_updated) completes before discovery starts."""
    b = _make_bridge()
    hosts = [
        {"name": f"h{i}", "ssh": f"u@h{i}", "runspec_paths": ["/v/bin/runspec"]}
        for i in range(20)
    ]
    b._hosts = hosts

    state = {"active": 0, "peak": 0}
    lock = threading.Lock()
    events: list[str] = []

    def busy(kind: str):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            events.append(kind)
        # tiny spin so tasks actually overlap under the pool
        for _ in range(2000):
            pass
        with lock:
            state["active"] -= 1

    def fake_probe(h, *, jitter=0, **_kw):
        busy("probe")
        with b._lock:
            b._connected_cache[h["name"]] = True

    def fake_discover(h, **_kw):
        busy("disc")
        return []

    with (
        patch.object(b, "_reload_hosts"),
        patch.object(b, "_local_host_entry", return_value={"name": "local"}),
        patch.object(b, "_dispatch", side_effect=lambda ev, _d: events.append(ev)),
        patch.object(b, "get_config", return_value={}),
        patch.object(b, "_publish_catalogs"),
        patch.object(b, "_probe_one", side_effect=fake_probe),
        patch.object(b, "_discover_one", side_effect=fake_discover),
        patch(
            "runspec_console.ssh_pool.refresh_settings",
            return_value={"interval": 30, "jitter": 0, "workers": 3},
        ),
    ):
        b._refresh_cycle()

    assert state["peak"] <= 3  # never more than `workers` concurrent tasks
    # phase ordering: every probe + the hosts_updated dispatch precede any discover
    first_disc = events.index("disc")
    assert "runspec:hosts_updated" in events[:first_disc]
    assert events[:first_disc].count("probe") == 20  # all probes done pre-discovery
    assert "disc" not in events[: events.index("runspec:hosts_updated")]


# ── _refresh_tick shutdown guard ──────────────────────────────────────────────


def test_refresh_tick_stops_quietly_on_interpreter_shutdown():
    """On app close the refresh daemon can reach ThreadPoolExecutor.submit after
    interpreter shutdown began ("cannot schedule new futures ..."). _refresh_tick
    swallows exactly that and returns False so the loop stops without a traceback;
    a normal cycle returns True; any other error still propagates."""
    import pytest

    from runspec_console.bridge_internals import InternalsMixin

    class _FakeSelf:
        def __init__(self, exc):
            self._exc = exc

        def _refresh_cycle(self):
            if self._exc is not None:
                raise self._exc

    shutdown = RuntimeError("cannot schedule new futures after interpreter shutdown")
    assert InternalsMixin._refresh_tick(_FakeSelf(shutdown)) is False
    assert InternalsMixin._refresh_tick(_FakeSelf(None)) is True
    with pytest.raises(RuntimeError):
        InternalsMixin._refresh_tick(_FakeSelf(RuntimeError("something else")))


# ── discovery cadence: discover on (re)connect + periodic, not every cycle ────

_H = [
    {"name": "h1", "ssh": "u@h1", "runspec_paths": ["/v/bin/runspec"]},
    {"name": "h2", "ssh": "u@h2", "runspec_paths": ["/v/bin/runspec"]},
]


def test_second_cycle_skips_fresh_hosts():
    b = _make_bridge()
    b._hosts = list(_H)
    _run_cycle(b)
    assert sorted(b.last_discovered) == ["h1", "h2", "local"]
    assert set(b._discovery_stamp) == {"local", "h1", "h2"}
    sizes, states = _run_cycle(b)
    assert b.last_discovered == []  # nothing stale → no SSH execs
    assert {r["host"] for r in b._runnables_cache} == {"local", "h1", "h2"}  # kept
    assert states[-1] == {"done": 3, "total": 3, "ready": True}


def test_steady_state_cycle_emits_no_runnables_updated():
    b = _make_bridge()
    b._hosts = list(_H)
    _run_cycle(b)
    sizes, states = _run_cycle(b)
    assert sizes == []  # no runnables_updated → the UI doesn't refetch
    assert len(states) == 2  # kickoff + final discovery_state only


def test_force_rediscovers_everything():
    b = _make_bridge()
    b._hosts = list(_H)
    _run_cycle(b)
    _run_cycle(b, force=True)
    assert sorted(b.last_discovered) == ["h1", "h2", "local"]


def test_discovery_interval_elapsed_rediscovers():
    b = _make_bridge()
    b._hosts = list(_H)
    _run_cycle(b)
    with b._lock:  # age every stamp past the interval
        for n, (ts, gen, key) in list(b._discovery_stamp.items()):
            b._discovery_stamp[n] = (ts - 1000, gen, key)
    _run_cycle(b, discovery_interval=300)
    assert sorted(b.last_discovered) == ["h1", "h2", "local"]


def test_discovery_interval_zero_means_every_cycle():
    b = _make_bridge()
    b._hosts = list(_H)
    _run_cycle(b, discovery_interval=0)
    _run_cycle(b, discovery_interval=0)
    assert sorted(b.last_discovered) == ["h1", "h2", "local"]


def test_paths_change_rediscovers_that_host_only():
    b = _make_bridge()
    b._hosts = [dict(h) for h in _H]
    _run_cycle(b)
    b._hosts[0] = {**b._hosts[0], "runspec_paths": ["/v/bin/runspec", "/w/bin/runspec"]}
    _run_cycle(b)
    assert b.last_discovered == ["h1"]


def test_disconnected_then_connected_rediscovers():
    b = _make_bridge()
    b._hosts = list(_H)
    _run_cycle(b)
    # h2 drops: its runnables vanish, its stamp is forgotten, h1 stays fresh.
    _run_cycle(b, connected={"h2": False})
    assert b.last_discovered == []
    assert {r["host"] for r in b._runnables_cache} == {"local", "h1"}
    assert "h2" not in b._discovery_stamp
    # h2 comes back → re-discovered (h1 still fresh).
    _run_cycle(b)
    assert b.last_discovered == ["h2"]
    assert {r["host"] for r in b._runnables_cache} == {"local", "h1", "h2"}


def test_reconnect_bumps_generation_and_rediscovers():
    """A real pool: cycle 1 connects + discovers; the transport dies; cycle 2
    reconnects (new generation) and re-discovers even though the stamp is fresh."""
    b = _make_bridge()
    host = {
        "name": "prod-1",
        "ssh": "deploy@prod-1",
        "runspec_paths": ["/v/bin/runspec"],
    }
    clients = []

    def make_client(target, idf, cfg):
        c = _Client([])  # a valid (empty) `runspec local --format json` reply
        clients.append(c)
        return c

    pool = SSHConnectionPool(settings=pool_settings({}), make_client=make_client)
    b._ssh_pool = pool
    b._hosts = [host]
    discovered: list[str] = []
    real_discover = b._discover_one

    def counting(h, **kw):
        discovered.append(h["name"])
        return real_discover(h, **kw)

    ctx = lambda: (  # noqa: E731
        patch.object(b, "_reload_hosts"),
        patch.object(b, "_local_host_entry", return_value={"name": "local"}),
        patch.object(b, "_dispatch"),
        patch.object(b, "get_config", return_value={}),
        patch.object(b, "_publish_catalogs"),
        patch.object(b, "_discover_one", side_effect=counting),
        patch(
            "runspec_console.ssh_pool.refresh_settings",
            return_value={"interval": 30, "jitter": 0, "workers": 8},
        ),
    )
    from contextlib import ExitStack

    with ExitStack() as st:
        for c in ctx():
            st.enter_context(c)
        b._refresh_cycle()
        assert sorted(discovered) == ["local", "prod-1"]
        assert len(clients) == 1
        assert "prod-1" in b._discovery_stamp
        discovered.clear()
        b._refresh_cycle()  # warm transport, fresh stamp → nothing re-discovered
        assert "prod-1" not in discovered
        assert len(clients) == 1
        clients[0].close()  # the transport drops
        discovered.clear()
        b._refresh_cycle()
        assert len(clients) == 2  # reconnected...
        assert "prod-1" in discovered  # ...and re-discovered


def test_stamps_pruned_for_hosts_leaving_scope():
    b = _make_bridge()
    b._hosts = list(_H)
    _run_cycle(b)
    b._hosts = [_H[0]]
    _run_cycle(b)
    assert set(b._discovery_stamp) == {"local", "h1"}
    assert {r["host"] for r in b._runnables_cache} == {"local", "h1"}


def test_dispatches_coalesced_mid_cycle():
    b = _make_bridge()
    b._hosts = [
        {"name": f"h{i}", "ssh": f"u@h{i}", "runspec_paths": ["/v/bin/runspec"]}
        for i in range(20)
    ]
    with patch(
        "runspec_console.bridge_internals._DISCOVERY_DISPATCH_MIN_INTERVAL", 1e9
    ):
        sizes, states = _run_cycle(b)
    assert sizes == [21]  # exactly one runnables_updated — the final one
    assert len(states) == 2  # kickoff + final
    assert states[-1]["ready"] is True


def test_get_config_read_once_per_cycle():
    b = _make_bridge()
    b._hosts = [
        {"name": f"h{i}", "ssh": f"u@h{i}", "runspec_paths": ["/v/a", "/v/b"]}
        for i in range(5)
    ]
    from unittest.mock import MagicMock

    cfg = MagicMock(return_value={})

    def fake_probe(h, **_kw):
        with b._lock:
            b._connected_cache[h["name"]] = True

    with (
        patch.object(b, "_reload_hosts"),
        patch.object(b, "_local_host_entry", return_value={"name": "local"}),
        patch.object(b, "_dispatch"),
        patch.object(b, "get_config", cfg),
        patch.object(b, "_publish_catalogs"),
        patch.object(b, "_probe_one", side_effect=fake_probe),
        patch("runspec_console.discovery.discover_remote", return_value=[]),
        patch("runspec_console.bridge_internals.discover_remote", return_value=[]),
        patch("runspec_console.bridge_internals.discover_local", return_value=[]),
        patch(
            "runspec_console.ssh_pool.refresh_settings",
            return_value={"interval": 30, "jitter": 0, "workers": 8},
        ),
    ):
        b._refresh_cycle()
    assert cfg.call_count == 1


def test_refresh_hosts_forces_a_cycle():
    b = _make_bridge()
    seen = {}
    with patch.object(b, "_refresh_cycle", side_effect=lambda **kw: seen.update(kw)):
        b.refresh_hosts()
        import time as _t

        for _ in range(100):
            if seen:
                break
            _t.sleep(0.01)
    assert seen == {"force": True}
