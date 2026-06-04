"""
test_refresh_cycle.py — the #104 refresh rework at the Bridge level:

  * one host costs at most ONE handshake per cycle (probe folded into the warm
    transport that discovery reuses) — down from probe + per-path handshakes;
  * the refresh watcher does NOT run before the window attaches (the startup
    terminal-noise fix), and set_window starts it and flushes buffered SSH logs.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from runspec_console.bridge import Bridge
from runspec_console.ssh_pool import SSHConnectionPool, pool_settings


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
        patch("runspec_console.bridge.load_hosts", return_value=[]),
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
            return_value={"interval": 30, "jitter": 0},
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
            return_value={"interval": 30, "jitter": 0},
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
        patch("runspec_console.bridge.load_hosts", return_value=[]),
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
        patch("runspec_console.bridge.load_hosts", return_value=[]),
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
    assert b._pre_window_ssh_logs == []  # drained
    assert any("runspec:ssh" in js for js in dispatched)
