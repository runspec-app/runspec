"""
ssh_pool.py — centralized, reusing SSH connection pool (issue #104).

The console's background refresh used to open a fresh SSH handshake for every
connectivity probe *and* every discovery call, concurrently for every host,
every 30s — a self-generated connection storm that trips bastion
``sshd MaxStartups`` / rate limits and surfaces as "Error reading SSH protocol
banner". See ``docs/design/ssh-connection-pool.md``.

This module keeps one long-lived paramiko transport per host, reused across
refresh cycles and across every SSH operation in the app. A warm transport
serves new commands over fresh *channels* (no new handshake); we only pay a
handshake when there is no live transport yet. Keepalive holds the transport
open against idle-disconnect; per-host exponential backoff stops a dead host
from being re-hammered; a global semaphore bounds concurrent *handshakes* and a
per-transport semaphore bounds concurrent *channels* (``sshd MaxSessions``).

The pool builds connections via ``executor._make_ssh_client`` so all the
existing connection shaping (proxy, ~/.ssh/config, host-key policy, explicit
timeouts) is inherited unchanged. ``executor.ssh_run`` / ``run_remote`` remain
the connect-per-call path used by ``pool = false`` and by key rotation.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Callable

# Pool / refresh defaults. Overridable via [ssh] and [refresh] in config.toml.
DEFAULT_KEEPALIVE = 30  # s; transport keepalive (0 disables)
DEFAULT_MAX_SESSIONS = 8  # max concurrent channels per transport (< sshd's 10)
DEFAULT_IDLE_TTL = 300  # s; reap a transport unused for longer than this
DEFAULT_MAX_CONCURRENT = 5  # max concurrent NEW handshakes across the fleet
DEFAULT_BACKOFF_BASE = 5  # s; per-host exponential backoff base
DEFAULT_BACKOFF_MAX = 300  # s; per-host backoff cap

DEFAULT_REFRESH_INTERVAL = 30  # s between refresh cycles
DEFAULT_REFRESH_JITTER = 3  # +/- s of per-host start jitter


def refresh_settings(refresh_cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve refresh-loop tunables from the ``[refresh]`` config section."""
    cfg = refresh_cfg or {}

    def _int(key: str, default: int) -> int:
        try:
            return int(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "interval": max(1, _int("interval", DEFAULT_REFRESH_INTERVAL)),
        "jitter": max(0, _int("jitter", DEFAULT_REFRESH_JITTER)),
    }


def pool_settings(ssh_cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve pool tunables from the ``[ssh]`` config section (with defaults)."""
    cfg = ssh_cfg or {}

    def _int(key: str, default: int) -> int:
        try:
            return int(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "enabled": cfg.get("pool", True) is not False,
        "keepalive": _int("keepalive", DEFAULT_KEEPALIVE),
        "max_sessions": max(1, _int("max_sessions", DEFAULT_MAX_SESSIONS)),
        "idle_ttl": _int("idle_ttl", DEFAULT_IDLE_TTL),
        "max_concurrent": max(1, _int("max_concurrent", DEFAULT_MAX_CONCURRENT)),
        "backoff_base": _int("backoff_base", DEFAULT_BACKOFF_BASE),
        "backoff_max": _int("backoff_max", DEFAULT_BACKOFF_MAX),
    }


class BackoffError(RuntimeError):
    """Raised when a host is in its connect-backoff window — fail fast, no handshake."""


def _host_key(ssh_target: str, identity_file: str | None, cfg: dict[str, Any]) -> tuple:
    """Identity for a pooled connection.

    A change to any field that shapes the connection (target, key, user, proxy,
    ssh_config opt-in) forces a fresh transport rather than silently reusing one
    built for a different destination/credential.
    """
    return (
        ssh_target,
        identity_file or "",
        cfg.get("user", ""),
        cfg.get("identityFile", ""),
        cfg.get("proxy", ""),
        bool(cfg.get("use_ssh_config")),
    )


class PooledConnection:
    """A single keepalive'd paramiko client, shared across channels.

    Channels (one per in-flight command) are bounded by ``max_sessions`` so we
    never exceed the server's per-connection ``MaxSessions``. The transport is
    never closed by callers — only by the pool (idle reap / shutdown / on a dead
    connection).
    """

    def __init__(self, client: Any, max_sessions: int, clock: Callable[[], float]):
        self._client = client
        self._sessions = threading.BoundedSemaphore(max_sessions)
        self._clock = clock
        self.last_used = clock()

    def is_alive(self, idle_ttl: int) -> bool:
        """True if the transport is still usable and not stale.

        Cheap: ``transport.is_active()`` plus an idle-TTL check. A transport that
        died silently (server reboot, NAT/VPN flap) reports inactive here, so the
        pool transparently rebuilds it on next use.
        """
        transport = self._client.get_transport()
        if transport is None or not transport.is_active():
            return False
        if idle_ttl and (self._clock() - self.last_used) > idle_ttl:
            return False
        return True

    def run(self, command: str, timeout: int) -> tuple[int, str, str]:
        """Run one command over a fresh channel on this transport."""
        self._sessions.acquire()
        try:
            self.last_used = self._clock()
            _, stdout, stderr = self._client.exec_command(command, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            code = stdout.channel.recv_exit_status()
            self.last_used = self._clock()
            return code, out, err
        finally:
            self._sessions.release()

    def open_session(self) -> tuple[Any, Callable[[], None]]:
        """Open a raw channel for streaming (long-running invocations).

        Returns ``(channel, release)``. The caller drives the channel and must
        call ``release()`` when done — it frees the session slot but leaves the
        transport open for reuse. The channel-semaphore slot is held for the full
        lifetime of the streamed command.
        """
        self._sessions.acquire()
        try:
            self.last_used = self._clock()
            transport = self._client.get_transport()
            if transport is None:
                raise OSError("transport closed")
            channel = transport.open_session()
        except BaseException:
            self._sessions.release()
            raise

        released = threading.Event()

        def release() -> None:
            if not released.is_set():
                released.set()
                self.last_used = self._clock()
                self._sessions.release()

        return channel, release

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


class SSHConnectionPool:
    """Thread-safe pool of reused SSH connections, one per host.

    Locking: a per-host lock serialises connect/reconnect for a single host (so
    one host never races itself) while different hosts connect in parallel. A
    short ``_map_lock`` guards the host→connection map and the per-host lock
    registry only.
    """

    def __init__(
        self,
        *,
        settings: dict[str, Any],
        make_client: Callable[..., Any],
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[float, float], float] = random.uniform,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ):
        self._settings = settings
        self._make_client = make_client
        self._clock = clock
        self._rng = rng
        self._on_event = on_event
        self._conns: dict[tuple, PooledConnection] = {}
        self._locks: dict[tuple, threading.Lock] = {}
        self._backoff: dict[
            tuple, tuple[int, float]
        ] = {}  # key → (fails, next_attempt)
        self._map_lock = threading.Lock()
        self._handshakes = threading.BoundedSemaphore(settings["max_concurrent"])

    def _event(self, level: str, message: str) -> None:
        """Surface a pool-lifecycle event (reuse / connect / backoff / reap).

        Forwarded to the Dev tab via the bridge as a ``runspec:ssh`` row so the
        pool's behavior is diagnosable. Never raises — diagnostics must not break
        the connection path.
        """
        if self._on_event is None:
            return
        try:
            self._on_event({"level": level, "logger": "ssh.pool", "message": message})
        except Exception:
            pass

    # ── locking helpers ────────────────────────────────────────────────────────

    def _host_lock(self, key: tuple) -> threading.Lock:
        with self._map_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    # ── connection lifecycle ───────────────────────────────────────────────────

    def _acquire_conn(
        self, ssh_target: str, identity_file: str | None, cfg: dict[str, Any]
    ) -> PooledConnection:
        """Return a live pooled connection, reconnecting/connecting as needed.

        Raises ``BackoffError`` when the host is in its backoff window, or the
        underlying connect exception on a fresh handshake failure.
        """
        key = _host_key(ssh_target, identity_file, cfg)
        idle_ttl = self._settings["idle_ttl"]

        # Fast path: a live connection, no per-host lock needed.
        with self._map_lock:
            conn = self._conns.get(key)
        if conn is not None and conn.is_alive(idle_ttl):
            self._event("DEBUG", f"reusing SSH connection to {ssh_target}")
            return conn

        with self._host_lock(key):
            # Re-check under the lock — another thread may have just connected.
            with self._map_lock:
                conn = self._conns.get(key)
            if conn is not None and conn.is_alive(idle_ttl):
                self._event("DEBUG", f"reusing SSH connection to {ssh_target}")
                return conn
            had_dead = conn is not None
            if conn is not None:
                conn.close()
                with self._map_lock:
                    self._conns.pop(key, None)

            remaining = self._backoff_remaining(key)
            if remaining is not None:
                self._event(
                    "DEBUG",
                    f"skipping {ssh_target}: in connect-backoff ({remaining:.0f}s left)",
                )
                raise BackoffError(f"host in connect-backoff for {remaining:.0f}s")

            conn = self._connect(ssh_target, identity_file, cfg)
            self._event(
                "INFO" if had_dead else "DEBUG",
                f"reconnected to {ssh_target} (previous transport dropped)"
                if had_dead
                else f"opened SSH connection to {ssh_target}",
            )
            with self._map_lock:
                self._conns[key] = conn
            return conn

    def _connect(
        self, ssh_target: str, identity_file: str | None, cfg: dict[str, Any]
    ) -> PooledConnection:
        key = _host_key(ssh_target, identity_file, cfg)
        self._handshakes.acquire()
        try:
            client = self._make_client(ssh_target, identity_file, cfg)
            keepalive = self._settings["keepalive"]
            if keepalive:
                transport = client.get_transport()
                if transport is not None:
                    transport.set_keepalive(keepalive)
        except Exception as exc:
            delay = self._record_failure(key)
            self._event(
                "WARNING",
                f"SSH connect to {ssh_target} failed ({exc}); backing off {delay:.0f}s",
            )
            raise
        finally:
            self._handshakes.release()
        self._record_success(key)
        return PooledConnection(client, self._settings["max_sessions"], self._clock)

    # ── per-host backoff ───────────────────────────────────────────────────────

    def _backoff_remaining(self, key: tuple) -> float | None:
        """Seconds left in the host's backoff window, or None if it's clear."""
        with self._map_lock:
            entry = self._backoff.get(key)
        if entry and self._clock() < entry[1]:
            return entry[1] - self._clock()
        return None

    def _record_success(self, key: tuple) -> None:
        with self._map_lock:
            self._backoff.pop(key, None)

    def _record_failure(self, key: tuple) -> float:
        base = self._settings["backoff_base"]
        cap = self._settings["backoff_max"]
        with self._map_lock:
            fails = self._backoff.get(key, (0, 0.0))[0] + 1
            delay = min(base * (2 ** (fails - 1)), cap)
            delay += self._rng(0, max(1.0, delay * 0.1))  # jitter so retries spread
            self._backoff[key] = (fails, self._clock() + delay)
        return delay

    # ── public API ─────────────────────────────────────────────────────────────

    def run(
        self,
        ssh_target: str,
        command: str,
        *,
        identity_file: str | None = None,
        global_ssh_config: dict[str, Any] | None = None,
        timeout: int = 15,
    ) -> tuple[int, str, str]:
        """Run one command on the host, reusing a pooled transport.

        Mirrors ``executor.ssh_run`` return shape ``(code, out, err)``; on any
        failure returns ``(-1, "", message)`` so callers need no try/except.
        One transparent reconnect is attempted if a *reused* transport turns out
        to be dead at exec time.
        """
        cfg = global_ssh_config or {}
        for attempt in (1, 2):
            try:
                conn = self._acquire_conn(ssh_target, identity_file, cfg)
            except BackoffError as exc:
                return -1, "", str(exc)
            except Exception as exc:
                return -1, "", str(exc)
            try:
                return conn.run(command, timeout)
            except Exception as exc:
                # A reused transport may have died between is_alive() and exec —
                # drop it and retry once with a fresh handshake.
                self._drop(ssh_target, identity_file, cfg)
                if attempt == 2:
                    return -1, "", str(exc)
        return -1, "", "unreachable"

    def open_session(
        self,
        ssh_target: str,
        *,
        identity_file: str | None = None,
        global_ssh_config: dict[str, Any] | None = None,
    ) -> tuple[Any, Callable[[], None]]:
        """Open a streaming channel on a pooled transport (for run_remote).

        Returns ``(channel, release)``. Raises on connect failure / backoff so
        the streaming caller can report it.
        """
        cfg = global_ssh_config or {}
        conn = self._acquire_conn(ssh_target, identity_file, cfg)
        return conn.open_session()

    def connectable(
        self,
        ssh_target: str,
        *,
        identity_file: str | None = None,
        global_ssh_config: dict[str, Any] | None = None,
    ) -> bool:
        """True if a live pooled transport can be obtained — the probe-free
        connectivity check. Replaces the standalone ``ssh host true`` handshake:
        if discovery can get a transport, the host is connected."""
        cfg = global_ssh_config or {}
        try:
            conn = self._acquire_conn(ssh_target, identity_file, cfg)
        except Exception:
            return False
        return conn.is_alive(self._settings["idle_ttl"])

    def _drop(
        self, ssh_target: str, identity_file: str | None, cfg: dict[str, Any]
    ) -> None:
        key = _host_key(ssh_target, identity_file, cfg)
        with self._map_lock:
            conn = self._conns.pop(key, None)
        if conn is not None:
            conn.close()

    def reap(self) -> int:
        """Close transports idle beyond ``idle_ttl``. Returns the count reaped.

        Called once per refresh cycle (cheap, deterministic) instead of running a
        separate timer thread.
        """
        idle_ttl = self._settings["idle_ttl"]
        if not idle_ttl:
            return 0
        with self._map_lock:
            stale = [k for k, c in self._conns.items() if not c.is_alive(idle_ttl)]
            dropped = [self._conns.pop(k) for k in stale]
        # close outside the map lock — close() does network I/O
        for key, conn in zip(stale, dropped):
            conn.close()
            self._event("DEBUG", f"reaped idle SSH connection to {key[0]}")
        return len(dropped)

    def close_all(self) -> None:
        """Close every pooled transport — called on app shutdown."""
        with self._map_lock:
            conns = list(self._conns.values())
            self._conns.clear()
            self._backoff.clear()
        for c in conns:
            c.close()
