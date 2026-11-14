"""
sources/webhook.py — webhooks a host's runspec-scheduler received (console 0.438.0).

A venv's runspec-scheduler daemon receives webhooks, keeps them in its spool and
runs what it can on the host. A console *follows* a venv by running
``runspec-scheduler hooks follow`` over the SSH connection it already pools (an
exec channel, so it works where port forwarding is blocked), as the venv owner,
and reads one JSON line per change. This module owns that connection:

* :class:`WebhookFollower` — one reconnecting thread per followed host + venv.
  It resumes from the last event id it saw, folds each line into a
  :class:`~runspec_console.webhooks.VenvHooks` and tells the bridge.
* :class:`LineStream` — the NDJSON reader over an exec channel (or a local
  process), which also keeps the stderr tail so a failed start says why.
* :class:`WebhookSource` — the inert ``webhook`` trigger source. The bridge feeds
  each fresh delivery into the trigger engine as ``Event("webhook", <hook name>)``;
  registering it is what shows ``webhook`` in the Triggers source list.

Everything that touches SSH is injected, so the loop is tested with fakes.
See docs/webhooks.md → "Following from runspec-console".
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from collections.abc import Callable
from typing import Any, Protocol

from ..events import EmitFn, EventSource, register_source
from ..webhooks import VenvHooks

logger = logging.getLogger("runspec.console.webhooks")

_RECV_BYTES = 32_768

#: Seconds between reconnect attempts: doubles from START up to MAX.
BACKOFF_START = 2.0
BACKOFF_MAX = 120.0
#: After a failure that won't fix itself (no runspec-scheduler, too old, sudo
#: refused) try again this rarely.
BACKOFF_STUCK = 600.0
#: A follow that hears nothing (the daemon pings every 15 s) for this long is
#: treated as dead and reconnected.
SILENCE_TIMEOUT = 60.0


class Stream(Protocol):
    def recv(self) -> dict[str, Any] | None: ...
    def close(self) -> None: ...
    def failure(self) -> str: ...


class LineStream:
    """NDJSON lines from an exec channel (paramiko ``Channel``) or anything with
    the same ``recv`` / ``recv_stderr`` / ``recv_exit_status`` methods.

    ``recv`` returns the next JSON object, skipping blank and non-JSON lines, and
    ``None`` at EOF, on a read error, or after :data:`SILENCE_TIMEOUT` seconds of
    nothing. :meth:`failure` then says why it ended, from the exit status and the
    last stderr lines."""

    def __init__(
        self,
        channel: Any,
        release: Callable[[], None] | None = None,
        *,
        timeout: float = SILENCE_TIMEOUT,
    ) -> None:
        self._chan = channel
        self._release = release
        self._buf = b""
        self._closed = False
        self._why = ""
        with contextlib.suppress(Exception):
            channel.settimeout(timeout)

    def _fill(self) -> bool:
        try:
            chunk = self._chan.recv(_RECV_BYTES)
        except TimeoutError:
            self._why = f"nothing heard for {int(SILENCE_TIMEOUT)} s"
            return False
        except Exception as exc:  # noqa: BLE001 — the transport died
            self._why = f"connection lost ({exc})"
            return False
        if not chunk:
            return False
        self._buf += chunk
        return True

    def recv(self) -> dict[str, Any] | None:
        while True:
            nl = self._buf.find(b"\n")
            while nl == -1:
                if self._closed or not self._fill():
                    return None
                nl = self._buf.find(b"\n")
            line, self._buf = self._buf[:nl], self._buf[nl + 1 :]
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except ValueError:
                logger.debug("webhooks: dropping a non-JSON line: %r", text[:200])
                continue
            if isinstance(obj, dict):
                return obj

    def failure(self) -> str:
        """Why the stream ended, as one line ('' when it just closed)."""
        if self._why:
            return self._why
        err = b""
        with contextlib.suppress(Exception):
            while self._chan.recv_stderr_ready():
                err += self._chan.recv_stderr(_RECV_BYTES)
        code = None
        with contextlib.suppress(Exception):
            if self._chan.exit_status_ready():
                code = self._chan.recv_exit_status()
        lines = [
            ln.strip()
            for ln in err.decode("utf-8", "replace").splitlines()
            if ln.strip()
        ]
        tail = lines[-1] if lines else ""
        if code in (None, 0) and not tail:
            return ""
        if code == 127 or "not found" in tail:
            return "runspec-scheduler isn't installed in this venv (or is older than 0.11.0)"
        if "sudo" in tail and "password" in tail:
            return f"passwordless sudo to the venv owner was refused ({tail})"
        if "hooks" in tail and (
            "invalid choice" in tail or "unknown command" in tail.lower()
        ):
            return "this venv's runspec-scheduler is older than 0.11.0 (no webhooks); Update venv… upgrades it"
        return tail or f"hooks follow stopped (exit {code})"

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self._chan.close()
        release, self._release = self._release, None
        if release is not None:
            try:
                release()
            except Exception:
                logger.debug("webhooks: release failed", exc_info=True)


def stuck(reason: str) -> bool:
    """A failure that retrying soon won't fix."""
    low = reason.lower()
    return any(
        s in low
        for s in (
            "isn't installed",
            "older than",
            "root-owned",
            "sudo",
            "refused",
            "no such file",
        )
    )


ROOT_OWNED = (
    "root-owned venv: runspec-scheduler never runs as root, so it takes no webhooks"
)


def unavailable(reason: str) -> bool:
    """A venv that can't take webhooks at all (no runspec-scheduler, one older
    than 0.11.0, root-owned): shown quietly, not as a fault."""
    low = reason.lower()
    return any(s in low for s in ("isn't installed", "older than", "root-owned"))


class WebhookFollower:
    """Follow one host + venv until :meth:`stop`. Reconnects with backoff and
    resumes from the last event id. ``open_stream(argv_cursor)`` opens a
    :class:`Stream` running ``hooks follow``; ``on_change(state)`` runs after
    every line (from this thread), ``on_arrival(state, delivery)`` for each fresh
    delivery that should fire console triggers."""

    def __init__(
        self,
        state: VenvHooks,
        open_stream: Callable[[int | None], Stream],
        *,
        on_change: Callable[[VenvHooks], None],
        on_arrival: Callable[[VenvHooks, dict[str, Any]], None],
        replay_s: int = 900,
        sleep: Callable[[float], bool] | None = None,
    ) -> None:
        self.state = state
        self._open = open_stream
        self._on_change = on_change
        self._on_arrival = on_arrival
        self._replay_s = replay_s
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._stream: Stream | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.run, name=f"webhooks-{self.state.key}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            stream = self._stream
        if stream is not None:
            stream.close()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _changed(self) -> None:
        try:
            self._on_change(self.state)
        except Exception:
            logger.exception("webhooks: on_change failed")

    def run_once(self) -> str:
        """One connection, start to end. Returns why it ended ('' = it just
        closed)."""
        try:
            stream = self._open(self.state.cursor)
        except Exception as exc:  # noqa: BLE001 — couldn't connect
            return str(exc) or exc.__class__.__name__
        with self._lock:
            self._stream = stream
        try:
            first = stream.recv()
            if first is None or first.get("kind") != "hello":
                return stream.failure() or "hooks follow didn't answer"
            self.state.hello(first)
            self._changed()
            while not self._stop.is_set():
                msg = stream.recv()
                if msg is None:
                    return stream.failure()
                fresh = self.state.apply(msg, replay_s=self._replay_s)
                if msg.get("kind") != "ping":
                    self._changed()
                if fresh is not None:
                    try:
                        self._on_arrival(self.state, fresh)
                    except Exception:
                        logger.exception("webhooks: on_arrival failed")
            return ""
        finally:
            with self._lock:
                self._stream = None
            stream.close()

    def run(self) -> None:
        backoff = BACKOFF_START
        last_logged = ""
        while not self._stop.is_set():
            was_connected = self.state.connected
            reason = self.run_once()
            if self._stop.is_set():
                break
            self.state.connected = False
            if unavailable(reason):
                self.state.unavailable = reason
                self.state.error = ""
            else:
                self.state.unavailable = ""
                self.state.error = reason or "connection closed; reconnecting"
            self._changed()
            if reason and reason != last_logged:
                log = logger.info if unavailable(reason) else logger.warning
                log("webhooks: %s — %s", self.state.key, reason)
                last_logged = reason
            if was_connected or self.state.cursor is not None and not reason:
                backoff = BACKOFF_START
            wait = BACKOFF_STUCK if stuck(reason) else backoff
            if self._sleep(wait):
                break
            backoff = min(backoff * 2, BACKOFF_MAX)
        self.state.connected = False


class WebhookSource:
    """The inert ``webhook`` trigger source: the bridge's followers feed it.

    The event type is the hook's name (a trigger with ``source = "webhook"``,
    ``event = "jira-deploy"``), so there's no fixed list."""

    id = "webhook"

    def available(self) -> tuple[bool, str]:
        return (True, "")

    def event_types(self) -> list[str]:
        return []

    def start(self, emit: EmitFn) -> None:
        """No-op: the followers feed the engine directly."""

    def stop(self) -> None:
        """No-op."""


_source: EventSource = WebhookSource()

register_source("webhook", WebhookSource)
