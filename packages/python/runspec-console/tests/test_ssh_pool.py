"""
test_ssh_pool.py — SSHConnectionPool reuse, liveness/reconnect, channel and
handshake bounds, per-host backoff/jitter, idle reaping, teardown (issue #104).

Uses a fake paramiko client/transport (no network) injected via ``make_client``,
and an injected clock + RNG so backoff/jitter are deterministic.
"""

from __future__ import annotations

import threading
import time


from runspec_console.ssh_pool import (
    SSHConnectionPool,
    pool_settings,
)


# ── fakes ─────────────────────────────────────────────────────────────────────


class FakeChannel:
    def __init__(self, out=b"ok", code=0):
        self._out = out
        self._code = code

    def recv_exit_status(self):
        return self._code


class FakeStream:
    def __init__(self, data=b"", channel=None):
        self._data = data
        self.channel = channel

    def read(self):
        return self._data


class FakeTransport:
    def __init__(self):
        self.active = True
        self.keepalive = None
        self.sessions_opened = 0

    def is_active(self):
        return self.active

    def set_keepalive(self, interval):
        self.keepalive = interval

    def open_session(self):
        self.sessions_opened += 1
        return object()


class FakeClient:
    def __init__(self, out=b"ok", code=0):
        self.transport = FakeTransport()
        self.exec_calls = 0
        self.closed = False
        self._out = out
        self._code = code
        self.last_timeout = None

    def get_transport(self):
        return self.transport

    def exec_command(self, command, timeout=None):
        self.exec_calls += 1
        self.last_timeout = timeout
        ch = FakeChannel(self._out, self._code)
        return object(), FakeStream(self._out, ch), FakeStream(b"")

    def close(self):
        self.closed = True
        self.transport.active = False


def make_pool(settings_overrides=None, **kw):
    base = pool_settings(settings_overrides or {})
    return SSHConnectionPool(settings=base, **kw)


# ── Dev-tab event emission ────────────────────────────────────────────────────


def test_events_new_connection_then_reuse():
    events = []
    pool = make_pool(make_client=lambda *a: FakeClient(), on_event=events.append)
    pool.run("u@h", "true")
    pool.run("u@h", "true")
    msgs = [(e["level"], e["message"]) for e in events]
    assert ("DEBUG", "opened SSH connection to u@h") in msgs
    assert any(lvl == "DEBUG" and "reusing" in m for lvl, m in msgs)
    assert all(e["logger"] == "ssh.pool" for e in events)


def test_events_warn_on_connect_failure_with_backoff():
    events = []

    def boom(*a):
        raise OSError("Error reading SSH protocol banner")

    pool = make_pool(make_client=boom, on_event=events.append, rng=lambda a, b: 0.0)
    pool.run("u@h", "true")
    warns = [e for e in events if e["level"] == "WARNING"]
    assert warns and "backing off" in warns[0]["message"]


def test_events_info_on_reconnect():
    events = []
    clients = []

    def make_client(*a):
        c = FakeClient()
        clients.append(c)
        return c

    pool = make_pool(make_client=make_client, on_event=events.append)
    pool.run("u@h", "true")
    clients[0].transport.active = False  # connection dropped
    pool.run("u@h", "true")
    assert any(e["level"] == "INFO" and "reconnected" in e["message"] for e in events)


def test_events_debug_on_reap():
    now = {"t": 0.0}
    events = []
    pool = make_pool(
        {"idle_ttl": 60},
        make_client=lambda *a: FakeClient(),
        clock=lambda: now["t"],
        on_event=events.append,
    )
    pool.run("u@h", "true")
    now["t"] = 120.0
    pool.reap()
    assert any("reaped idle" in e["message"] for e in events)


# ── reuse ─────────────────────────────────────────────────────────────────────


def test_second_run_reuses_transport_no_new_handshake():
    clients = []

    def make_client(target, idf, cfg):
        c = FakeClient()
        clients.append(c)
        return c

    pool = make_pool(make_client=make_client)
    pool.run("u@h", "true")
    pool.run("u@h", "runspec local --format json")

    assert len(clients) == 1  # one handshake total
    assert clients[0].exec_calls == 2  # two commands over the same transport


def test_keepalive_applied_on_connect():
    def make_client(target, idf, cfg):
        return FakeClient()

    pool = make_pool({"keepalive": 45}, make_client=make_client)
    pool.run("u@h", "true")
    # the live connection carries the configured keepalive
    conn = next(iter(pool._conns.values()))
    assert conn._client.transport.keepalive == 45


def test_distinct_hosts_get_distinct_connections():
    clients = []

    def make_client(target, idf, cfg):
        c = FakeClient()
        clients.append(c)
        return c

    pool = make_pool(make_client=make_client)
    pool.run("u@h1", "true")
    pool.run("u@h2", "true")
    assert len(clients) == 2


def test_different_identity_file_forces_new_connection():
    clients = []

    def make_client(target, idf, cfg):
        c = FakeClient()
        clients.append(c)
        return c

    pool = make_pool(make_client=make_client)
    pool.run("u@h", "true", identity_file="/keys/old")
    pool.run("u@h", "true", identity_file="/keys/new")
    assert len(clients) == 2  # rotation: old vs new key are different connections


# ── liveness / reconnect ──────────────────────────────────────────────────────


def test_dead_transport_triggers_reconnect():
    clients = []

    def make_client(target, idf, cfg):
        c = FakeClient()
        clients.append(c)
        return c

    pool = make_pool(make_client=make_client)
    pool.run("u@h", "true")
    # simulate the server dropping the connection
    clients[0].transport.active = False
    pool.run("u@h", "true")
    assert len(clients) == 2  # rebuilt transparently


def test_run_retries_once_when_exec_raises_on_reused_transport():
    state = {"n": 0}

    class FlakyClient(FakeClient):
        def exec_command(self, command, timeout=None):
            state["n"] += 1
            if state["n"] == 1:
                # first transport accepted, then dies at exec time
                raise OSError("socket closed")
            return super().exec_command(command, timeout)

    clients = []

    def make_client(target, idf, cfg):
        c = FlakyClient()
        clients.append(c)
        return c

    pool = make_pool(make_client=make_client)
    code, out, _ = pool.run("u@h", "true")
    assert code == 0 and out == "ok"
    assert len(clients) == 2  # dropped the dead one, reconnected once


# ── connectivity probe replacement ────────────────────────────────────────────


def test_connectable_true_on_live_transport():
    pool = make_pool(make_client=lambda *a: FakeClient())
    assert pool.connectable("u@h") is True


def test_connectable_false_on_connect_failure():
    def boom(*a):
        raise OSError("Error reading SSH protocol banner")

    pool = make_pool(make_client=boom)
    assert pool.connectable("u@h") is False


# ── channel (MaxSessions) bound ───────────────────────────────────────────────


def test_channel_semaphore_bounds_concurrent_sessions():
    pool = make_pool({"max_sessions": 2}, make_client=lambda *a: FakeClient())
    _, rel1 = pool.open_session("u@h")
    _, rel2 = pool.open_session("u@h")
    conn = next(iter(pool._conns.values()))
    assert conn._sessions.acquire(blocking=False) is False  # third would block
    rel1()
    assert conn._sessions.acquire(blocking=False) is True  # freed after release
    conn._sessions.release()
    rel2()


# ── handshake (storm) bound ───────────────────────────────────────────────────


def test_handshake_semaphore_caps_concurrent_connects():
    gate = threading.Event()
    in_connect = threading.Semaphore(0)
    peak = {"n": 0, "cur": 0}
    lock = threading.Lock()

    def make_client(target, idf, cfg):
        with lock:
            peak["cur"] += 1
            peak["n"] = max(peak["n"], peak["cur"])
        in_connect.release()
        gate.wait(2)
        with lock:
            peak["cur"] -= 1
        return FakeClient()

    pool = make_pool({"max_concurrent": 2}, make_client=make_client)
    threads = [
        threading.Thread(target=pool.run, args=(f"u@h{i}", "true")) for i in range(6)
    ]
    for t in threads:
        t.start()
    # let connects pile up against the cap
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join()
    assert peak["n"] <= 2  # never more than max_concurrent handshakes at once


# ── per-host backoff + jitter ─────────────────────────────────────────────────


def test_failing_host_enters_backoff_and_fails_fast():
    now = {"t": 1000.0}
    attempts = {"n": 0}

    def make_client(target, idf, cfg):
        attempts["n"] += 1
        raise OSError("connection refused")

    pool = make_pool(
        {"backoff_base": 10},
        make_client=make_client,
        clock=lambda: now["t"],
        rng=lambda a, b: 0.0,  # no jitter
    )
    code, _, _ = pool.run("u@h", "true")
    assert code == -1 and attempts["n"] == 1

    # within the backoff window: no further handshake attempted
    now["t"] = 1005.0
    code, _, msg = pool.run("u@h", "true")
    assert code == -1 and attempts["n"] == 1  # fast-failed, no new connect
    assert "backoff" in msg

    # after the window: it tries again
    now["t"] = 1011.0
    pool.run("u@h", "true")
    assert attempts["n"] == 2


def test_backoff_is_exponential_and_capped():
    now = {"t": 0.0}

    def boom(*a):
        raise OSError("nope")

    pool = make_pool(
        {"backoff_base": 5, "backoff_max": 30},
        make_client=boom,
        clock=lambda: now["t"],
        rng=lambda a, b: 0.0,  # no jitter — assert the exact base schedule
    )
    key = ("u@h", "", "", "", "", False)
    delays = []
    for _ in range(5):
        now["t"] = pool._backoff.get(key, (0, 0.0))[1]  # jump to the open window
        pool.run("u@h", "true")
        fails, next_at = pool._backoff[key]
        delays.append(next_at - now["t"])
    # 5, 10, 20, 40→cap 30, 30
    assert delays == [5, 10, 20, 30, 30]


def test_success_clears_backoff():
    now = {"t": 0.0}
    state = {"fail": True}

    def make_client(target, idf, cfg):
        if state["fail"]:
            raise OSError("nope")
        return FakeClient()

    pool = make_pool(
        make_client=make_client, clock=lambda: now["t"], rng=lambda a, b: 0
    )
    pool.run("u@h", "true")
    assert pool._backoff  # recorded a failure
    state["fail"] = False
    now["t"] = 10_000
    code, _, _ = pool.run("u@h", "true")
    assert code == 0
    assert not pool._backoff  # cleared on success


# ── idle reaping + teardown ───────────────────────────────────────────────────


def test_reap_closes_idle_transports():
    now = {"t": 0.0}
    pool = make_pool(
        {"idle_ttl": 60}, make_client=lambda *a: FakeClient(), clock=lambda: now["t"]
    )
    pool.run("u@h", "true")
    conn = next(iter(pool._conns.values()))
    now["t"] = 120.0  # exceed idle_ttl
    assert pool.reap() == 1
    assert conn._client.closed is True
    assert not pool._conns


def test_close_all_closes_every_transport():
    closed = []

    def make_client(target, idf, cfg):
        c = FakeClient()
        closed.append(c)
        return c

    pool = make_pool(make_client=make_client)
    pool.run("u@h1", "true")
    pool.run("u@h2", "true")
    pool.close_all()
    assert all(c.closed for c in closed)
    assert not pool._conns


# ── settings resolution ───────────────────────────────────────────────────────


def test_pool_settings_defaults_and_overrides():
    d = pool_settings(None)
    assert d["enabled"] is True
    assert d["keepalive"] == 30 and d["max_sessions"] == 8

    o = pool_settings({"pool": False, "keepalive": 0, "max_sessions": 4})
    assert o["enabled"] is False
    assert o["keepalive"] == 0 and o["max_sessions"] == 4


def test_pool_settings_ignores_garbage_values():
    d = pool_settings({"keepalive": "nope", "max_sessions": None})
    assert d["keepalive"] == 30 and d["max_sessions"] == 8
