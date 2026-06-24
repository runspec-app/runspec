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

    def fake_probe(h, *, jitter=0):
        busy("probe")
        with b._lock:
            b._connected_cache[h["name"]] = True

    def fake_discover(h):
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
