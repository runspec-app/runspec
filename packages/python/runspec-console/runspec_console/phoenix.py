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

# Default seconds start() waits for the HTTP layer to respond before reporting
# the server didn't come up. Overridable via [phoenix] startup_timeout — first-run
# alembic migrations + sandbox init can be slow on some hosts.
_DEFAULT_START_TIMEOUT_S = 60.0
_POLL_INTERVAL_S = 0.25


def _phoenix_argv() -> list[str]:
    """The argv to launch the Phoenix server.

    Prefer the ``phoenix`` **console script** installed next to this interpreter —
    the exact entry ``phoenix serve`` uses (arize-phoenix declares
    ``phoenix = "phoenix.server.main:main"``). Launching ``python -m
    phoenix.server.main serve`` instead is *not* equivalent: it depends on a
    module ``__main__`` guard and, on Windows, interacts badly with multiprocessing
    ``spawn`` — the server process can start but never bind the port (looks like a
    slow/failed start). The console-script wrapper handles ``spawn`` correctly.
    Fall back to ``-m`` only when the script isn't found (non-standard layout)."""
    bindir = Path(sys.executable).parent
    names = ("phoenix.exe", "phoenix") if os.name == "nt" else ("phoenix",)
    for name in names:
        exe = bindir / name
        if exe.exists():
            return [str(exe), "serve"]
    return [sys.executable, "-m", "phoenix.server.main", "serve"]


def available() -> tuple[bool, str]:
    """``(True, "")`` when ``arize-phoenix`` is installed; else ``(False, reason)``.

    Probes with ``importlib.util.find_spec`` rather than ``import phoenix`` so the
    check never *executes* the package: importing arize-phoenix has heavy
    module-load side effects (ensures its working dir, registers alembic plugins +
    sandbox adapters, spins up machinery) that would otherwise run — and log — in
    the console's own process, purely to answer "is it installed?". We only ever
    run Phoenix as a subprocess (see ``_phoenix_argv``), so the package is never
    needed in-process. Never raises."""
    import importlib.util

    try:
        found = importlib.util.find_spec("phoenix") is not None
    except Exception:  # noqa: BLE001 — a broken/partial install ⇒ treat as absent
        found = False
    if not found:
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
        # Open file handle capturing the managed server's stdout/stderr, and its
        # path — so a failed/slow start is diagnosable instead of vanishing.
        self._log_fh: Any = None
        self._log_path: str = ""

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

            cmd = _phoenix_argv()
            # Capture the server's stdout+stderr to a log file (truncated each
            # launch) so a wrong invocation / bind error is diagnosable rather than
            # lost to DEVNULL. Falls back to DEVNULL if the file can't be opened.
            self._close_log()
            out: Any = subprocess.DEVNULL
            if working_dir:
                try:
                    self._log_path = str(Path(working_dir) / "phoenix-server.log")
                    self._log_fh = open(self._log_path, "wb")  # noqa: SIM115
                    out = self._log_fh
                except OSError:
                    self._log_fh = None
                    self._log_path = ""
            try:
                self._proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
                    cmd,
                    env=env,
                    stdout=out,
                    stderr=subprocess.STDOUT if out is not subprocess.DEVNULL else out,
                    stdin=subprocess.DEVNULL,
                )
            except Exception as exc:  # noqa: BLE001 — degrade, never raise
                logger.warning("phoenix: failed to launch server: %s", exc)
                self._close_log()
                self._proc = None
                self._port = 0
                return self.status()

            self._port = port
            self._adopted = False  # we own this process
            timeout = float(cfg.get("startup_timeout") or _DEFAULT_START_TIMEOUT_S)
            if not self._wait_until_up(port, timeout):
                logger.warning(
                    "phoenix: server did not come up on port %d within %.0fs%s",
                    port,
                    timeout,
                    f" — see {self._log_path}" if self._log_path else "",
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

    def _wait_until_up(self, port: int, timeout: float) -> bool:
        # Wait for the HTTP layer to actually respond, not just the TCP port to
        # open — so by the time we report "running" and the tab points its iframe
        # at the server, the page is serveable (no connection-refused flash).
        url = f"http://{self._host}:{port}/"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # A crash before it comes up: give up early.
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                return False
            if _http_ready(url):
                return True
            time.sleep(_POLL_INTERVAL_S)
        return False

    def _close_log(self) -> None:
        """Close the captured-output file handle if open (best-effort)."""
        fh, self._log_fh = self._log_fh, None
        if fh is not None:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass

    def _terminate_locked(self) -> None:
        """Kill the managed subprocess if we own one. Caller holds ``_lock``."""
        proc, self._proc = self._proc, None
        try:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 — teardown must never raise
            logger.warning("phoenix: error stopping server", exc_info=True)
        finally:
            self._close_log()
