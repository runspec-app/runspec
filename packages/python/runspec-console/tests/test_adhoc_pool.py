"""
test_adhoc_pool.py — persistent ad-hoc SSH pool for the built-in file tools.

The agent's built-in file tools (download_file / relay_file) reach an ad-hoc host
(``source_target``, optionally via ``source_via``) through
``_open_source_conn``. That path used to open **and close** a fresh
``_make_ssh_client`` per call — a full handshake every time. It now routes
through the persistent :class:`AdhocConnectionPool`, so a second call to the same
target reuses a warm transport.

Two layers are covered:

* ``AdhocConnectionPool`` itself, against a fake client (reuse, reconnect on a
  dead transport, LRU eviction over cap, idle reap, teardown, on_close for a
  via-jump channel, no secret retained) — mirrors ``test_ssh_pool.py``.
* ``_open_source_conn(..., pooled=True)`` wiring: a second built-in call against
  the same ad-hoc target reuses the pooled transport (one handshake, not two),
  and the browse path (``pooled=False``) is unaffected.
"""

from __future__ import annotations

from runspec_console.bridge import Bridge
from runspec_console.ssh_pool import (
    AdhocConnectionPool,
    adhoc_key_label,
    pool_settings,
)


# ── fakes ─────────────────────────────────────────────────────────────────────


class FakeTransport:
    def __init__(self) -> None:
        self.active = True
        self.keepalive = None
        self.sessions_opened = 0

    def is_active(self) -> bool:
        return self.active

    def set_keepalive(self, interval: int) -> None:
        self.keepalive = interval

    def open_session(self) -> object:
        self.sessions_opened += 1
        return object()


class FakeClient:
    def __init__(self) -> None:
        self.transport = FakeTransport()
        self.closed = False
        self.sftp_opened = 0

    def get_transport(self) -> FakeTransport:
        return self.transport

    def open_sftp(self) -> object:
        self.sftp_opened += 1
        return object()

    def close(self) -> None:
        self.closed = True
        self.transport.active = False


KEY = ("", "svc", "target", 22, "")


def make_pool(overrides=None, **kw) -> AdhocConnectionPool:
    return AdhocConnectionPool(settings=pool_settings(overrides or {}), **kw)


# ── key label (pure) ───────────────────────────────────────────────────────────


def test_key_label_variants():
    assert adhoc_key_label(("", "svc", "box", 22, "")) == "svc@box"
    assert adhoc_key_label(("", "", "box", 2222, "")) == "box:2222"
    assert adhoc_key_label(("jmp", "svc", "box", 22, "")) == "svc@box via jmp"


# ── reuse / connect ─────────────────────────────────────────────────────────────


def test_second_acquire_reuses_transport_without_reconnecting():
    connects = []

    def connect():
        c = FakeClient()
        connects.append(c)
        return c, None

    pool = make_pool()
    a = pool.acquire(KEY, connect)
    b = pool.acquire(KEY, connect)
    assert a is b
    assert len(connects) == 1  # connect closure invoked once — warm transport reused


def test_keepalive_set_on_connect():
    pool = make_pool({"keepalive": 45})
    conn = pool.acquire(KEY, lambda: (FakeClient(), None))
    # the FakeTransport recorded the keepalive the pool applied
    assert conn._client.transport.keepalive == 45


def test_dead_transport_triggers_reconnect():
    connects = []

    def connect():
        c = FakeClient()
        connects.append(c)
        return c, None

    pool = make_pool()
    pool.acquire(KEY, connect)
    connects[0].transport.active = False  # transport dropped
    pool.acquire(KEY, connect)
    assert len(connects) == 2  # rebuilt on the dead transport
    assert connects[0].closed is True  # old one closed


def test_events_new_then_reuse():
    events = []
    pool = make_pool(on_event=events.append)
    pool.acquire(KEY, lambda: (FakeClient(), None))
    pool.acquire(KEY, lambda: (FakeClient(), None))
    msgs = [(e["level"], e["message"]) for e in events]
    assert ("DEBUG", "opened ad-hoc SSH connection to svc@target") in msgs
    assert any(lvl == "DEBUG" and "reusing" in m for lvl, m in msgs)
    assert all(e["logger"] == "ssh.adhoc" for e in events)


# ── on_close (via-jump channel release) ─────────────────────────────────────────


def test_on_close_runs_when_transport_closed():
    released = []

    def connect():
        return FakeClient(), lambda: released.append(True)

    pool = make_pool()
    pool.acquire(KEY, connect)
    pool.close_all()
    assert released == [True]  # the jump-channel release fired alongside client close


# ── bounded LRU eviction ────────────────────────────────────────────────────────


def test_lru_eviction_over_cap():
    now = {"t": 0.0}
    clock = lambda: now["t"]  # noqa: E731
    closed: list[str] = []

    def connect_for(host):
        def connect():
            c = FakeClient()
            orig = c.close

            def close():
                closed.append(host)
                orig()

            c.close = close  # type: ignore[method-assign]
            return c, None

        return connect

    pool = make_pool(max_conns=2, clock=clock)
    pool.acquire(("", "u", "h1", 22, ""), connect_for("h1"))
    now["t"] = 1.0
    pool.acquire(("", "u", "h2", 22, ""), connect_for("h2"))
    now["t"] = 2.0
    pool.acquire(("", "u", "h3", 22, ""), connect_for("h3"))  # over cap → evict LRU
    assert closed == ["h1"]  # h1 was least-recently-used


def test_eviction_never_drops_the_just_acquired_key():
    now = {"t": 0.0}
    clock = lambda: now["t"]  # noqa: E731
    pool = make_pool(max_conns=1, clock=clock)
    pool.acquire(("", "u", "h1", 22, ""), lambda: (FakeClient(), None))
    now["t"] = 1.0
    conn2 = pool.acquire(("", "u", "h2", 22, ""), lambda: (FakeClient(), None))
    # h1 evicted (over cap), h2 (just handed out) kept alive.
    assert conn2.is_alive(pool._settings["idle_ttl"])


# ── idle reaping / drop / teardown ──────────────────────────────────────────────


def test_reap_closes_idle_transports():
    now = {"t": 0.0}
    clock = lambda: now["t"]  # noqa: E731
    c = FakeClient()
    pool = make_pool({"idle_ttl": 10}, clock=clock)
    pool.acquire(KEY, lambda: (c, None))
    now["t"] = 100.0  # past idle_ttl
    assert pool.reap() == 1
    assert c.closed is True


def test_drop_evicts_one():
    c = FakeClient()
    pool = make_pool()
    pool.acquire(KEY, lambda: (c, None))
    pool.drop(KEY)
    assert c.closed is True
    # a subsequent acquire rebuilds
    c2 = FakeClient()
    pool.acquire(KEY, lambda: (c2, None))
    assert c2.closed is False


def test_close_all_teardown():
    clients = [FakeClient(), FakeClient()]
    pool = make_pool()
    pool.acquire(("", "u", "h1", 22, ""), lambda: (clients[0], None))
    pool.acquire(("", "u", "h2", 22, ""), lambda: (clients[1], None))
    pool.close_all()
    assert all(c.closed for c in clients)


# ── _open_source_conn wiring: built-in tool ad-hoc path reuses the pool ─────────


class _ConnBridge:
    """Minimal Bridge slice exercising the real _open_source_conn pooled branch."""

    _open_source_conn = Bridge._open_source_conn
    _open_adhoc_pooled = Bridge._open_adhoc_pooled
    _jump_channel = Bridge._jump_channel

    def __init__(self, adhoc_pool, ssh_pool=None):
        self._adhoc_pool = adhoc_pool
        self._ssh_pool = ssh_pool

    def _host_entry(self, h):  # only used by the via-jump path
        return {"name": h, "ssh": f"deploy@{h}", "identityFile": None}


def test_open_source_conn_jumpless_reuses_pooled_transport(monkeypatch):
    """Two built-in-tool ad-hoc reads against the same target handshake once."""
    import runspec_console.executor as executor_mod

    made: list[str] = []

    def fake_make(ssh_target, identity_file, cfg, password=None):
        made.append(ssh_target)
        return FakeClient()

    monkeypatch.setattr(executor_mod, "_make_ssh_client", fake_make)

    b = _ConnBridge(make_pool())
    source = {"kind": "adhoc", "host": "box", "jump": "", "user": "svc", "port": 22}

    sftp1, sf1, rel1 = b._open_source_conn(source, "pw", {})
    rel1()  # first "download" finishes, frees the sftp slot
    sftp2, sf2, rel2 = b._open_source_conn(source, "pw", {})
    rel2()

    assert made == ["svc@box"]  # exactly one handshake across both calls
    assert sftp1 is not sftp2  # a fresh sftp channel each call…
    # …on the *same* transport (one client built).


def test_pooled_session_factory_opens_on_same_transport(monkeypatch):
    import runspec_console.executor as executor_mod

    client = FakeClient()
    monkeypatch.setattr(executor_mod, "_make_ssh_client", lambda *a, **k: client)

    b = _ConnBridge(make_pool())
    source = {"kind": "adhoc", "host": "box", "jump": "", "user": "svc"}
    _sftp, session_factory, _rel = b._open_source_conn(source, None, {})
    chan, chan_rel = session_factory()
    assert client.transport.sessions_opened == 1  # exec channel on the warm transport
    chan_rel()


def test_open_source_conn_via_jump_uses_pool_direct_tcpip(monkeypatch):
    """The via-jump ad-hoc path tunnels through the fleet pool's warm jump
    transport (open_direct_tcpip), not a fresh jump client, and releases the
    jump channel when the target transport is torn down."""
    import runspec_console.executor as executor_mod

    released = {"chan": 0}

    class _FleetPool:
        def open_direct_tcpip(self, ssh_target, dest_host, dest_port, **kw):
            def release():
                released["chan"] += 1

            return object(), release  # (channel, release)

    target_client = FakeClient()

    class _FakeParamiko:
        class SSHClient:
            def __init__(self):
                self._t = target_client

            def set_missing_host_key_policy(self, *_a):
                pass

            def connect(self, **kw):
                assert kw["sock"] is not None  # tunneled through the jump channel

            def get_transport(self):
                return target_client.transport

            def open_sftp(self):
                return target_client.open_sftp()

            def close(self):
                target_client.close()

    monkeypatch.setitem(__import__("sys").modules, "paramiko", _FakeParamiko)
    monkeypatch.setattr(executor_mod, "_apply_host_key_policy", lambda *a, **k: None)

    pool = make_pool()
    b = _ConnBridge(pool, ssh_pool=_FleetPool())
    source = {"kind": "adhoc", "host": "box", "jump": "jmp", "user": "svc"}
    _sftp, _sf, _rel = b._open_source_conn(source, None, {})
    # tear down the transport → the pooled jump channel is released via on_close.
    pool.close_all()
    assert released["chan"] == 1


def test_browse_path_opts_out_of_pool(monkeypatch):
    """_get_adhoc_sftp (relay browser) passes pooled=False, so it never touches
    the persistent ad-hoc pool — keeping close_adhoc's dialog-scoped cleanup."""
    import runspec_console.executor as executor_mod

    monkeypatch.setattr(executor_mod, "_make_ssh_client", lambda *a, **k: FakeClient())

    class _BrowseBridge:
        _open_source_conn = Bridge._open_source_conn
        _open_adhoc_pooled = Bridge._open_adhoc_pooled

        def __init__(self):
            self.used_pool = False
            # a pool that flags if it's ever touched
            self._adhoc_pool = self

        def acquire(self, *a, **k):  # AdhocConnectionPool.acquire shape
            self.used_pool = True
            raise AssertionError("browse path must not use the persistent pool")

    b = _BrowseBridge()
    sftp, _sf, release = b._open_source_conn(
        {"kind": "adhoc", "host": "box", "jump": ""}, None, {}, pooled=False
    )
    release()
    assert b.used_pool is False


# ── no secret is retained on the pool ───────────────────────────────────────────


def test_password_not_retained_on_pool(monkeypatch):
    """The connect closure captures the password; once the transport is warm the
    pool holds only the PooledConnection — the password lives nowhere on it."""
    import gc

    import runspec_console.executor as executor_mod

    monkeypatch.setattr(executor_mod, "_make_ssh_client", lambda *a, **k: FakeClient())

    b = _ConnBridge(make_pool())
    source = {"kind": "adhoc", "host": "box", "jump": "", "user": "svc"}
    secret = "unlikely-secret-" + "x" * 12
    _sftp, _sf, rel = b._open_source_conn(source, secret, {})
    rel()
    del source, _sftp, _sf, rel
    gc.collect()
    # The secret string must not be reachable from the pooled connection object.
    conn = b._adhoc_pool._conns[("", "svc", "box", 22, "")]
    reachable = {id(o) for o in gc.get_referents(conn, conn.__dict__)}
    assert id(secret) not in reachable
