"""bridge_infra.py — InfraMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import logging
from typing import Any


from .trace import trace_path, write_trace

from .bridge_const import (
    logger,
)
from .bridge_helpers import (
    _ParamikoDevHandler,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


class InfraMixin:
    def set_window(self, window: Any) -> None:
        self._window = window
        # Flush any SSH logs captured during pre-window startup, then start the
        # background refresh. Starting it here (not in __init__) means the first
        # cycle never dispatches into a missing window — which is what leaked the
        # "dispatch dropped: no window attached" warnings to the terminal.
        with self._lock:
            buffered = self._pre_window_ssh_logs
            self._pre_window_ssh_logs = []
        for detail in buffered:
            self._dispatch("runspec:ssh", detail)
        if not self._refresh_started:
            self._refresh_started = True
            self._start_refresh_watcher()
        # Start configured event triggers now the window can receive dispatches.
        self.start_triggers()
        # Join the chat room (always-on, independent of triggers) — its connect/
        # presence dispatches need the window up too.
        self.start_chat()
        # Abort in-flight turns the moment the window closes, so a turn parked at
        # the autonomy gate can't stall shutdown. Best-effort across pywebview
        # versions (the event may be absent).
        try:
            window.events.closed += self._abort_all_turns
            window.events.closed += self._on_window_closed
        except Exception:
            logger.debug("could not hook window 'closed' event", exc_info=True)

    def _on_window_closed(self, *_args: Any) -> None:
        """Window-close handler: leave the room cleanly so the supervisor thread
        doesn't keep reconnecting after the UI is gone."""
        self.stop_chat()

    def _build_ssh_pool(self) -> Any:
        """Construct the SSH connection pool from current ``[ssh]`` config.

        Returns None when pooling is disabled — callers then fall back to the
        connect-per-call ``executor.ssh_run`` path.
        """
        from .ssh_pool import SSHConnectionPool, pool_settings
        from .executor import _make_ssh_client

        settings = pool_settings(self.get_config().get("ssh"))
        if not settings["enabled"]:
            return None
        return SSHConnectionPool(
            settings=settings,
            make_client=_make_ssh_client,
            on_event=self._on_pool_event,
        )

    def _on_pool_event(self, detail: dict[str, Any]) -> None:
        """Surface a pool-lifecycle event in the Dev tab's ``ssh`` category.

        DEBUG rows (per-cycle reuse / new handshake / reap) are chatty, so they
        appear only when started with ``--dev`` — the same gate the paramiko
        capture uses. INFO/WARNING (reconnect, connect-backoff) always show.
        """
        if detail.get("level") == "DEBUG" and not self._debug:
            return
        self._emit_ssh_log(detail)

    def _close_ssh_pool(self) -> None:
        self.close_adhoc()
        pool = self._ssh_pool
        if pool is not None:
            pool.close_all()

    def _ssh_run(
        self,
        ssh_target: str,
        command: str,
        *,
        identity_file: str | None = None,
        timeout: int = 15,
    ) -> tuple[int, str, str]:
        """Run one SSH command, reusing a pooled transport when pooling is on.

        Single chokepoint for the bridge's one-shot SSH commands (discovery,
        history, logs, analytics) so they all share the per-host connection.
        Falls back to ``executor.ssh_run`` when the pool is disabled.
        """
        cfg = self.get_config().get("ssh")
        pool = self._ssh_pool
        if pool is not None:
            return pool.run(
                ssh_target,
                command,
                identity_file=identity_file,
                global_ssh_config=cfg,
                timeout=timeout,
            )
        from .executor import ssh_run

        return ssh_run(
            ssh_target,
            command,
            identity_file=identity_file,
            global_ssh_config=cfg,
            timeout=timeout,
        )

    def set_debug(self, debug: bool) -> None:
        """Record whether the Chromium inspector was enabled at startup.

        Also widens SSH log capture to DEBUG when debug is on, so the Dev tab's
        ``ssh`` rows include paramiko's packet/kex detail for deep investigation.
        """
        self._debug = debug
        logging.getLogger("paramiko").setLevel(logging.DEBUG if debug else logging.INFO)
        path = trace_path(debug)
        if path:
            logger.info("agent trace enabled → %s", path)

    def _trace(self, kind: str, data: dict[str, Any]) -> None:
        """Append a diagnostic record (tool result / outbound model request) to
        the agent trace file when tracing is on — see ``trace.py``. No-op
        otherwise; never raises into the chat turn."""
        path = trace_path(self._debug)
        if path:
            write_trace(path, kind, data)

    def _install_ssh_log_forwarder(self) -> None:
        """Route paramiko's loggers into the Dev tab and off the terminal.

        Attaches one handler to the top ``paramiko`` logger (child loggers such
        as ``paramiko.transport`` propagate up to it) and disables propagation
        so records never reach the root logger / launching terminal.
        """
        handler = _ParamikoDevHandler(self)
        self._ssh_log_handler = handler
        root = logging.getLogger("paramiko")
        # Idempotent: drop any handler we installed on a previous construction
        # (matters under tests / repeated Bridge() in one process).
        root.handlers = [
            h for h in root.handlers if not isinstance(h, _ParamikoDevHandler)
        ]
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        root.propagate = False
        # Let paramiko.transport inherit the parent level so its records flow to
        # the shared handler at the level we set above (app._suppress_noisy_loggers
        # pins it to WARNING at startup; clear that so INFO connect logs surface).
        logging.getLogger("paramiko.transport").setLevel(logging.NOTSET)

    def _emit_ssh_log(self, detail: dict[str, Any]) -> None:
        """Dispatch a captured paramiko record as a ``runspec:ssh`` event.

        Before the window attaches, buffer the record rather than dropping it —
        set_window() flushes the buffer so early connect logs still reach the
        Dev tab. The buffer is bounded so a connect storm before the UI is up
        can't grow it without limit.
        """
        if self._window is None:
            with self._lock:
                if len(self._pre_window_ssh_logs) < 200:
                    self._pre_window_ssh_logs.append(detail)
            return
        self._dispatch("runspec:ssh", detail)

    def is_debug_enabled(self) -> bool:
        """Exposed to the Dev tab so its inspector button behaves honestly."""
        return self._debug

    def open_devtools(self) -> None:
        """Best-effort open of the native Chromium inspector.

        pywebview 5.x has no reliable cross-platform API for this — debug mode
        (set at startup) enables right-click → Inspect / F12. We attempt any hook
        the backend happens to expose and otherwise no-op; the Dev tab tells the
        user to use the context menu / F12.
        """
        win = self._window
        if win is None:
            return
        for attr in ("open_devtools", "show_devtools"):
            fn = getattr(win, attr, None)
            if callable(fn):
                try:
                    fn()
                    return
                except Exception:
                    logger.debug("%s failed", attr, exc_info=True)

    # ── window controls ───────────────────────────────────────────────────────

    def minimize_window(self) -> None:
        if self._window:
            self._window.minimize()

    def toggle_maximize_window(self) -> None:
        if self._window:
            if self._window.maximized:
                self._window.restore()
            else:
                self._window.maximize()

    def close_window(self) -> None:
        if self._window:
            self._window.destroy()

    def resize_window(self, width: int, height: int) -> None:
        if self._window:
            self._window.resize(width, height)

    def move_window(self, x: int, y: int) -> None:
        if self._window:
            self._window.move(x, y)

    # ── hosts ────────────────────────────────────────────────────────────────
