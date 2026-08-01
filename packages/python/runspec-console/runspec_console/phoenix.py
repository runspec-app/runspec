"""
phoenix.py — a console-managed local Arize Phoenix server for private,
zero-config trace analysis.

Phoenix (``arize-phoenix``) is an OpenTelemetry OTLP collector *and* an
LLM-aware trace-analysis web UI in one process, both served on a single port
(6006 by default). The console already emits OTLP spans (``observability.py``);
this module lets the console *launch and manage* a local Phoenix instance so
those spans land in a fully local, private trace explorer — nothing leaves the
machine, no external backend, no API keys.

Design mirrors ``observability.py`` / ``sources/schedule.py``: **opt-in behind
the ``phoenix`` extra, best-effort, never raises into the caller.** ``phoenix``
is a *lazy import* (probed only in ``available()``), so importing this module
never drags Phoenix's heavy dependency tree into a base install. The server runs
as a managed subprocess (clean to kill on stop / atexit), its lifecycle modelled
on the schedule source: a handle held under a lock, nulled on stop, guarded
against double-start, torn down best-effort.

See ``docs/console-phoenix.md``.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# How long start() waits for the collector/UI port to accept connections before
# giving up and reporting the server didn't come up.
_START_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 0.25


def available() -> tuple[bool, str]:
    """``(True, "")`` when ``arize-phoenix`` imports; else ``(False, reason)``.

    Pure probe (mirrors ``observability.available()`` /
    ``ScheduleSource.available()``); drives the Phoenix tab's "install the extra"
    hint. Never raises."""
    try:
        import phoenix  # noqa: F401
    except Exception:  # noqa: BLE001 — any import failure ⇒ unavailable
        return (
            False,
            "Phoenix not installed — pip install runspec-console[phoenix]",
        )
    return (True, "")


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """True when a TCP connection to ``(host, port)`` succeeds — used to poll for
    the Phoenix server coming up (and to detect it's already serving)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_ready(url: str, timeout: float = 1.0) -> bool:
    """True when an HTTP request to ``url`` gets *any* response from the server —
    i.e. the HTTP layer is serving, not merely the TCP port being open. A 4xx/5xx
    still counts (the server answered); only a connection/read failure is "not
    ready". This closes the gap where Phoenix accepts the socket a beat before it
    can serve the page, which otherwise flashes a connection-refused page in the
    embedding iframe."""
    import urllib.error
    import urllib.request

    try:
        urllib.request.urlopen(url, timeout=timeout)  # noqa: S310 — fixed loopback URL
        return True
    except urllib.error.HTTPError:
        return True  # server responded (any status) ⇒ it's serving
    except Exception:  # noqa: BLE001 — URLError / timeout / socket ⇒ not ready yet
        return False


def traces_endpoint(port: int) -> str:
    """The OTLP/HTTP traces URL for a local Phoenix on ``port`` — what the console
    points its span exporter at."""
    return f"http://127.0.0.1:{port}/v1/traces"


class PhoenixServer:
    """Manages the lifecycle of one local Phoenix subprocess.

    Thread-safe: every start/stop/status transition is taken under ``_lock`` so a
    tab click and an atexit teardown can't race the handle. All methods are
    best-effort and return a status dict rather than raising."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._port: int = 0
        self._host = "127.0.0.1"
        # True when we're using a server we didn't launch (already listening on the
        # port). It's usable, but stop() must not kill it — we don't own it.
        self._adopted = False

    # ── public API ────────────────────────────────────────────────────────────

    def url(self) -> str:
        return f"http://{self._host}:{self._port}" if self._port else ""

    def is_running(self) -> bool:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            return True
        return self._adopted and self._port != 0

    def status(self) -> dict[str, Any]:
        ok, reason = available()
        running = self.is_running()
        return {
            "available": ok,
            "reason": reason,
            "running": running,
            "url": self.url() if running else "",
            "port": self._port if running else 0,
        }

    def start(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """Launch Phoenix (or return the running one). ``cfg`` is a
        ``config.read_phoenix()`` dict. Returns a status dict."""
        with self._lock:
            ok, reason = available()
            if not ok:
                return self.status()
            if self._proc is not None and self._proc.poll() is None:
                return self.status()  # already running — double-start guard

            port = int(cfg.get("port") or 6006)
            working_dir = str(cfg.get("working_dir") or "")
            try:
                if working_dir:
                    Path(working_dir).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("phoenix: could not create working_dir: %s", exc)

            # A server already listening on the port (a Phoenix the user started
            # by hand, or a leftover) — adopt it rather than fail. We don't own the
            # process, so stop() won't kill it, but the endpoint is usable.
            if _port_open(self._host, port):
                self._port = port
                self._adopted = True
                logger.info("phoenix: port %d already serving — adopting it", port)
                return self.status()

            env = dict(os.environ)
            env["PHOENIX_PORT"] = str(port)
            env["PHOENIX_HOST"] = self._host
            if working_dir:
                env["PHOENIX_WORKING_DIR"] = working_dir
            # Phoenix's built-in agent assistant (its /chat endpoint) can call an
            # external LLM + do web/bash — off by default to keep this feature
            # fully local. Set explicitly (not merely left unset) so an inherited
            # PHOENIX_DISABLE_AGENT_ASSISTANT from the parent env can't flip it.
            env["PHOENIX_DISABLE_AGENT_ASSISTANT"] = (
                "false" if cfg.get("assistant") else "true"
            )
            # Operator escape hatch: any extra PHOENIX_* vars pinned in
            # [phoenix].env, applied last so they win over the convenience knobs.
            for k, v in (cfg.get("env") or {}).items():
                env[str(k)] = str(v)

            cmd = [sys.executable, "-m", "phoenix.server.main", "serve"]
            try:
                self._proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
                    cmd,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
            except Exception as exc:  # noqa: BLE001 — degrade, never raise
                logger.warning("phoenix: failed to launch server: %s", exc)
                self._proc = None
                self._port = 0
                return self.status()

            self._port = port
            self._adopted = False  # we own this process
            if not self._wait_until_up(port):
                logger.warning(
                    "phoenix: server did not come up on port %d within %.0fs",
                    port,
                    _START_TIMEOUT_S,
                )
                self._terminate_locked()
            else:
                logger.info("phoenix: server running at %s", self.url())
            return self.status()

    def stop(self) -> dict[str, Any]:
        """Terminate the managed subprocess (best-effort). A server we merely
        *adopted* (started outside the console) is left alone — we only null our
        handle."""
        with self._lock:
            self._terminate_locked()
            self._port = 0
            self._adopted = False
            return self.status()

    # ── internals ─────────────────────────────────────────────────────────────

    def _wait_until_up(self, port: int) -> bool:
        # Wait for the HTTP layer to actually respond, not just the TCP port to
        # open — so by the time we report "running" and the tab points its iframe
        # at the server, the page is serveable (no connection-refused flash).
        url = f"http://{self._host}:{port}/"
        deadline = time.monotonic() + _START_TIMEOUT_S
        while time.monotonic() < deadline:
            # A crash before it comes up: give up early.
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                return False
            if _http_ready(url):
                return True
            time.sleep(_POLL_INTERVAL_S)
        return False

    def _terminate_locked(self) -> None:
        """Kill the managed subprocess if we own one. Caller holds ``_lock``."""
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 — teardown must never raise
            logger.warning("phoenix: error stopping server", exc_info=True)
