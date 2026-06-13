"""
bridge.py — Python implementation of the BridgeApi declared in bridge/index.ts.

Every public method on this class is callable from the frontend via
window.pywebview.api.<method>(...).  pywebview wraps each call in a Promise
automatically — methods must be synchronous (no async def).

Streaming (invoke_runnable, send_chat) works by dispatching CustomEvents from
background threads via window.evaluate_js().  The frontend listens for:
  runspec:output   { id, line, stream }          — one log line
  runspec:run_end  { id, exit_code, duration_ms } — invocation finished
  runspec:token    { id, token }                  — LLM token (chat only)
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import queue
import random
import sys
import threading
import time
import uuid
import webbrowser
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from . import notify
from .config import (
    confirm_notifications_enabled,
    filters_path,
    hosts_path,
    read_config,
    room_notifications_enabled,
    write_config,
)
from .discovery import discover_local, discover_remote
from .executor import run_local, run_remote
from .hosts import load_hosts, save_hosts, venv_name

logger = logging.getLogger(__name__)

_CANCEL_KEY = "__cancel_event__"  # key in _in_flight dicts, not surfaced to JS


class _ParamikoDevHandler(logging.Handler):
    """Forward paramiko log records into the Dev tab as ``runspec:ssh`` events.

    paramiko logs the full "Error reading SSH protocol banner" traceback from
    its transport thread at ERROR. With no handler configured those records hit
    Python's last-resort handler and spill into the launching terminal as raw
    tracebacks. Routing them here turns each into a filterable, timestamped
    Dev-tab row (the connect/banner/auth lifecycle, with the exact failure), and
    — because the paramiko logger's ``propagate`` is turned off when this is
    installed — keeps the terminal clean.
    """

    def __init__(self, bridge: Bridge) -> None:
        super().__init__()
        self._bridge = bridge
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            detail: dict[str, Any] = {
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            # format() appends the traceback after the (bare) message when the
            # record carries exc_info — that's the banner-timeout stack we want
            # expandable in the Dev tab.
            if record.exc_info or record.exc_text:
                detail["traceback"] = self.format(record)
            self._bridge._emit_ssh_log(detail)
        except Exception:
            # A logging handler must never raise — that would break the call
            # site that emitted the log (here, paramiko's transport thread).
            pass


# Autonomy escalation order — higher = more restrictive (must check before run).
_AUTONOMY_RANK = {"autonomous": 0, "confirm": 1, "supervised": 2, "manual": 3}

# Built-in (not runnable-backed) agent tools. Names are intentionally free of
# "__" so they never collide with the host__runnable scheme; both are
# side-effecting file moves, so the autonomy gate prompts before running.
_CUSTOM_TOOL_NAMES = frozenset({"download_file", "relay_file"})


class _BridgeTriggerOps:
    """Adapts the Bridge to trigger_runner.BridgeOps without exposing these
    methods on the JS api surface (only public Bridge methods are exposed)."""

    def __init__(self, bridge: Bridge) -> None:
        self._bridge = bridge

    def run_agent_turn(
        self,
        chat_id: str,
        system: str,
        message: str,
        *,
        draft: bool,
        reply_to: str | None = None,
        room: str | None = None,
        from_mailbox: str | None = None,
        allowed_runnables: list[str] | None = None,
        record: Any = None,
        stage: Any = None,
    ) -> str:
        return self._bridge._trigger_run_agent_turn(
            chat_id,
            system,
            message,
            draft=draft,
            reply_to=reply_to,
            room=room,
            from_mailbox=from_mailbox,
            allowed_runnables=allowed_runnables,
            record=record,
            stage=stage,
        )

    def queue_mailbox_for(self, payload: dict[str, Any]) -> str:
        queue = self._bridge._email_queue_for(payload)
        return self._bridge._email_mailbox_for(queue) if queue else ""

    def run_rule_tool(
        self, chat_id: str, tool_name: str, tool_input: dict[str, Any]
    ) -> str:
        return self._bridge._gated_run_tool(tool_name, tool_input, chat_id)

    def mirror_to_chat(self, kind: str, label: str, text: str) -> None:
        # The Console tab renders this as a finished block matching the trigger
        # mode: kind="run" → a slash-command run card, kind="chat" → an agent
        # card — the same blocks a manual invocation makes, so a triggered run
        # looks like the same command run by hand. The Triggers tab's activity
        # list listens for this event too. The id keys the Console block.
        self._bridge._dispatch(
            "runspec:trigger_note",
            {
                "id": uuid.uuid4().hex[:12],
                "kind": kind,
                "label": label,
                "text": text,
            },
        )


def _chat_connect_hint(exc: BaseException, *, via: str) -> str | None:
    """A concise, actionable message for a known-*permanent* chat-connect failure,
    or ``None`` to fall back to a full traceback.

    ``Administratively prohibited`` on a ``direct-tcpip`` channel means the host
    refuses SSH port-forwarding (``AllowTcpForwarding no``) — retrying the tunnel
    will never succeed, so a 20-line traceback every backoff is just noise. The
    fix is the exec relay, so point the operator straight at it."""
    if via != "exec" and "Administratively prohibited" in str(exc):
        return (
            "SSH port-forwarding is blocked on this host (Administratively "
            'prohibited). Set room_via = "exec" on the host entry to reach the '
            "room over an exec channel (console-room connect) instead of a tunnel."
        )
    return None


def _classify_room_connection(exc: BaseException | None, *, via: str) -> dict[str, Any]:
    """Turn a room-connect attempt's outcome into a structured, human-readable
    result for the UI's connection tester: ``{ok, stage, message, hint}``.

    ``stage`` localises the failure — ``ssh`` (reaching/authing the host, incl.
    the HTTP proxy), ``channel`` (opening the tunnel / exec relay), ``room`` (the
    relay reached the host but not the room server), or ``ok``. ``hint`` is an
    actionable next step when we recognise the failure."""
    if exc is None:
        return {"ok": True, "stage": "ok", "message": "", "hint": None}
    text = str(exc) or type(exc).__name__
    low = text.lower()
    if "in connect-backoff" in low:
        return {
            "ok": False,
            "stage": "ssh",
            "message": f"Host is in connect-backoff after recent failures ({text}). The always-on reconnect loop is already retrying — try again in a moment.",
            "hint": None,
        }
    if isinstance(exc, TimeoutError) or "timed out" in low or "timeout" in low:
        return {
            "ok": False,
            "stage": "ssh",
            "message": "Timed out reaching the host — the SSH connection (through the HTTP proxy, if configured) didn't complete in time.",
            "hint": "If the proxy works for discovery/transfers, this is usually transient at startup (many connections open at once) — retry. If it persists, check the [ssh] proxy host:port is reachable.",
        }
    if "administratively prohibited" in low:
        return {
            "ok": False,
            "stage": "channel",
            "message": "The host refused the tunnel — SSH port-forwarding is disabled (AllowTcpForwarding no).",
            "hint": _chat_connect_hint(exc, via=via),
        }
    if "authentication" in low or "not a valid" in low or "no authentication" in low:
        return {
            "ok": False,
            "stage": "ssh",
            "message": f"SSH authentication failed ({text}). Check this host's key / identityFile.",
            "hint": None,
        }
    return {
        "ok": False,
        "stage": "ssh",
        "message": text,
        "hint": _chat_connect_hint(exc, via=via),
    }


class _RotaPresenceWatcher:
    """Pushes console presence to the chat room as the working-hours rota flips.

    A console is *available* in the room while on-shift and *away* otherwise (see
    the design §1). The rota only changes at window boundaries — no event arrives
    to drive it — so this polls ``clock_ok`` on an interval and calls
    ``set_presence`` only when the on-shift state actually flips. The first tick
    aligns presence with the rota immediately (the source announces "available" on
    connect, which the rota may correct to "away")."""

    def __init__(
        self,
        clock_ok: Callable[[], bool],
        set_presence: Callable[[bool], None],
        *,
        interval: float = 30.0,
    ) -> None:
        self._clock_ok = clock_ok
        self._set_presence = set_presence
        self._interval = interval
        self._stop = threading.Event()
        self._last: bool | None = None
        self._thread: threading.Thread | None = None

    def _tick(self) -> None:
        """Evaluate the rota once; push presence only on a change (split out so
        the flip logic is unit-tested without the thread / sleep)."""
        try:
            on = bool(self._clock_ok())
        except Exception:
            logger.exception("chat: rota clock check failed")
            return
        if on == self._last:
            return
        self._last = on
        try:
            self._set_presence(on)
        except Exception:
            logger.exception("chat: presence push failed")

    def start(self) -> None:
        self._tick()  # align presence with the rota right away
        self._thread = threading.Thread(
            target=self._run, name="chat-presence", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._tick()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None


class Bridge:
    def __init__(self) -> None:
        self._window: Any = None
        self._debug: bool = False
        self._lock = threading.Lock()
        # All UI dispatches go through one FIFO queue drained by a single daemon
        # thread, so evaluate_js is never called concurrently from many threads
        # (trigger workers, the COM event pump, the agent loop). Concurrent
        # evaluate_js from background threads was racy on pywebview — under a
        # burst of events the autonomy-confirm prompt could be lost or the
        # buttons never render. Serialising fixes that and keeps delivery in order.
        self._dispatch_q: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue()
        self._dispatch_thread = threading.Thread(
            target=self._ui_dispatch_loop, name="ui-dispatch", daemon=True
        )
        self._dispatch_thread.start()
        self._in_flight: dict[str, dict[str, Any]] = {}
        # Cached ad-hoc SFTP connections for the relay source browser, keyed by
        # (jump, host, user, port) — so navigating directories on a slow link
        # handshakes the target once, then reuses the warm connection. Torn down
        # by close_adhoc() (relay dialog close) and on shutdown.
        self._adhoc_conns: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._adapter: Any = None  # ModelAdapter, loaded on demand
        # Event-trigger engine (built lazily in start_triggers) and the in-memory
        # kanban board: card_id → card.to_dict(). Cards are not persisted — a
        # missed event while the console was closed is intentionally not replayed.
        self._trigger_engine: Any = None
        self._trigger_cards: dict[str, dict[str, Any]] = {}
        # Per email conversation (Outlook ConversationID), the ordered fold events
        # (requester messages + claim/lifecycle markers) behind an email queue
        # card's state — the mailbox is the source of truth, replayed locally.
        self._email_threads: dict[str, list[dict[str, Any]]] = {}
        # Item ids (Message-ID / EntryID) already folded, so the startup backlog
        # scan and the live NewMailEx source can't double-count an overlapping item.
        self._email_seen: set[str] = set()
        # Audit feed: every event the engine sees + its disposition (bounded ring).
        self._trigger_events: list[dict[str, Any]] = []
        # Chat rooms are a property of fleet host entries (room + room_port in
        # runspec_hosts.toml). The console joins each declared room always-on,
        # independent of triggers, via a reconnecting supervisor per room — using
        # that host's own ssh target + key so the tunnel reuses the pooled
        # connection. Keyed by room name: live ChatSource while connected.
        self._chat_sources: dict[str, Any] = {}
        self._chat_stop = threading.Event()
        self._chat_supervisors: dict[str, threading.Thread] = {}
        # Helpdesk agent mode (a host entry with room_queue). Threads the hub has
        # told us about: conversation room → {state, assignee} from `conversation`
        # frames (populated by the subscribe catch-up, kept live after). Agent
        # namespaces we're connected under → our display user there (the identity
        # the hub records as assignee on our claims). Catalog index: namespace →
        # bare tool name → the discovered runnable, so a visitor request can be
        # mapped back to (host, runnable) at enactment time.
        self._thread_states: dict[str, dict[str, Any]] = {}
        self._agent_rooms: dict[str, str] = {}
        self._catalog_index: dict[str, dict[str, dict[str, Any]]] = {}
        # namespace → last catalog actually published, so the periodic refresh
        # only re-sends (and logs) when the contents change. Cleared on disconnect
        # so the next subscribe republishes from scratch.
        self._published_catalogs: dict[str, list[dict[str, Any]]] = {}
        self._hosts: list[dict[str, Any]] = []
        # Named source-side stream filters (runspec_filters.toml). Loaded once at
        # startup and reloaded on save; resolved by name when a relay/download
        # selects one. See filters_store.py for the schema.
        self._filters: list[dict[str, Any]] = []
        # tid → final transfer_end detail, so a synchronous caller (the agent tool)
        # can await a transfer's outcome. Bounded; oldest entries are evicted.
        self._recent_transfer_results: dict[str, dict[str, Any]] = {}
        self._connected_cache: dict[str, bool] = {}  # host name → last known state
        # Real remote log dirs per venv runspec path (asked of the remote, cached
        # for the session — they don't move). Avoids guessing the dir from the
        # binary path, which is wrong for pipx / ~/.local / system installs.
        self._remote_logdir_cache: dict[str, list[str]] = {}
        self._runnables_cache: list[dict[str, Any]] = []
        # Persistent agent conversation so the model remembers prior turns. One
        # rolling conversation per app session; reset via clear_chat(). The lock
        # serialises turns so two quick sends can't interleave the history.
        self._chat_history: list[dict[str, Any]] = []
        self._chat_lock = threading.Lock()
        # chat_id → cancel Event, so a turn can be stopped mid-run (guarded by
        # self._lock, NOT _chat_lock — cancel must fire while a turn holds that).
        self._chat_cancels: dict[str, threading.Event] = {}
        # request_id → (chat_id, Event, {"approved", "cancelled"}) for agent
        # tool-call confirmations. chat_id lets cancel_chat wake a pending
        # prompt so Stop works while the Approve/Deny dialog is up.
        self._pending_confirms: dict[
            str, tuple[str, threading.Event, dict[str, bool]]
        ] = {}
        # request_id → display metadata for the same pending confirm, kept in
        # parallel so the work-inbox cold-start snapshot (get_pending_confirms)
        # can show a parked gate that predates the view mounting. Populated in
        # _register_confirm, dropped in _finish_confirm alongside the tuple above.
        self._pending_confirm_meta: dict[str, dict[str, Any]] = {}
        self._ssh_log_handler: _ParamikoDevHandler | None = None
        # SSH log records captured before the window attaches are buffered here
        # (not dropped), then flushed to the Dev tab once it's ready — so the
        # very first refresh cycle's connect logs aren't lost.
        self._pre_window_ssh_logs: list[dict[str, Any]] = []
        self._refresh_started = False
        # Centralized SSH connection pool — one reused transport per host,
        # shared by every SSH path (refresh, discovery, history, logs,
        # invocation). None when [ssh] pool = false (legacy connect-per-call).
        self._ssh_pool: Any = self._build_ssh_pool()
        self._install_ssh_log_forwarder()
        self._reload_hosts()
        self._reload_filters()
        import atexit

        atexit.register(self._close_ssh_pool)
        atexit.register(self.stop_chat)  # stop the room reconnect supervisor
        atexit.register(self.stop_triggers)  # quit any source pump threads cleanly
        # Unblock any in-flight turn parked at the autonomy gate so closing the
        # app can't hang waiting to join a worker stuck in event.wait(). Registered
        # last so it runs first (LIFO) — before concurrent.futures joins workers.
        atexit.register(self._abort_all_turns)

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

    def get_hosts(self) -> list[dict[str, Any]]:
        self._reload_hosts()
        local = self._local_host_entry()
        all_hosts = [local] + [h for h in self._hosts if h.get("name") != "local"]
        result: list[dict[str, Any]] = []
        for h in all_hosts:
            paths = _paths(h)
            with self._lock:
                # Local is always connected; remotes use last cached probe result
                # (defaults to False until first probe completes)
                connected = self._connected_cache.get(h["name"], h.get("ssh") is None)
            result.append(
                {
                    "name": h["name"],
                    "connected": connected,
                    "runnableCount": 0,  # filled by get_runnables
                    "groups": [venv_name(p) for p in paths],
                    "role": h.get("role"),
                    "group": h.get("group"),
                }
            )
        return result

    def get_runnables(self, host: str) -> list[dict[str, Any]]:
        with self._lock:
            cache = list(self._runnables_cache)
        if host == "all":
            return cache
        return [r for r in cache if r.get("host") == host]

    def list_dir(
        self,
        host: str,
        path: str = "",
        group: str | None = None,
        dirs_only: bool = False,
        runnable: str | None = None,
        as_service_account: bool = False,
    ) -> dict[str, Any]:
        """List a directory on ``host`` for the Forms path-picker.

        ``path`` empty means the connecting user's home directory. Returns
        ``{cwd, parent, entries, truncated}`` (see ``fs_browse``) or
        ``{"error": ...}``. By default lists as the SSH login user (or the console
        user locally). With ``as_service_account`` and a ``runnable``, the remote
        listing is wrapped in that runnable's ``run_as``/become — so the picker
        shows what the job itself would see. ``group`` disambiguates a runnable
        that exists in more than one venv on the host.
        """
        entry = self._host_entry(host)
        if entry is None:
            return {"error": f"unknown host {host}"}
        try:
            if entry.get("ssh"):
                from .fs_browse import parse_remote_listing, remote_list_command

                run_as, become_method, become_flags = "", "sudo", None
                if as_service_account:
                    if not runnable:
                        return {
                            "error": "service-account view needs a runnable context"
                        }
                    with self._lock:
                        cached = next(
                            (
                                r
                                for r in self._runnables_cache
                                if r.get("host") == host
                                and r.get("name") == runnable
                                and (group is None or r.get("group") == group)
                            ),
                            None,
                        )
                    run_as, become_method, become_flags = self._become_params(
                        cached, host
                    )
                    if not run_as:
                        return {
                            "error": f"no service account (run_as) configured for {runnable} on {host}"
                        }

                cmd = remote_list_command(
                    path,
                    run_as=run_as,
                    become_method=become_method,
                    become_flags=become_flags,
                )
                code, out, err = self._ssh_run(
                    entry["ssh"],
                    cmd,
                    identity_file=entry.get("identityFile"),
                    timeout=15,
                )
                if code != 0:
                    who = f" as {run_as}" if run_as else ""
                    return {
                        "error": err.strip()
                        or f"could not list {path or '~'}{who} (exit {code})"
                    }
                return parse_remote_listing(out, dirs_only=dirs_only)

            from .fs_browse import list_local

            return list_local(path, dirs_only=dirs_only)
        except (OSError, ValueError) as e:
            return {"error": str(e)}

    # ── file transfer (SFTP) ────────────────────────────────────────────────────

    def _open_sftp(
        self, ssh_target: str, identity_file: str | None, cfg: dict[str, Any] | None
    ) -> tuple[Any, Callable[[], None]]:
        """Open an SFTP channel on ``ssh_target`` — pooled when pooling is on."""
        pool = self._ssh_pool
        if pool is not None:
            return pool.open_sftp(
                ssh_target, identity_file=identity_file, global_ssh_config=cfg
            )
        from .executor import _make_ssh_client

        client = _make_ssh_client(ssh_target, identity_file, cfg)
        sftp = client.open_sftp()

        def release() -> None:
            for closer in (sftp, client):
                try:
                    closer.close()
                except Exception:
                    pass

        return sftp, release

    def start_transfer(
        self,
        source_host: str,
        source_paths: list[str],
        dest_host: str,
        dest_dir: str,
        filter_name: str = "",
        filter_arg: str | None = None,
    ) -> dict[str, Any]:
        """Copy files between the local machine and a remote host over SFTP.

        v1 supports **local↔remote only** (exactly one side is ``"local"``).
        ``filter_name`` selects a saved source-side filter (``filter_arg`` is the
        runtime value for its ``{{arg}}`` placeholder); it applies only to a
        host and only the output is written. For a **download** (pull) the filter
        runs on the remote source; for an **upload** (push) it runs locally over
        the local source (needs a POSIX ``sh`` on PATH). Returns ``{"id": ...}``
        and then streams ``runspec:transfer_progress`` / ``runspec:transfer_end``
        events, or ``{"error": ...}`` on a bad request.
        """
        import shutil

        from .fs_transfer import plan_transfer, render_pipeline

        try:
            plan = plan_transfer(source_host, source_paths, dest_host, dest_dir)
        except ValueError as e:
            return {"error": str(e)}

        entry = self._host_entry(plan.remote_host)
        if entry is None or not entry.get("ssh"):
            return {"error": f"{plan.remote_host} is not a reachable remote host"}

        rendered = None
        if filter_name:
            filt = self._filter_entry(filter_name)
            if filt is None:
                return {"error": f"unknown filter: {filter_name}"}
            if plan.direction == "push" and shutil.which("sh") is None:
                return {"error": "filtering an upload needs a POSIX shell (sh) on PATH"}
            try:
                rendered = render_pipeline(filt.get("pipeline", ""), filter_arg)
            except ValueError as e:
                return {"error": str(e)}

        tid = uuid.uuid4().hex[:12]
        cancel_event = threading.Event()
        with self._lock:
            self._in_flight[tid] = {
                "id": tid,
                "runnable": f"transfer · {plan.direction}",
                "group": "",
                "host": plan.remote_host,
                "operator": self._current_user(),
                "runAs": "",
                "startedAt": _iso_now(),
                "args": {
                    "direction": plan.direction,
                    "files": len(plan.items),
                    **({"filter": filter_name} if rendered is not None else {}),
                },
                _CANCEL_KEY: cancel_event,
            }

        def run() -> None:
            cfg = self.get_config().get("ssh")
            ok, error, done = True, None, 0
            release: Callable[[], None] | None = None
            count = len(plan.items)

            def session_factory() -> tuple[Any, Callable[[], None]]:
                pool = self._ssh_pool
                if pool is not None:
                    return pool.open_session(
                        entry["ssh"],
                        identity_file=entry.get("identityFile"),
                        global_ssh_config=cfg,
                    )
                from .executor import _make_ssh_client

                client = _make_ssh_client(entry["ssh"], entry.get("identityFile"), cfg)
                transport = client.get_transport()
                if transport is None:
                    client.close()
                    raise OSError("source transport unavailable")
                return transport.open_session(), client.close

            try:
                sftp, release = self._open_sftp(
                    entry["ssh"], entry.get("identityFile"), cfg
                )
                for idx, item in enumerate(plan.items):
                    if cancel_event.is_set():
                        ok, error = False, "cancelled"
                        break
                    try:
                        if rendered is not None and plan.direction == "pull":
                            self._transfer_one_filtered(
                                session_factory,
                                sftp,
                                item,
                                rendered,
                                tid,
                                idx,
                                count,
                                cancel_event,
                            )
                        elif rendered is not None:  # push: filter locally
                            self._transfer_one_filtered_local(
                                sftp,
                                item,
                                rendered,
                                tid,
                                idx,
                                count,
                                cancel_event,
                            )
                        else:
                            self._transfer_one(
                                sftp, plan.direction, item, tid, idx, count
                            )
                        done += 1
                    except Exception as e:
                        # One file failing is reported but doesn't abort the rest.
                        ok, error = False, str(e)
                        self._dispatch(
                            "runspec:transfer_progress",
                            {
                                "id": tid,
                                "file": item.name,
                                "index": idx,
                                "count": count,
                                "transferred": 0,
                                "total": 0,
                                "error": str(e),
                            },
                        )
            except Exception as e:
                ok, error = False, str(e)
            finally:
                if release:
                    release()
                with self._lock:
                    self._in_flight.pop(tid, None)
            logger.info(
                "transfer %s %s: %d/%d ok=%s", tid, plan.direction, done, count, ok
            )
            self._emit_transfer_end(
                tid,
                {"id": tid, "ok": ok, "files": done, "total": count, "error": error},
            )

        threading.Thread(target=run, daemon=True).start()
        return {"id": tid}

    def _transfer_one(
        self,
        sftp: Any,
        direction: str,
        item: Any,
        tid: str,
        idx: int,
        count: int,
        host: str | None = None,
    ) -> None:
        """SFTP put/get a single file, emitting throttled progress events.

        ``host`` is set for fan-out (one upload per target host) so the UI can
        show per-host progress; it is omitted for a single transfer.
        """
        last = [0.0]

        def cb(transferred: int, total: int) -> None:
            now = time.monotonic()
            if now - last[0] >= 0.15 or transferred >= total:
                last[0] = now
                detail: dict[str, Any] = {
                    "id": tid,
                    "file": item.name,
                    "index": idx,
                    "count": count,
                    "transferred": transferred,
                    "total": total,
                }
                if host is not None:
                    detail["host"] = host
                self._dispatch("runspec:transfer_progress", detail)

        if direction == "push":
            if os.path.isdir(item.src):
                raise IsADirectoryError(
                    f"{item.name} is a directory — directory transfer isn't supported yet"
                )
            sftp.put(item.src, item.dst, callback=cb)
        else:
            sftp.get(item.src, item.dst, callback=cb)

    def _emit_transfer_end(self, tid: str, detail: dict[str, Any]) -> None:
        """Dispatch the ``runspec:transfer_end`` UI event and record the result so
        a synchronous waiter (``_await_transfer``) can read the outcome."""
        self._dispatch("runspec:transfer_end", detail)
        with self._lock:
            self._recent_transfer_results[tid] = detail
            # Keep the map bounded — drop the oldest insertions.
            while len(self._recent_transfer_results) > 64:
                oldest = next(iter(self._recent_transfer_results))
                self._recent_transfer_results.pop(oldest, None)

    def _await_transfer(
        self, tid: str, timeout: float = 600.0
    ) -> dict[str, Any] | None:
        """Block until the transfer ``tid`` finishes (or ``timeout`` elapses),
        returning its final ``transfer_end`` detail. Result is recorded by
        ``_emit_transfer_end`` whether or not anyone waits, so there's no race."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                detail = self._recent_transfer_results.pop(tid, None)
            if detail is not None:
                return detail
            time.sleep(0.05)
        return None

    def cancel_transfer(self, transfer_id: str) -> None:
        """Request cancellation of an in-flight transfer (stops before the next file)."""
        with self._lock:
            inv = self._in_flight.get(transfer_id)
        ev = inv.get(_CANCEL_KEY) if inv else None
        if ev is not None:
            ev.set()

    def start_fanout(
        self,
        source_paths: list[str],
        dest_hosts: list[str],
        dest_dir: str,
        filter_name: str = "",
        filter_arg: str | None = None,
    ) -> dict[str, Any]:
        """Upload the same local source file(s) to ``dest_dir`` on **many** remote hosts.

        Push-only (local → N remotes), one SFTP transfer per host in parallel over
        each host's pooled transport. When ``filter_name`` is set, each source is
        filtered **once** locally (needs a POSIX ``sh``) and the filtered output is
        what gets fanned out. Returns ``{"id": ...}`` and streams
        ``runspec:transfer_progress`` events carrying a ``host`` field, then a
        single ``runspec:transfer_end`` whose ``hosts`` lists each host's result —
        or ``{"error": ...}`` on a bad request.
        """
        import shutil

        from .fs_transfer import plan_transfer, render_pipeline

        paths = [p for p in (source_paths or []) if p and p.strip()]
        targets = [h for h in (dest_hosts or []) if h and h != "local"]
        if not paths:
            return {"error": "no source files selected"}
        if not targets:
            return {"error": "no target hosts selected"}
        if not dest_dir or not dest_dir.strip():
            return {"error": "no destination folder selected"}

        rendered = None
        if filter_name:
            filt = self._filter_entry(filter_name)
            if filt is None:
                return {"error": f"unknown filter: {filter_name}"}
            if shutil.which("sh") is None:
                return {"error": "filtering an upload needs a POSIX shell (sh) on PATH"}
            try:
                rendered = render_pipeline(filt.get("pipeline", ""), filter_arg)
            except ValueError as e:
                return {"error": str(e)}

        entries: dict[str, dict[str, Any]] = {}
        for h in targets:
            entry = self._host_entry(h)
            if entry is None or not entry.get("ssh"):
                return {"error": f"{h} is not a reachable remote host"}
            entries[h] = entry

        tid = uuid.uuid4().hex[:12]
        cancel_event = threading.Event()
        with self._lock:
            self._in_flight[tid] = {
                "id": tid,
                "runnable": f"fan-out · {len(targets)} hosts",
                "group": "",
                "host": ", ".join(targets),
                "operator": self._current_user(),
                "runAs": "",
                "startedAt": _iso_now(),
                "args": {"hosts": len(targets), "files": len(paths)},
                _CANCEL_KEY: cancel_event,
            }

        cfg = self.get_config().get("ssh")
        # The list each host actually uploads — the raw sources, or (when a filter
        # is set) the locally pre-filtered temp copies, swapped in by run().
        send_paths = list(paths)

        def per_host(h: str) -> dict[str, Any]:
            entry = entries[h]
            ok, error = True, None
            release: Callable[[], None] | None = None
            try:
                sftp, release = self._open_sftp(
                    entry["ssh"], entry.get("identityFile"), cfg
                )
                plan = plan_transfer("local", send_paths, h, dest_dir)
                count = len(plan.items)
                for idx, item in enumerate(plan.items):
                    if cancel_event.is_set():
                        ok, error = False, "cancelled"
                        break
                    try:
                        self._transfer_one(sftp, "push", item, tid, idx, count, host=h)
                    except Exception as e:
                        ok, error = False, str(e)
                        self._dispatch(
                            "runspec:transfer_progress",
                            {
                                "id": tid,
                                "host": h,
                                "file": item.name,
                                "index": idx,
                                "count": count,
                                "transferred": 0,
                                "total": 0,
                                "error": str(e),
                            },
                        )
            except Exception as e:
                ok, error = False, str(e)
            finally:
                if release:
                    release()
            return {"host": h, "ok": ok, "error": error}

        def run() -> None:
            import shutil as _shutil
            import tempfile
            import types
            from concurrent.futures import ThreadPoolExecutor

            results: list[dict[str, Any]] = []
            tmpdir: str | None = None
            try:
                if rendered is not None:
                    # Filter each source once, locally, into a temp dir keyed by the
                    # original basename (so the dest filename is unchanged), then fan
                    # out the small filtered copies instead of the originals.
                    tmpdir = tempfile.mkdtemp(prefix="runspec-filter-")
                    temp_paths: list[str] = []
                    for i, p in enumerate(paths):
                        if cancel_event.is_set():
                            raise RuntimeError("cancelled")
                        base = os.path.basename(p)
                        tmp = os.path.join(tmpdir, base)
                        item = types.SimpleNamespace(name=base, src=p, dst=tmp)
                        self._run_filter_stream_local(
                            rendered,
                            p,
                            dst_open=lambda tmp=tmp: open(tmp, "wb"),
                            dst_remove=lambda tmp=tmp: os.remove(tmp),
                            in_size=os.path.getsize(p) if os.path.exists(p) else 0,
                            item=item,
                            tid=tid,
                            idx=i,
                            count=len(paths),
                            cancel_event=cancel_event,
                        )
                        temp_paths.append(tmp)
                    send_paths[:] = temp_paths
                with ThreadPoolExecutor(max_workers=min(len(targets), 8)) as ex:
                    results = list(ex.map(per_host, targets))
            except Exception as e:
                # A failed/cancelled pre-filter means nothing was fanned out.
                results = [{"host": h, "ok": False, "error": str(e)} for h in targets]
            finally:
                if tmpdir is not None:
                    _shutil.rmtree(tmpdir, ignore_errors=True)
                with self._lock:
                    self._in_flight.pop(tid, None)
            overall = bool(results) and all(r["ok"] for r in results)
            logger.info("fan-out %s: %d hosts ok=%s", tid, len(targets), overall)
            self._emit_transfer_end(
                tid,
                {
                    "id": tid,
                    "ok": overall,
                    "files": len(paths),
                    "total": len(paths),
                    "error": None if overall else "one or more hosts failed",
                    "hosts": results,
                },
            )

        threading.Thread(target=run, daemon=True).start()
        return {"id": tid}

    def check_dir_exists(
        self, hosts: list[str], path: str
    ) -> dict[str, dict[str, Any]]:
        """Pre-flight: does directory ``path`` exist on each of ``hosts``?

        Used by the fan-out dialog to flag hosts whose layout differs *before* a
        push (the destination is only browsed on the first host). Returns
        ``{host: {"exists": bool, "error": str | None}}``; a connection failure
        is reported as ``exists=False`` with an ``error``.
        """
        import shlex

        result: dict[str, dict[str, Any]] = {}
        if not path or not path.strip():
            return result
        for h in hosts:
            if not h:
                continue
            if h == "local":
                result[h] = {
                    "exists": os.path.isdir(os.path.expanduser(path)),
                    "error": None,
                }
                continue
            entry = self._host_entry(h)
            if entry is None or not entry.get("ssh"):
                result[h] = {"exists": False, "error": "not a reachable remote host"}
                continue
            code, _out, err = self._ssh_run(
                entry["ssh"],
                f"test -d {shlex.quote(path)}",
                identity_file=entry.get("identityFile"),
                timeout=10,
            )
            if code == 0:
                result[h] = {"exists": True, "error": None}
            elif code == 1:
                result[h] = {"exists": False, "error": None}  # test -d: not a directory
            else:
                result[h] = {
                    "exists": False,
                    "error": err.strip() or f"check failed (exit {code})",
                }
        return result

    def _open_source_conn(
        self, source: dict[str, Any], password: str | None, cfg: dict[str, Any] | None
    ) -> tuple[Any, Callable[[], tuple[Any, Callable[[], None]]], Callable[[], None]]:
        """Open a relay source — a configured host, or an ad-hoc host reached via
        a configured jump (key/agent first, then ``password``).

        Returns ``(sftp, session_factory, release)``:

        * ``sftp`` — for ``stat()`` and the unfiltered straight-through path.
        * ``session_factory`` — ``() -> (channel, chan_release)``; opens an exec
          channel on the *same* transport, for the source-side filter path. The
          caller drives ``channel`` and must call ``chan_release()`` when done
          (on the pooled path it frees a session slot; the transport stays warm).
        * ``release`` — tears down ``sftp`` and the transport/tunnel.
        """
        if source.get("kind") == "host":
            entry = self._host_entry(source["host"])
            if entry is None or not entry.get("ssh"):
                raise OSError(f"{source.get('host')} is not a reachable remote host")
            ssh_target = entry["ssh"]
            identity = entry.get("identityFile")
            sftp, sftp_release = self._open_sftp(ssh_target, identity, cfg)

            def session_factory() -> tuple[Any, Callable[[], None]]:
                pool = self._ssh_pool
                if pool is not None:
                    return pool.open_session(
                        ssh_target, identity_file=identity, global_ssh_config=cfg
                    )
                from .executor import _make_ssh_client

                client = _make_ssh_client(ssh_target, identity, cfg)
                transport = client.get_transport()
                if transport is None:
                    client.close()
                    raise OSError("source transport unavailable")
                return transport.open_session(), client.close

            return sftp, session_factory, sftp_release

        # ad-hoc: tunnel a direct-tcpip channel through the configured jump (warm
        # pooled transport), then SSH to the target over it (keys/agent, then password).
        import paramiko

        from .executor import _apply_host_key_policy

        jump_entry = self._host_entry(source["jump"])
        if jump_entry is None or not jump_entry.get("ssh"):
            raise OSError(f"jump host {source.get('jump')} is not reachable")
        target = str(source["host"])
        port = int(source.get("port") or 22)

        chan, chan_release, jump_client = self._jump_channel(
            jump_entry, target, port, cfg
        )

        tclient = paramiko.SSHClient()
        _apply_host_key_policy(tclient, cfg or {})
        try:
            tclient.connect(
                hostname=target,
                port=port,
                username=source.get("user") or None,
                password=password or None,
                sock=chan,
                allow_agent=True,
                look_for_keys=True,
                timeout=float((cfg or {}).get("connect_timeout", 10)),
            )
        except Exception:
            chan_release()
            if jump_client is not None:
                jump_client.close()
            raise
        sftp = tclient.open_sftp()

        def adhoc_session_factory() -> tuple[Any, Callable[[], None]]:
            transport = tclient.get_transport()
            if transport is None:
                raise OSError("source transport closed")
            # The tunneled client has no per-channel slot to free (that's a pool
            # concept), so the release is a no-op — the channel is closed by the
            # filtered loop and the client by ``release`` below.
            return transport.open_session(), (lambda: None)

        def release() -> None:
            for c in (sftp, tclient):
                try:
                    c.close()
                except Exception:
                    pass
            chan_release()
            if jump_client is not None:
                try:
                    jump_client.close()
                except Exception:
                    pass

        return sftp, adhoc_session_factory, release

    def _jump_channel(
        self,
        jump_entry: dict[str, Any],
        target: str,
        port: int,
        cfg: dict[str, Any] | None,
    ) -> tuple[Any, Callable[[], None], Any]:
        """Open a direct-tcpip channel to ``target:port`` through the jump.

        Uses the pooled (warm) jump transport when pooling is on — returns
        ``(channel, release, None)``; otherwise opens a fresh jump client and
        returns it as the third element for the caller to close.
        """
        pool = self._ssh_pool
        if pool is not None:
            chan, release = pool.open_direct_tcpip(
                jump_entry["ssh"],
                target,
                port,
                identity_file=jump_entry.get("identityFile"),
                global_ssh_config=cfg,
            )
            return chan, release, None

        from .executor import _make_ssh_client

        jump_client = _make_ssh_client(
            jump_entry["ssh"], jump_entry.get("identityFile"), cfg
        )
        transport = jump_client.get_transport()
        if transport is None:
            jump_client.close()
            raise OSError("jump transport unavailable")
        chan = transport.open_channel("direct-tcpip", (target, port), ("127.0.0.1", 0))
        return chan, (lambda: None), jump_client

    def _relay_one(
        self,
        src_sftp: Any,
        dest_sftp: Any,
        item: Any,
        tid: str,
        idx: int,
        count: int,
        cancel_event: threading.Event,
    ) -> None:
        """Stream one file source→dest through the console, in chunks."""
        size = int(src_sftp.stat(item.src).st_size)
        transferred = 0
        last = 0.0
        with (
            src_sftp.open(item.src, "rb") as fsrc,
            dest_sftp.open(item.dst, "wb") as fdst,
        ):
            try:
                fsrc.prefetch(size)
            except Exception:
                pass
            while True:
                if cancel_event.is_set():
                    raise RuntimeError("cancelled")
                chunk = fsrc.read(32768)
                if not chunk:
                    break
                fdst.write(chunk)
                transferred += len(chunk)
                now = time.monotonic()
                if now - last >= 0.15 or transferred >= size:
                    last = now
                    self._dispatch(
                        "runspec:transfer_progress",
                        {
                            "id": tid,
                            "file": item.name,
                            "index": idx,
                            "count": count,
                            "transferred": transferred,
                            "total": size,
                        },
                    )

    def _run_filter_stream(
        self,
        session_factory: Callable[[], tuple[Any, Callable[[], None]]],
        cmd: str,
        dst_open: Callable[[], Any],
        dst_remove: Callable[[], None],
        in_size: int,
        item: Any,
        tid: str,
        idx: int,
        count: int,
        cancel_event: threading.Event,
    ) -> None:
        """Run ``cmd`` on the source over an exec channel, streaming its stdout
        into the destination ``dst_open()`` writes to. Shared by the relay and
        download filter paths — only the destination sink differs.

        Output size is unknown up front, so progress is reported as bytes written
        with ``indeterminate: True`` (``total`` carries the input size, for
        context only). A pipeline that exits non-zero raises — except exit code 1
        with no stderr, which is grep's "no lines matched" and yields a legitimate
        empty result. On any failure or cancel the partial destination is removed.
        """
        channel, chan_release = session_factory()
        STDERR_CAP = 65536
        try:
            channel.set_combine_stderr(False)
            channel.exec_command(cmd)
            transferred = 0
            last = 0.0
            stderr_buf = b""

            def tick() -> None:
                self._dispatch(
                    "runspec:transfer_progress",
                    {
                        "id": tid,
                        "file": item.name,
                        "index": idx,
                        "count": count,
                        "transferred": transferred,
                        "total": in_size,
                        "indeterminate": True,
                    },
                )

            f = dst_open()
            try:
                while True:
                    if cancel_event.is_set():
                        raise RuntimeError("cancelled")
                    got = False
                    if channel.recv_ready():
                        chunk = channel.recv(32768)
                        if chunk:
                            f.write(chunk)
                            transferred += len(chunk)
                            got = True
                    if channel.recv_stderr_ready():
                        err = channel.recv_stderr(32768)
                        if err and len(stderr_buf) < STDERR_CAP:
                            stderr_buf += err[: STDERR_CAP - len(stderr_buf)]
                        got = got or bool(err)
                    now = time.monotonic()
                    if got and now - last >= 0.15:
                        last = now
                        tick()
                    if channel.exit_status_ready():
                        while True:  # drain any buffered output after exit
                            chunk = channel.recv(32768)
                            if not chunk:
                                break
                            f.write(chunk)
                            transferred += len(chunk)
                        while True:
                            err = channel.recv_stderr(32768)
                            if not err:
                                break
                            if len(stderr_buf) < STDERR_CAP:
                                stderr_buf += err[: STDERR_CAP - len(stderr_buf)]
                        break
                    if not got:
                        time.sleep(0.01)
            finally:
                f.close()

            code = channel.recv_exit_status()
            stderr_text = stderr_buf.decode("utf-8", errors="replace").strip()
            # grep returns 1 for "no lines matched" — a legitimately empty result,
            # not a failure. Distinguish it from a real error by the absence of any
            # diagnostic on stderr.
            if code != 0 and not (code == 1 and not stderr_text):
                raise RuntimeError(stderr_text or f"filter exited {code}")
            tick()  # final count
        except BaseException:
            try:
                dst_remove()
            except Exception:
                pass
            raise
        finally:
            try:
                channel.close()
            except Exception:
                pass
            chan_release()

    def _run_filter_stream_local(
        self,
        rendered_pipeline: str,
        src_path: str,
        dst_open: Callable[[], Any],
        dst_remove: Callable[[], None],
        in_size: int,
        item: Any,
        tid: str,
        idx: int,
        count: int,
        cancel_event: threading.Event,
    ) -> None:
        """Filter a *local* file through ``rendered_pipeline`` (run via ``sh -c``
        with the file on stdin) and stream its stdout into the destination
        ``dst_open()`` writes to. The local-source counterpart of
        ``_run_filter_stream`` — used for an upload (push), where the source host
        is this machine. Same exit-code / cleanup / indeterminate-progress
        semantics. Requires a POSIX shell on PATH (raises otherwise)."""
        import shutil
        import subprocess

        if shutil.which("sh") is None:
            raise RuntimeError("filtering on upload needs a POSIX shell (sh) on PATH")

        transferred = 0
        last = 0.0

        def tick() -> None:
            self._dispatch(
                "runspec:transfer_progress",
                {
                    "id": tid,
                    "file": item.name,
                    "index": idx,
                    "count": count,
                    "transferred": transferred,
                    "total": in_size,
                    "indeterminate": True,
                },
            )

        src = open(src_path, "rb")
        try:
            proc = subprocess.Popen(
                ["sh", "-c", rendered_pipeline],
                stdin=src,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        finally:
            src.close()  # the child holds its own copy of the fd

        # Drain stderr on a side thread so a chatty pipeline can't deadlock by
        # filling the stderr pipe while we're busy reading stdout.
        stderr_chunks: list[bytes] = []

        def drain_err() -> None:
            assert proc.stderr is not None
            for block in iter(lambda: proc.stderr.read(32768), b""):
                if sum(len(c) for c in stderr_chunks) < 65536:
                    stderr_chunks.append(block)

        err_thread = threading.Thread(target=drain_err, daemon=True)
        err_thread.start()

        try:
            assert proc.stdout is not None
            f = dst_open()
            try:
                while True:
                    if cancel_event.is_set():
                        proc.kill()
                        raise RuntimeError("cancelled")
                    chunk = proc.stdout.read(32768)
                    if not chunk:
                        break
                    f.write(chunk)
                    transferred += len(chunk)
                    now = time.monotonic()
                    if now - last >= 0.15:
                        last = now
                        tick()
            finally:
                f.close()

            code = proc.wait()
            err_thread.join(timeout=5)
            stderr_text = (
                b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
            )
            if code != 0 and not (code == 1 and not stderr_text):
                raise RuntimeError(stderr_text or f"filter exited {code}")
            tick()
        except BaseException:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                dst_remove()
            except Exception:
                pass
            raise
        finally:
            for pipe in (proc.stdout, proc.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except Exception:
                    pass

    def _relay_one_filtered(
        self,
        session_factory: Callable[[], tuple[Any, Callable[[], None]]],
        dest_sftp: Any,
        item: Any,
        rendered_pipeline: str,
        src_sftp: Any,
        tid: str,
        idx: int,
        count: int,
        cancel_event: threading.Event,
    ) -> None:
        """Relay one file through a source-side filter, writing the filtered
        output to the destination host over SFTP."""
        from .fs_transfer import build_filter_command

        try:
            in_size = int(src_sftp.stat(item.src).st_size)
        except Exception:
            in_size = 0
        cmd = build_filter_command(rendered_pipeline, item.src)
        self._run_filter_stream(
            session_factory,
            cmd,
            dst_open=lambda: dest_sftp.open(item.dst, "wb"),
            dst_remove=lambda: dest_sftp.remove(item.dst),
            in_size=in_size,
            item=item,
            tid=tid,
            idx=idx,
            count=count,
            cancel_event=cancel_event,
        )

    def _transfer_one_filtered(
        self,
        session_factory: Callable[[], tuple[Any, Callable[[], None]]],
        src_sftp: Any,
        item: Any,
        rendered_pipeline: str,
        tid: str,
        idx: int,
        count: int,
        cancel_event: threading.Event,
    ) -> None:
        """Download one file through a source-side filter, writing the filtered
        output to the local destination file."""
        from .fs_transfer import build_filter_command

        try:
            in_size = int(src_sftp.stat(item.src).st_size)
        except Exception:
            in_size = 0
        cmd = build_filter_command(rendered_pipeline, item.src)
        self._run_filter_stream(
            session_factory,
            cmd,
            dst_open=lambda: open(item.dst, "wb"),
            dst_remove=lambda: os.remove(item.dst),
            in_size=in_size,
            item=item,
            tid=tid,
            idx=idx,
            count=count,
            cancel_event=cancel_event,
        )

    def _transfer_one_filtered_local(
        self,
        dest_sftp: Any,
        item: Any,
        rendered_pipeline: str,
        tid: str,
        idx: int,
        count: int,
        cancel_event: threading.Event,
    ) -> None:
        """Upload one file through a local filter: run the pipeline on this
        machine over the local source and write the filtered output to the remote
        destination over SFTP."""
        try:
            in_size = os.path.getsize(item.src)
        except OSError:
            in_size = 0
        self._run_filter_stream_local(
            rendered_pipeline,
            item.src,
            dst_open=lambda: dest_sftp.open(item.dst, "wb"),
            dst_remove=lambda: dest_sftp.remove(item.dst),
            in_size=in_size,
            item=item,
            tid=tid,
            idx=idx,
            count=count,
            cancel_event=cancel_event,
        )

    def start_relay(
        self,
        source: dict[str, Any],
        source_paths: list[str],
        dest_host: str,
        dest_dir: str,
        filter_name: str = "",
        filter_arg: str | None = None,
    ) -> dict[str, Any]:
        """Relay file(s) from one host to another, streaming through the console.

        ``source`` is ``{"kind": "host", "host": "<configured>"}`` or
        ``{"kind": "adhoc", "host", "port"?, "user"?, "jump": "<configured>",
        "password"?}``. A password (when given) connects to the ad-hoc target only
        and is **never** stored, logged, or pooled. ``filter_name`` selects a saved
        source-side filter (``filter_arg`` is the runtime value for its ``{{arg}}``
        placeholder); when set, the file is filtered on the source host and only
        the output is relayed. Returns ``{"id": ...}`` and streams the usual
        transfer events, or ``{"error": ...}``.
        """
        from .fs_transfer import relay_items, render_pipeline

        try:
            items = relay_items(source_paths, dest_dir)
        except ValueError as e:
            return {"error": str(e)}

        dest_entry = self._host_entry(dest_host)
        if dest_entry is None or not dest_entry.get("ssh"):
            return {"error": f"{dest_host} is not a reachable remote host"}

        kind = source.get("kind")
        if kind not in ("host", "adhoc") or not source.get("host"):
            return {"error": "no source host specified"}
        if kind == "adhoc" and not source.get("jump"):
            return {"error": "no jump host selected"}

        rendered = None
        if filter_name:
            filt = self._filter_entry(filter_name)
            if filt is None:
                return {"error": f"unknown filter: {filter_name}"}
            try:
                rendered = render_pipeline(filt.get("pipeline", ""), filter_arg)
            except ValueError as e:
                return {"error": str(e)}

        # Pull the password out so it never lands in _in_flight / logs.
        password = source.pop("password", None)
        cfg = self.get_config().get("ssh")

        tid = uuid.uuid4().hex[:12]
        cancel_event = threading.Event()
        with self._lock:
            self._in_flight[tid] = {
                "id": tid,
                "runnable": "relay",
                "group": "",
                "host": f"{source.get('host')} → {dest_host}",
                "operator": self._current_user(),
                "runAs": "",
                "startedAt": _iso_now(),
                "args": {
                    "source": source.get("host"),
                    "via": source.get("jump"),
                    "dest": dest_host,
                    "files": len(items),
                    # The filter name is recorded for the activity panel; the
                    # rendered pipeline / runtime value are not (avoids logging
                    # operator identifiers).
                    **({"filter": filter_name} if filter_name else {}),
                },
                _CANCEL_KEY: cancel_event,
            }

        def run() -> None:
            ok, error, done = True, None, 0
            src_release: Callable[[], None] | None = None
            dest_release: Callable[[], None] | None = None
            count = len(items)
            try:
                src_sftp, session_factory, src_release = self._open_source_conn(
                    source, password, cfg
                )
                dest_sftp, dest_release = self._open_sftp(
                    dest_entry["ssh"], dest_entry.get("identityFile"), cfg
                )
                for idx, item in enumerate(items):
                    if cancel_event.is_set():
                        ok, error = False, "cancelled"
                        break
                    try:
                        if rendered is not None:
                            self._relay_one_filtered(
                                session_factory,
                                dest_sftp,
                                item,
                                rendered,
                                src_sftp,
                                tid,
                                idx,
                                count,
                                cancel_event,
                            )
                        else:
                            self._relay_one(
                                src_sftp, dest_sftp, item, tid, idx, count, cancel_event
                            )
                        done += 1
                    except Exception as e:
                        ok, error = False, str(e)
                        self._dispatch(
                            "runspec:transfer_progress",
                            {
                                "id": tid,
                                "file": item.name,
                                "index": idx,
                                "count": count,
                                "transferred": 0,
                                "total": 0,
                                "error": str(e),
                            },
                        )
            except Exception as e:
                ok, error = False, str(e)
            finally:
                for r in (src_release, dest_release):
                    if r:
                        r()
                with self._lock:
                    self._in_flight.pop(tid, None)
            logger.info("relay %s: %d/%d ok=%s", tid, done, count, ok)
            self._emit_transfer_end(
                tid,
                {"id": tid, "ok": ok, "files": done, "total": count, "error": error},
            )

        threading.Thread(target=run, daemon=True).start()
        return {"id": tid}

    def list_dir_adhoc(
        self,
        jump: str,
        host: str,
        path: str = "",
        user: str | None = None,
        password: str | None = None,
        port: int | None = None,
        dirs_only: bool = False,
    ) -> dict[str, Any]:
        """List a directory on an ad-hoc host (reached via ``jump``) for the relay
        source browser. Same return shape as :meth:`list_dir`.

        The target connection is cached for the browse session (keyed by jump /
        host / user / port) so navigation on a slow link only handshakes the
        target once. The password is used only when (re)establishing and is never
        stored or logged."""
        if not host:
            return {"error": "no source host specified"}
        if not jump:
            return {"error": "no jump host selected"}
        key = (jump, host, user or "", int(port or 22))
        cfg = self.get_config().get("ssh")
        for attempt in (1, 2):
            try:
                sftp = self._get_adhoc_sftp(key, jump, host, user, password, port, cfg)
            except Exception as e:
                return {"error": str(e)}
            try:
                from .fs_browse import list_sftp

                return list_sftp(sftp, path, dirs_only=dirs_only)
            except Exception as e:
                # A reused connection may have dropped — drop it and retry once.
                self._close_adhoc_key(key)
                if attempt == 2:
                    return {"error": str(e)}
        return {"error": "unreachable"}

    def _get_adhoc_sftp(
        self,
        key: tuple[Any, ...],
        jump: str,
        host: str,
        user: str | None,
        password: str | None,
        port: int | None,
        cfg: dict[str, Any] | None,
    ) -> Any:
        """Return a live cached ad-hoc SFTP for ``key``, establishing one if needed."""
        with self._lock:
            entry = self._adhoc_conns.get(key)
        if entry is not None and self._adhoc_alive(entry["sftp"]):
            entry["last_used"] = time.monotonic()
            return entry["sftp"]

        source = {
            "kind": "adhoc",
            "host": host,
            "jump": jump,
            "user": user,
            "port": port,
        }
        # Browsing only needs the sftp; the exec session_factory is for filters.
        sftp, _session_factory, release = self._open_source_conn(source, password, cfg)
        with self._lock:
            old = self._adhoc_conns.get(key)
            self._adhoc_conns[key] = {
                "sftp": sftp,
                "release": release,
                "last_used": time.monotonic(),
            }
        if old is not None:
            try:
                old["release"]()
            except Exception:
                pass
        return sftp

    @staticmethod
    def _adhoc_alive(sftp: Any) -> bool:
        try:
            chan = sftp.get_channel()
            transport = chan.get_transport() if chan is not None else None
            return bool(transport is not None and transport.is_active())
        except Exception:
            return False

    def _close_adhoc_key(self, key: tuple[Any, ...]) -> None:
        with self._lock:
            entry = self._adhoc_conns.pop(key, None)
        if entry is not None:
            try:
                entry["release"]()
            except Exception:
                pass

    def close_adhoc(self) -> None:
        """Tear down all cached ad-hoc browse connections (relay dialog close / shutdown)."""
        with self._lock:
            entries = list(self._adhoc_conns.values())
            self._adhoc_conns.clear()
        for e in entries:
            try:
                e["release"]()
            except Exception:
                pass

    def list_dir_unsaved(
        self,
        hostname: str,
        path: str = "",
        user: str | None = None,
        port: int | None = None,
        identity_file: str | None = None,
        dirs_only: bool = False,
    ) -> dict[str, Any]:
        """List a directory on a host that isn't saved yet — the Settings → Jump
        Hosts add/edit form — so the runspec-path picker can browse it directly.

        Same return shape as :meth:`list_dir`. Connects **straight** to the host
        (no jump) from the form's ``hostname``/``user``/``port``/``identity_file``,
        reusing the shared client builder so the browse goes through the same
        key/agent → ``[ssh] proxy`` → ``~/.ssh/config`` → host-key handling as
        every saved host. The connection is cached for the browse session (keyed
        by the target) and torn down by :meth:`close_adhoc` when the form closes.
        """
        if not hostname:
            return {"error": "no hostname specified"}
        target = f"{user}@{hostname}" if user else hostname
        if port:
            target = f"{target}:{int(port)}"
        key = ("__unsaved__", target, identity_file or "")
        cfg = self.get_config().get("ssh")
        for attempt in (1, 2):
            try:
                sftp = self._get_unsaved_sftp(key, target, identity_file, cfg)
            except Exception as e:
                return {"error": str(e)}
            try:
                from .fs_browse import list_sftp

                return list_sftp(sftp, path, dirs_only=dirs_only)
            except Exception as e:
                # A reused connection may have dropped — drop it and retry once.
                self._close_adhoc_key(key)
                if attempt == 2:
                    return {"error": str(e)}
        return {"error": "unreachable"}

    def _get_unsaved_sftp(
        self,
        key: tuple[Any, ...],
        target: str,
        identity_file: str | None,
        cfg: dict[str, Any] | None,
    ) -> Any:
        """Return a live cached direct SFTP for an unsaved host, opening one if needed.

        Owns a dedicated (non-pooled) client — the unsaved host has no pool entry
        yet — cached alongside the relay ad-hoc connections so ``close_adhoc``
        cleans it up too."""
        with self._lock:
            entry = self._adhoc_conns.get(key)
        if entry is not None and self._adhoc_alive(entry["sftp"]):
            entry["last_used"] = time.monotonic()
            return entry["sftp"]

        from .executor import _make_ssh_client

        client = _make_ssh_client(target, identity_file, cfg)
        sftp = client.open_sftp()

        def release() -> None:
            for c in (sftp, client):
                try:
                    c.close()
                except Exception:
                    pass

        with self._lock:
            old = self._adhoc_conns.get(key)
            self._adhoc_conns[key] = {
                "sftp": sftp,
                "release": release,
                "last_used": time.monotonic(),
            }
        if old is not None:
            try:
                old["release"]()
            except Exception:
                pass
        return sftp

    # ── history ───────────────────────────────────────────────────────────────

    def get_history(
        self, host: str, runnable: str | None = None
    ) -> list[dict[str, Any]]:
        entry = self._host_entry(host)
        if entry is None:
            return []
        if entry.get("ssh"):
            return self._get_remote_history(entry, runnable)
        paths = _paths(entry)
        records: list[dict[str, Any]] = []
        globs = _log_globs(runnable)
        # Scan every venv's logs dir AND ~/logs. A runnable writes to ~/logs when
        # its venv dir isn't writable (managed machines) — but the venv dir is
        # still *created* on the way to that fallback, so a "venv dir, else
        # ~/logs" check reads an empty venv dir and misses the real records in
        # ~/logs. The Logs tab works because the CLI's log_dirs() reads both.
        log_dirs = [Path(rp).parent.parent / "logs" for rp in paths if rp]
        log_dirs.append(Path.home() / "logs")
        matches: set[Path] = set()
        seen_dirs: set[Path] = set()
        for log_dir in log_dirs:
            if log_dir in seen_dirs or not log_dir.exists():
                continue
            seen_dirs.add(log_dir)
            matches |= {f for pat in globs for f in log_dir.glob(pat)}
        for log_file in sorted(matches, key=lambda p: p.stat().st_mtime, reverse=True):
            records.extend(_parse_log(log_file, host))
        records.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return records[:200]

    def get_history_all(self) -> list[dict[str, Any]]:
        """History across every host — local plus *connected* remotes — newest
        first. Disconnected remotes are skipped so a slow/offline host can't hang
        the read (the History tab uses this so a run shows up regardless of which
        host it ran on; per-host scoping stays for Forms/Logs/etc.)."""
        with self._lock:
            remotes = list(self._hosts)
            connected = dict(self._connected_cache)
        names = ["local"]
        names += [
            h["name"] for h in remotes if h.get("name") and connected.get(h["name"])
        ]
        records: list[dict[str, Any]] = []
        for name in names:
            try:
                records.extend(self.get_history(name))
            except Exception:
                logger.exception("history: read failed for host %s", name)
        records.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return records[:300]

    def _remote_log_dirs(
        self, entry: dict[str, Any], rp: str, timeout: int = 30
    ) -> list[str]:
        """Real log directories for one remote venv, asked of the remote runspec.

        ``logs status --json`` reports ``log_dirs()`` — the actual dirs the Logs
        tab reads — so History/Analytics don't guess the dir from the binary
        path (which is wrong for pipx / ~/.local / system installs, where the
        runnable's ``sys.prefix`` isn't ``{rp}/../..``). Falls back to the
        path-derived dir plus ``$HOME/logs``. Cached per venv path for the
        session (only when the query actually resolved, so a transient failure
        doesn't pin the fallback)."""
        from pathlib import PurePosixPath
        import shlex

        cached = self._remote_logdir_cache.get(rp)
        if cached is not None:
            return cached

        dirs: list[str] = []

        def _add(d: str) -> None:
            if d and d not in dirs:
                dirs.append(d)

        resolved = False
        cmd = " ".join(shlex.quote(p) for p in [rp, "logs", "status", "--json"])
        try:
            code, out, _ = self._ssh_run(
                entry["ssh"],
                cmd,
                identity_file=entry.get("identityFile"),
                timeout=timeout,
            )
            if code == 0 and out.strip():
                for d in json.loads(out).get("dirs", []):
                    _add(str(d))
                resolved = True
        except Exception:
            logger.debug("remote log-dir query failed for %s", rp, exc_info=True)
        _add(str(PurePosixPath(rp).parent.parent / "logs"))  # path-derived fallback
        _add("$HOME/logs")
        if resolved:
            self._remote_logdir_cache[rp] = dirs
        return dirs

    def _get_remote_history(
        self, entry: dict[str, Any], runnable: str | None
    ) -> list[dict[str, Any]]:
        ssh = entry["ssh"]
        idf = entry.get("identityFile")
        paths = _paths(entry)
        # Ask the remote runspec where its logs actually are (per venv), so a
        # non-standard install (pipx / ~/.local) whose logs the Logs tab finds
        # isn't missed by History. Union across venvs.
        log_dirs: list[str] = []
        for rp in paths:
            if not rp:
                continue
            for d in self._remote_log_dirs(entry, rp):
                if d not in log_dirs:
                    log_dirs.append(d)
        if not log_dirs:
            log_dirs = ["$HOME/logs"]
        script = _remote_log_script(log_dirs, runnable)
        _, stdout, _ = self._ssh_run(ssh, script, identity_file=idf, timeout=20)
        records: list[dict[str, Any]] = []
        current_name: str | None = None
        current_lines: list[str] = []
        for line in stdout.splitlines():
            if line.startswith("\x00RUNSPEC_LOG:"):
                if current_name is not None:
                    records.extend(
                        _parse_log_text(
                            current_name, "\n".join(current_lines), entry["name"]
                        )
                    )
                current_name = line[len("\x00RUNSPEC_LOG:") :]
                current_lines = []
            else:
                current_lines.append(line)
        if current_name is not None:
            records.extend(
                _parse_log_text(current_name, "\n".join(current_lines), entry["name"])
            )
        records.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return records[:200]

    # ── analytics ─────────────────────────────────────────────────────────────

    def _collect_host_records(
        self, entry: dict[str, Any], runnable: str | None
    ) -> list[dict[str, Any]]:
        """Parse every run_summary across all of a host's venvs (uncapped).

        Unlike get_history this keeps the rich ``extra`` fields and tags each
        record with its venv ``group``. Local hosts read the log files directly;
        remote hosts stream them over SSH using the same framing as
        _get_remote_history. Raises on hard failure so the caller can mark the
        host partial.
        """
        from pathlib import PurePosixPath

        paths = _paths(entry)
        records: list[dict[str, Any]] = []
        globs = _log_globs(runnable)

        if entry.get("ssh"):
            ssh = entry["ssh"]
            idf = entry.get("identityFile")
            # Ask the remote runspec for each venv's real log dirs (same as
            # History) so non-standard installs aren't missed, tagging each dir
            # with that venv's group.
            remote_dir_groups: list[tuple[str, str]] = []
            for rp in paths:
                if not rp:
                    continue
                group = PurePosixPath(rp).parent.parent.name
                for d in self._remote_log_dirs(entry, rp):
                    remote_dir_groups.append((d, group))
            seen_remote: set[str] = set()
            for log_dir, group in remote_dir_groups:
                if log_dir in seen_remote:
                    continue
                seen_remote.add(log_dir)
                script = _remote_log_script(log_dir, runnable)
                code, stdout, stderr = self._ssh_run(
                    ssh, script, identity_file=idf, timeout=45
                )
                if code != 0 and not stdout:
                    raise RuntimeError(stderr.strip() or f"ssh exit {code}")
                current_name: str | None = None
                current_lines: list[str] = []

                def _flush(group: str = group) -> None:
                    if current_name is not None:
                        for rec in _parse_log_text(
                            current_name,
                            "\n".join(current_lines),
                            entry["name"],
                            include_extra=True,
                        ):
                            rec["group"] = group
                            records.append(rec)

                for line in stdout.splitlines():
                    if line.startswith("\x00RUNSPEC_LOG:"):
                        _flush()
                        current_name = line[len("\x00RUNSPEC_LOG:") :]
                        current_lines = []
                    else:
                        current_lines.append(line)
                _flush()
            return records

        # Local host (distinct Path var — `log_dir` above is a str for the
        # remote branch, so reusing the name would muddle its type). Scan each
        # venv's logs dir (tagged with its venv group) AND ~/logs (the writable
        # fallback when a venv dir isn't writable) so runs that landed there
        # aren't missed — same defect as get_history. ~/logs records are tagged
        # with the primary venv's group (correct in the common single-venv case).
        dir_groups: list[tuple[Path, str]] = [
            (Path(rp).parent.parent / "logs", venv_name(rp)) for rp in paths if rp
        ]
        fallback_group = venv_name(paths[0]) if paths and paths[0] else ""
        dir_groups.append((Path.home() / "logs", fallback_group))
        seen_dirs: set[Path] = set()
        for local_dir, group in dir_groups:
            if local_dir in seen_dirs or not local_dir.exists():
                continue
            seen_dirs.add(local_dir)
            matches = {f for pat in globs for f in local_dir.glob(pat)}
            for log_file in matches:
                for rec in _parse_log(log_file, entry["name"], include_extra=True):
                    rec["group"] = group
                    records.append(rec)
        return records

    def get_analytics(
        self,
        hosts: list[str] | None = None,
        since_days: int = 30,
        runnable: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate run history across the fleet into drill-ready analytics.

        Scans full (uncapped) audit logs across the requested hosts' venvs,
        buckets by day and by every drill dimension, and surfaces the rich
        run_summary fields. Heavy work (parsing, SSH) happens here so the
        frontend can re-filter the compact ``buckets`` instantly.
        """
        from datetime import datetime, timedelta, timezone

        if not hosts or "all" in hosts:
            target_names = ["local"] + [
                h["name"] for h in self._hosts if h.get("name") != "local"
            ]
        else:
            target_names = list(hosts)

        entries: list[dict[str, Any]] = []
        for name in target_names:
            e = self._host_entry(name)
            if e is not None:
                entries.append(e)

        now = datetime.now(timezone.utc)
        since_dt = now - timedelta(days=max(0, since_days - 1))
        since = since_dt.date().isoformat()
        until = now.date().isoformat()

        all_records: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        lock = threading.Lock()

        def collect(entry: dict[str, Any]) -> None:
            try:
                recs = self._collect_host_records(entry, runnable)
            except Exception as exc:  # noqa: BLE001 — degrade gracefully per host
                with lock:
                    errors.append({"host": entry["name"], "message": str(exc)})
                return
            with lock:
                all_records.extend(recs)

        threads = [
            threading.Thread(target=collect, args=(e,), daemon=True) for e in entries
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Window filter (UTC day, inclusive lower bound)
        in_window = [r for r in all_records if r.get("ts", "")[:10] >= since]

        return _aggregate_analytics(
            in_window, since, until, partial=bool(errors), errors=errors
        )

    # ── logs management ───────────────────────────────────────────────────────
    #
    # The Logs tab drives `runspec logs <verb> --json` against each venv — local
    # via subprocess, remote via SSH — the same uniform, version-correct
    # interface discovery uses (`runspec local`). status is read-only inventory;
    # prune/compact mutate, so the UI gates them behind a --dry-run preview.

    def logs_status(
        self, host: str, runnable: str | None = None
    ) -> list[dict[str, Any]]:
        """Per-venv per-runnable log inventory for a host (read-only).

        Returns one dict per venv: ``{group, ok, dirs, runnables, total_bytes,
        total_files}`` (or ``{group, ok: False, error}`` on failure). Drives
        ``runspec logs status --json``.
        """
        argv = ["logs", "status"]
        if runnable:
            argv.append(runnable)
        argv.append("--json")

        out: list[dict[str, Any]] = []
        for group, rp in self._venvs(host):
            code, stdout, stderr = self._run_logs(host, rp, argv, timeout=30)
            if code != 0 and not stdout.strip():
                out.append(
                    {
                        "group": group,
                        "ok": False,
                        "error": stderr.strip() or f"exit {code}",
                    }
                )
                continue
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError:
                out.append(
                    {"group": group, "ok": False, "error": "unparseable status output"}
                )
                continue
            payload["group"] = group
            payload["ok"] = True
            out.append(payload)
        return out

    def logs_view(
        self,
        host: str,
        group: str,
        runnable: str,
        since: str | None = None,
        user: str | None = None,
        run: str | None = None,
    ) -> dict[str, Any]:
        """Merged per-invocation stream for one runnable in one venv.

        Returns ``{ok, records}`` where each record is a parsed JSON log line
        (timestamp-sorted, archives included). Drives ``runspec logs <runnable>
        --json``.
        """
        rp = self._venv_path(host, group)
        if not rp:
            return {"ok": False, "error": f"no venv '{group}' on {host}", "records": []}
        argv = ["logs", runnable, "--json"]
        if since:
            argv += ["--since", since]
        if user:
            argv += ["--user", user]
        if run:
            argv += ["--run", run]
        code, stdout, stderr = self._run_logs(host, rp, argv, timeout=45)
        if code != 0 and not stdout.strip():
            return {
                "ok": False,
                "error": stderr.strip() or f"exit {code}",
                "records": [],
            }
        records: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return {"ok": True, "records": records}

    def logs_prune(
        self,
        host: str,
        group: str,
        runnable: str | None = None,
        older_than: str | None = None,
        max_files: int | None = None,
        max_total_size: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Prune old per-invocation files in one venv. Drives ``runspec logs prune``.

        At least one policy is required. Returns the engine's structured result
        (``{dry_run, count, freed_bytes, deleted}``) plus ``{group, ok}``.
        """
        if older_than is None and max_files is None and max_total_size is None:
            return {
                "ok": False,
                "error": "prune needs at least one policy (--older-than / --max-files / --max-total-size)",
            }
        rp = self._venv_path(host, group)
        if not rp:
            return {"ok": False, "error": f"no venv '{group}' on {host}"}
        argv = ["logs", "prune"]
        if runnable:
            argv.append(runnable)
        if older_than:
            argv += ["--older-than", older_than]
        if max_files is not None:
            argv += ["--max-files", str(max_files)]
        if max_total_size:
            argv += ["--max-total-size", max_total_size]
        if dry_run:
            argv.append("--dry-run")
        argv.append("--json")
        return self._logs_action(host, rp, group, argv, timeout=120)

    def logs_compact(
        self,
        host: str,
        group: str,
        runnable: str | None = None,
        older_than: str | None = None,
        gzip: bool = True,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Compact old per-invocation files into archives in one venv.

        ``--older-than`` is required. Returns the engine's structured result
        (``{dry_run, compacted, archives}``) plus ``{group, ok}``.
        """
        if not older_than:
            return {"ok": False, "error": "compact requires --older-than (e.g. 7d)"}
        rp = self._venv_path(host, group)
        if not rp:
            return {"ok": False, "error": f"no venv '{group}' on {host}"}
        argv = ["logs", "compact"]
        if runnable:
            argv.append(runnable)
        argv += ["--older-than", older_than]
        if gzip:
            argv.append("--gzip")
        if dry_run:
            argv.append("--dry-run")
        argv.append("--json")
        return self._logs_action(host, rp, group, argv, timeout=180)

    def _logs_action(
        self, host: str, rp: str, group: str, argv: list[str], timeout: int
    ) -> dict[str, Any]:
        """Run a prune/compact action and merge its JSON result with {group, ok}."""
        code, stdout, stderr = self._run_logs(host, rp, argv, timeout=timeout)
        if code != 0 and not stdout.strip():
            return {
                "group": group,
                "ok": False,
                "error": stderr.strip() or f"exit {code}",
            }
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return {"group": group, "ok": False, "error": "unparseable result output"}
        payload["group"] = group
        payload["ok"] = True
        return payload

    def _venvs(self, host: str) -> list[tuple[str, str]]:
        """(group, runspec_path) for every venv on a host."""
        entry = self._host_entry(host)
        if entry is None:
            return []
        return [(venv_name(rp), rp) for rp in _paths(entry) if rp]

    def _venv_path(self, host: str, group: str) -> str:
        """Resolve a host+venv group to its runspec binary path, or ''."""
        return next((rp for g, rp in self._venvs(host) if g == group), "")

    def _run_logs(
        self, host: str, rp: str, argv: list[str], timeout: int
    ) -> tuple[int, str, str]:
        """Run ``<rp> <argv...>`` on a venv — local subprocess or remote SSH.

        Mirrors discovery: local hosts shell out directly; remote hosts go over
        SSH with the path + args joined into a quoted command string.
        """
        import shlex

        entry = self._host_entry(host)
        if entry is None:
            return -1, "", f"unknown host {host}"

        if entry.get("ssh"):
            command = " ".join(shlex.quote(p) for p in [rp, *argv])
            return self._ssh_run(
                entry["ssh"],
                command,
                identity_file=entry.get("identityFile"),
                timeout=timeout,
            )

        import subprocess

        try:
            proc = subprocess.run(
                [rp, *argv],
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            return -1, "", f"runspec not found at {rp}"
        except subprocess.TimeoutExpired:
            return -1, "", f"timed out after {timeout}s"
        return proc.returncode, proc.stdout, proc.stderr

    # ── schedules ─────────────────────────────────────────────────────────────

    def get_schedules(self) -> list[dict[str, Any]]:
        path = hosts_path().parent / "runspec_schedules.toml"
        if not path.exists():
            return []
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return data.get("schedule", [])

    def create_schedule(self, data: dict[str, Any]) -> None:
        path = hosts_path().parent / "runspec_schedules.toml"
        schedules = self.get_schedules()
        schedules.append(data)
        _write_schedules(path, schedules)

    def delete_schedule(self, id: str) -> None:
        path = hosts_path().parent / "runspec_schedules.toml"
        schedules = [s for s in self.get_schedules() if s.get("id") != id]
        _write_schedules(path, schedules)

    # ── today digest ──────────────────────────────────────────────────────────

    def get_today(self, host: str, group: str) -> dict[str, Any] | None:
        entry = self._host_entry(host)
        if entry is None:
            return None
        paths = _paths(entry)
        rp = next(
            (p for p in paths if venv_name(p) == group), paths[0] if paths else ""
        )
        if not rp:
            return None
        today_file = Path(rp).parent.parent / "runspec_today.json"
        if not today_file.exists():
            return None
        try:
            return json.loads(today_file.read_text(encoding="utf-8"))
        except Exception:
            return None

    # ── config ────────────────────────────────────────────────────────────────

    def get_config(self) -> dict[str, Any]:
        return read_config()

    def config_dir(self) -> str:
        """Return the resolved config directory path (e.g. C:/Users/jason/AppData/Roaming/runspec-console)."""
        from .config import config_path

        return _normalize_path(str(config_path().parent))

    # ── version / docs ─────────────────────────────────────────────────────────

    def app_version(self) -> str:
        """The installed runspec-console version, for display in the UI."""
        from .version import installed_version

        return installed_version()

    def check_for_update(self) -> dict[str, Any]:
        """Compare the installed version against the newest one the venv's
        configured package index offers (PyPI, or an internal proxy/mirror if
        this venv's pip points at one). Best-effort.

        ``ok`` is False when the latest version couldn't be reached (offline /
        blocked network policy) — the UI then shows just the version with no
        update indicator. The check lives behind its own bridge method (it shells
        out to pip) so the version renders instantly while this resolves in the
        background.
        """
        from .version import installed_version, is_newer, latest_version

        current = installed_version()
        latest = latest_version()
        if latest is None:
            return {
                "ok": False,
                "current": current,
                "latest": None,
                "update_available": False,
            }
        return {
            "ok": True,
            "current": current,
            "latest": latest,
            "update_available": is_newer(latest, current),
        }

    def docs_url(self) -> str:
        """The documentation URL the docs icon opens (config-overridable)."""
        from .config import docs_url

        return docs_url()

    def open_docs(self) -> None:
        """Open the documentation site in the user's default browser."""
        try:
            webbrowser.open(self.docs_url())
        except Exception:
            logger.debug("open_docs failed", exc_info=True)

    def save_config(self, data: dict[str, Any]) -> None:
        old = self.get_config()
        ssh = data.get("ssh")
        if isinstance(ssh, dict):
            data = {**data, "ssh": _normalize_ssh_section(ssh)}
        write_config(data)
        self._adapter = None
        pool_before = self._ssh_pool
        self._apply_ssh_config(old.get("ssh"), data.get("ssh"))
        # Re-join the room if its [chat] config changed, or the SSH pool was
        # rebuilt (the supervisor's tunnel factory is bound to the pool instance).
        if old.get("chat") != data.get("chat") or self._ssh_pool is not pool_before:
            self.restart_chat()

    def _apply_ssh_config(
        self, old_ssh: dict[str, Any] | None, new_ssh: dict[str, Any] | None
    ) -> None:
        """React to an ``[ssh]`` change with the least disruption to live
        connections. The pool consumes only ``[ssh]`` (never ``[refresh]``), so
        an unrelated save (LLM, refresh tuning…) reaches here with no change and
        does nothing.

        A full rebuild — which drops every connection — happens only when
        pooling is toggled on/off or ``max_concurrent`` changes (the handshake
        semaphore can't be resized in place). Every other change is applied
        without a drop: behavioural tunables update in place, and
        connection-shaping fields (user / proxy / identity) migrate lazily via
        the connection key on next use.
        """
        if old_ssh == new_ssh:
            return
        from .ssh_pool import pool_settings

        old_s = pool_settings(old_ssh)
        new_s = pool_settings(new_ssh)
        if (old_s["enabled"], old_s["max_concurrent"]) != (
            new_s["enabled"],
            new_s["max_concurrent"],
        ):
            old_pool = self._ssh_pool
            self._ssh_pool = self._build_ssh_pool()
            if old_pool is not None:
                old_pool.close_all()
        elif self._ssh_pool is not None:
            self._ssh_pool.update_settings(new_s)

    def _generate_keypair_to_path(self, dest: Path) -> dict[str, Any]:
        """Write a new ed25519 key pair to dest (OpenSSH PEM + PPK v2). Does not touch config."""
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )
            from cryptography.hazmat.primitives import serialization as _serial
        except ImportError:
            return {
                "ok": False,
                "public_key": "",
                "key_path": "",
                "message": "pip install cryptography — required for key generation",
            }
        dest.parent.mkdir(parents=True, exist_ok=True)
        private_key = Ed25519PrivateKey.generate()

        dest.write_bytes(
            private_key.private_bytes(
                encoding=_serial.Encoding.PEM,
                format=_serial.PrivateFormat.OpenSSH,
                encryption_algorithm=_serial.NoEncryption(),
            )
        )

        raw_priv = private_key.private_bytes(
            encoding=_serial.Encoding.Raw,
            format=_serial.PrivateFormat.Raw,
            encryption_algorithm=_serial.NoEncryption(),
        )
        raw_pub = private_key.public_key().public_bytes(
            encoding=_serial.Encoding.Raw,
            format=_serial.PublicFormat.Raw,
        )
        _write_ppk_v2(Path(str(dest) + ".ppk"), raw_priv, raw_pub)

        pub = (
            private_key.public_key()
            .public_bytes(
                encoding=_serial.Encoding.OpenSSH,
                format=_serial.PublicFormat.OpenSSH,
            )
            .decode()
            .strip()
        )
        # Persist the public key next to the private key so it can be copied
        # again later (e.g. to authorise it on a host after the fact).
        try:
            Path(str(dest) + ".pub").write_text(pub + "\n", encoding="utf-8")
        except Exception:
            pass
        return {
            "ok": True,
            "public_key": pub,
            "key_path": _normalize_path(str(dest)),
            "message": "",
        }

    def generate_ssh_key(self) -> dict[str, Any]:
        """Generate a fresh ed25519 key pair, backing up any existing key.

        Commits to config immediately — use rotate_ssh_key() when you want the
        safe automated push to remote hosts before committing.
        """
        import os
        import time
        from datetime import datetime, timezone

        key_dir = Path(os.environ.get("APPDATA", "~")).expanduser() / "runspec-console"
        canonical = key_dir / "runspec_ed25519"

        if canonical.exists():
            backup = key_dir / f"runspec_ed25519.bak.{int(time.time())}"
            canonical.rename(backup)

        result = self._generate_keypair_to_path(canonical)
        if not result["ok"]:
            return {**result, "committed": False, "per_host": []}

        cfg = self.get_config()
        ssh_section = dict(cfg.get("ssh", {}))
        ssh_section["identityFile"] = _normalize_path(str(canonical))
        ssh_section["key_created_at"] = datetime.now(timezone.utc).isoformat()
        self.save_config({**cfg, "ssh": ssh_section})

        return {
            "ok": True,
            "committed": True,
            "per_host": [],
            "public_key": result["public_key"],
            "key_path": _normalize_path(str(canonical)),
            "message": f"Key generated at {_normalize_path(str(canonical))}",
        }

    def _public_key_for(self, identity_file: str) -> dict[str, Any]:
        """Resolve the OpenSSH public key line for an identity file.

        Reads the persisted ``<identityFile>.pub`` if present; otherwise derives
        it from the private key (and writes the ``.pub`` for next time). Returns
        ``{ok, public_key, message}`` — ``ok=False`` when no key is configured
        or the private file is missing. Shared by ``get_public_key`` (global key)
        and ``copy_ssh_id`` (a host's effective key, which may be a per-host
        override).
        """
        if not identity_file:
            return {"ok": False, "public_key": "", "message": "No SSH key configured."}
        priv = Path(identity_file).expanduser()
        pub_file = Path(str(priv) + ".pub")
        if pub_file.is_file():
            return {
                "ok": True,
                "public_key": pub_file.read_text(encoding="utf-8").strip(),
                "message": "",
            }
        if not priv.is_file():
            return {
                "ok": False,
                "public_key": "",
                "message": f"Key file not found: {identity_file}",
            }
        try:
            pub = _public_key_from_private(priv)
        except Exception as exc:
            return {
                "ok": False,
                "public_key": "",
                "message": f"Could not read public key: {exc}",
            }
        try:
            pub_file.write_text(pub + "\n", encoding="utf-8")
        except Exception:
            pass
        return {"ok": True, "public_key": pub, "message": ""}

    def get_public_key(self) -> dict[str, Any]:
        """Return the current SSH public key, so it can be copied at any time.

        Reads the persisted ``<identityFile>.pub`` if present; otherwise derives
        it from the private key (and writes the ``.pub`` for next time). This is
        what lets the UI show the public key long after generation, even for
        keys created before ``.pub`` files were written.
        """
        cfg = self.get_config()
        idf = (cfg.get("ssh") or {}).get("identityFile", "")
        return self._public_key_for(idf)

    def copy_ssh_id(self, host_name: str, password: str) -> dict[str, Any]:
        """Copy this machine's public key to a host's ``authorized_keys`` over a
        one-off password connection — the in-app equivalent of ``ssh-copy-id``.

        Bootstraps key-based SSH for a freshly added host: until our key is in
        the remote's ``~/.ssh/authorized_keys`` key auth fails and the host shows
        disconnected, forcing the user out to PuTTY/another client. This logs in
        once with the supplied password, appends the key (idempotently), then
        re-probes so the sidebar dot flips green. The password is used only for
        this single connection — never stored, cached, or logged.
        """
        entry = self._host_entry(host_name)
        if entry is None or not entry.get("ssh"):
            return {
                "ok": False,
                "connected": False,
                "message": f"{host_name} is not a reachable remote host",
            }
        if not (password or "").strip():
            return {
                "ok": False,
                "connected": False,
                "message": "Password required to copy the key.",
            }

        # Push the host's *effective* key — a per-host identityFile overrides the
        # global one, matching how _check_connected / _make_ssh_client pick the
        # key the host's own key-auth sessions will use. Pushing the wrong key
        # would leave the host red even after a "successful" copy.
        global_ssh = self.get_config().get("ssh") or {}
        effective_idf = entry.get("identityFile") or global_ssh.get("identityFile", "")
        key_res = self._public_key_for(effective_idf)
        if not key_res["ok"]:
            # No key at all → point the user at Generate key (we never auto-create
            # one here; a surprise key could orphan keys other hosts authorize).
            message = (
                "No SSH key configured — generate one first, then copy it."
                if not effective_idf
                else key_res["message"]
            )
            return {"ok": False, "connected": False, "message": message}
        public_key = key_res["public_key"]

        # Same idempotent push the key-rotation path uses (grep -qF || append).
        # public_key is single-quote-wrapped; an OpenSSH key contains no quotes.
        push_cmd = (
            f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
            f"grep -qF '{public_key}' ~/.ssh/authorized_keys || "
            f"printf '%s\\n' '{public_key}' >> ~/.ssh/authorized_keys"
        )

        import paramiko

        from .executor import _make_ssh_client, friendly_ssh_error

        # Connect with the password via the shared client builder so this goes
        # through the same [ssh] proxy / host-key / ssh_config handling as every
        # other connection — essential when the whole fleet sits behind an HTTP
        # CONNECT proxy. Password auth disables agent/key fallback internally.
        try:
            client = _make_ssh_client(entry["ssh"], None, global_ssh, password=password)
        except paramiko.AuthenticationException:
            return {
                "ok": False,
                "connected": False,
                "message": "Authentication failed — check the password.",
            }
        except paramiko.BadHostKeyException:
            return {
                "ok": False,
                "connected": False,
                "message": "Remote host key changed — refusing to connect.",
            }
        except Exception as exc:  # noqa: BLE001 — surface a friendly one-liner
            return {"ok": False, "connected": False, "message": friendly_ssh_error(exc)}

        try:
            _stdin, stdout, stderr = client.exec_command(push_cmd)
            exit_code = stdout.channel.recv_exit_status()
            err = stderr.read().decode(errors="replace")
        finally:
            client.close()

        if exit_code != 0:
            return {
                "ok": False,
                "connected": False,
                "message": (
                    "Could not update authorized_keys: "
                    f"{err.strip() or f'exit {exit_code}'}"
                ),
            }

        # Re-probe just this host so we can report the result now and flip the
        # dot immediately; a background full cycle refreshes its runnables too.
        connected = self._check_connected(entry)
        with self._lock:
            self._connected_cache[host_name] = connected
        self._dispatch("runspec:hosts_updated", {})
        threading.Thread(target=self._refresh_cycle, daemon=True).start()
        return {
            "ok": True,
            "connected": connected,
            "message": (
                f"Public key copied — {host_name} is now connected."
                if connected
                else "Public key copied — verifying connection…"
            ),
        }

    def rotate_ssh_key(self) -> dict[str, Any]:
        """Safely rotate the SSH key: generate new, push to all remote hosts using the
        old key, verify the new key works, then swap. Active sessions stay alive
        throughout because the old key is not replaced until all hosts are verified."""
        if getattr(self, "_rotating", False):
            return {
                "ok": False,
                "committed": False,
                "public_key": "",
                "key_path": "",
                "per_host": [],
                "message": "Rotation already in progress",
            }
        self._rotating = True
        try:
            return self._do_rotate_ssh_key()
        finally:
            self._rotating = False

    def _do_rotate_ssh_key(self) -> dict[str, Any]:
        import os
        from .executor import ssh_run

        key_dir = Path(os.environ.get("APPDATA", "~")).expanduser() / "runspec-console"
        canonical = key_dir / "runspec_ed25519"
        new_path = key_dir / "runspec_ed25519.new"

        # Step 1 — generate to side path (old key untouched)
        result = self._generate_keypair_to_path(new_path)
        if not result["ok"]:
            return {
                "ok": False,
                "committed": False,
                "public_key": "",
                "key_path": "",
                "per_host": [],
                "message": result["message"],
            }

        public_key = result["public_key"]
        cfg = self.get_config()
        global_ssh = dict(cfg.get("ssh", {}))
        global_idf = global_ssh.get("identityFile", "")

        # Step 2 — collect push targets
        self._reload_hosts()
        remote_hosts = [h for h in self._hosts if h.get("ssh")]

        # Snapshot connection status once (same source as the green dot in the UI)
        # so the slow SSH loop below doesn't hold the lock. Every entry here has an
        # `ssh` target, so a never-probed remote defaults to disconnected (False).
        with self._lock:
            connected_snapshot = dict(self._connected_cache)

        if not remote_hosts:
            self._commit_key_rotation(canonical, new_path, global_ssh, cfg)
            return {
                "ok": True,
                "committed": True,
                "per_host": [],
                "public_key": public_key,
                "key_path": _normalize_path(str(canonical)),
                "message": "Key rotated — no remote hosts configured. Add the public key to each host's authorized_keys before reconnecting.",
            }

        if global_idf and not Path(global_idf).expanduser().exists():
            if new_path.exists():
                new_path.unlink()
            return {
                "ok": False,
                "committed": False,
                "public_key": "",
                "key_path": "",
                "per_host": [],
                "message": f"Current key file not found: {global_idf}. Restore the file or generate a fresh key.",
            }

        # Steps 3 & 4 — push then verify each host
        per_host: list[dict[str, Any]] = []
        for host in remote_hosts:
            name = host.get("name", "?")
            ssh_target = host.get("ssh", "")
            host_idf = host.get("identityFile") or global_idf

            # Skip disconnected hosts — pushing would just fail and block the whole
            # rotation. They keep only the old key and must be updated manually once
            # reconnected (surfaced in per_host + the manual snippet in the UI).
            if not connected_snapshot.get(name, False):
                per_host.append(
                    {
                        "host": name,
                        "pushed": False,
                        "verified": False,
                        "skipped": True,
                        "error": "disconnected — new key not pushed; update manually",
                    }
                )
                continue

            # Skip hosts with a different per-host key — their session uses a different key
            if host.get("identityFile") and host.get("identityFile") != global_idf:
                per_host.append(
                    {
                        "host": name,
                        "pushed": False,
                        "verified": False,
                        "skipped": True,
                        "error": "per-host key override — rotate manually",
                    }
                )
                continue

            # Push new public key using old key
            push_cmd = (
                f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
                f"touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
                f"grep -qF '{public_key}' ~/.ssh/authorized_keys || "
                f"printf '%s\\n' '{public_key}' >> ~/.ssh/authorized_keys"
            )
            exit_code, _out, stderr = ssh_run(
                ssh_target,
                push_cmd,
                identity_file=host_idf,
                global_ssh_config=global_ssh,
            )
            if exit_code != 0:
                per_host.append(
                    {
                        "host": name,
                        "pushed": False,
                        "verified": False,
                        "skipped": False,
                        "error": stderr.strip() or f"push failed (exit {exit_code})",
                    }
                )
                continue

            # Verify new key works
            v_code, v_out, v_err = ssh_run(
                ssh_target,
                "echo ok",
                identity_file=str(new_path),
                global_ssh_config=global_ssh,
            )
            verified = v_code == 0 and "ok" in v_out
            per_host.append(
                {
                    "host": name,
                    "pushed": True,
                    "verified": verified,
                    "skipped": False,
                    "error": ""
                    if verified
                    else (
                        v_err.strip()
                        or v_out.strip()
                        or f"verify failed (exit {v_code})"
                    ),
                }
            )

        # Step 5 — commit gate: all non-skipped hosts must be verified, AND at least
        # one host must have actually been verified. The second clause prevents
        # silently committing a key that no reachable host authorizes (every remote
        # skipped because disconnected) — that would lock us out everywhere.
        any_verified = any(h["verified"] for h in per_host)
        can_commit = (
            all(h["verified"] or h["skipped"] for h in per_host) and any_verified
        )

        if not any_verified:
            return {
                "ok": True,
                "committed": False,
                "public_key": public_key,
                "key_path": "",
                "per_host": per_host,
                "message": (
                    "No connected host could be verified — old key kept. "
                    "Connect at least one host and retry, or use Generate new key "
                    "to swap manually."
                ),
            }

        if not can_commit:
            failed = [
                h["host"] for h in per_host if not h["verified"] and not h["skipped"]
            ]
            return {
                "ok": True,
                "committed": False,
                "public_key": public_key,
                "key_path": "",
                "per_host": per_host,
                "message": f"Push or verification failed on: {', '.join(failed)}. Old key still active.",
            }

        # Step 6 — commit
        self._commit_key_rotation(canonical, new_path, global_ssh, cfg)
        verified_n = sum(1 for h in per_host if h["verified"])
        skipped_n = sum(1 for h in per_host if h["skipped"])
        message = f"Key rotated — verified on {verified_n} host(s)."
        if skipped_n:
            message += (
                f" {skipped_n} host(s) skipped (disconnected or per-host key) "
                "— update them manually."
            )
        return {
            "ok": True,
            "committed": True,
            "public_key": public_key,
            "key_path": _normalize_path(str(canonical)),
            "per_host": per_host,
            "message": message,
        }

    def _commit_key_rotation(
        self, canonical: Path, new_path: Path, global_ssh: dict, cfg: dict
    ) -> None:
        import time
        from datetime import datetime, timezone

        ts = int(time.time())
        if canonical.exists():
            canonical.rename(canonical.parent / f"runspec_ed25519.bak.{ts}")
        new_path.rename(canonical)

        # Rotate the PPK alongside the OpenSSH key
        canonical_ppk = Path(str(canonical) + ".ppk")
        new_ppk = Path(str(new_path) + ".ppk")
        if canonical_ppk.exists():
            canonical_ppk.rename(canonical.parent / f"runspec_ed25519.ppk.bak.{ts}")
        if new_ppk.exists():
            new_ppk.rename(canonical_ppk)

        # Rotate the .pub alongside too
        canonical_pub = Path(str(canonical) + ".pub")
        new_pub = Path(str(new_path) + ".pub")
        if canonical_pub.exists():
            canonical_pub.rename(canonical.parent / f"runspec_ed25519.pub.bak.{ts}")
        if new_pub.exists():
            new_pub.rename(canonical_pub)

        ssh_section = dict(global_ssh)
        ssh_section["identityFile"] = _normalize_path(str(canonical))
        ssh_section["key_created_at"] = datetime.now(timezone.utc).isoformat()
        self.save_config({**cfg, "ssh": ssh_section})

    # ── PuTTY integration ─────────────────────────────────────────────────────

    def puttygen_path(self) -> str:
        """Locate puttygen.exe in common PuTTY install locations or on PATH."""
        found = _find_putty_exe("puttygen.exe")
        return str(found) if found else ""

    def launch_puttygen(self) -> None:
        """Launch puttygen.exe in the background (fire and forget)."""
        import subprocess as _sp

        path = self.puttygen_path()
        if not path:
            return
        try:
            _sp.Popen([path], close_fds=True)
        except Exception:
            pass

    def open_putty_url(self, url: str) -> None:
        """Open a URL in the user's default browser."""
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def browse_ssh_binary(self) -> str:
        """Open a native file picker for an SSH binary; returns the chosen path or ''.

        The path is normalised to forward slashes before returning so the UI
        input field and config.toml see a consistent representation.
        """
        import webview

        windows = getattr(webview, "windows", None) or []
        if not windows:
            return ""
        try:
            result = windows[0].create_file_dialog(
                webview.FileDialog.OPEN,
                file_types=("Executables (*.exe)",),
            )
        except Exception:
            return ""
        if not result:
            return ""
        if isinstance(result, (list, tuple)):
            chosen = str(result[0]) if result else ""
        else:
            chosen = str(result)
        return _normalize_path(chosen)

    # ── hosts file management ─────────────────────────────────────────────────

    def test_host(self, name: str) -> dict[str, Any]:
        entry = self._host_entry(name)
        if entry is None:
            return {
                "connected": False,
                "runspec_ok": False,
                "runnable_count": 0,
                "stdout": "",
                "stderr": f"Host '{name}' not found in hosts file",
                "exit_code": -1,
            }

        ssh = entry.get("ssh")
        paths = _paths(entry)
        rp = paths[0] if paths else ""
        idf = entry.get("identityFile")

        if ssh:
            code, out, err = self._ssh_run(
                ssh, f"{rp} local --format json", identity_file=idf
            )
            connected = code != -1  # -1 means connection itself failed
            ok = code == 0
            count = 0
            if ok:
                try:
                    import json as _json

                    count = len(_json.loads(out))
                except Exception:
                    ok = False
            return {
                "connected": connected,
                "runspec_ok": ok,
                "runnable_count": count,
                "stdout": out[:2000],
                "stderr": err[:2000],
                "exit_code": code,
            }
        else:
            import subprocess as _sp

            try:
                r = _sp.run(
                    [rp, "local", "--format", "json"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    encoding="utf-8",
                    errors="replace",
                )
            except Exception as exc:
                return {
                    "connected": False,
                    "runspec_ok": False,
                    "runnable_count": 0,
                    "stdout": "",
                    "stderr": str(exc),
                    "exit_code": -1,
                }
            ok = r.returncode == 0
            count = 0
            if ok:
                try:
                    import json as _json

                    count = len(_json.loads(r.stdout))
                except Exception:
                    ok = False
            return {
                "connected": True,
                "runspec_ok": ok,
                "runnable_count": count,
                "stdout": r.stdout[:2000],
                "stderr": r.stderr[:2000],
                "exit_code": r.returncode,
            }

    def get_jump_hosts(self) -> list[dict[str, Any]]:
        self._reload_hosts()
        result: list[dict[str, Any]] = []
        for h in self._hosts:
            ssh = h.get("ssh", "")
            user, hostname, port = _parse_ssh(ssh)
            entry: dict[str, Any] = {
                "name": h["name"],
                "hostname": hostname,
                "runspec_paths": _paths(h),
            }
            if user:
                entry["user"] = user
            if port is not None:
                entry["port"] = port
            if h.get("identityFile"):
                entry["identityFile"] = h["identityFile"]
            if h.get("group"):
                entry["group"] = h["group"]
            if h.get("room"):
                entry["room"] = h["room"]
            if isinstance(h.get("room_port"), int) and not isinstance(
                h.get("room_port"), bool
            ):
                entry["room_port"] = h["room_port"]
            if h.get("room_user"):
                entry["room_user"] = h["room_user"]
            if str(h.get("room_via") or "").strip().lower() == "exec":
                entry["room_via"] = "exec"
            if h.get("room_exec_command"):
                entry["room_exec_command"] = h["room_exec_command"]
            if h.get("room_queue"):
                entry["room_queue"] = h["room_queue"]
            if isinstance(h.get("room_catalog"), list):
                names = [str(n).strip() for n in h["room_catalog"] if str(n).strip()]
                if names:
                    entry["room_catalog"] = names
            result.append(entry)
        return result

    # Host-entry keys the Hosts editor owns (and rebuilds on save). Everything
    # else in an entry — e.g. room_dest_host, role, or a hand-added key — is
    # preserved verbatim so a save through the UI never silently drops it.
    _MANAGED_HOST_KEYS = frozenset(
        {
            "name",
            "ssh",
            "hostname",
            "user",
            "port",
            "runspec_paths",
            "runspec_path",
            "identityFile",
            "group",
            "room",
            "room_port",
            "room_user",
            "room_via",
            "room_exec_command",
            "room_queue",
            "room_catalog",
        }
    )

    def save_jump_hosts(self, hosts: list[dict[str, Any]]) -> None:
        # Freshest on-disk entries, to carry over any keys the editor doesn't manage.
        existing = {str(e.get("name") or ""): e for e in load_hosts(hosts_path())}
        entries: list[dict[str, Any]] = []
        for h in hosts:
            hostname = h.get("hostname", "")
            user = h.get("user", "")
            port = h.get("port")
            ssh = f"{user}@{hostname}" if user else hostname
            if port:
                ssh = f"{ssh}:{port}"
            raw_paths = h.get("runspec_paths") or []
            if not raw_paths and h.get("runspec_path"):
                raw_paths = [h["runspec_path"]]
            # Normalise filesystem paths to forward slashes for TOML storage.
            # Remote runspec paths are POSIX so this is a no-op for them;
            # identityFile on Windows benefits from the conversion.
            normalised_paths = [_normalize_path(p) for p in raw_paths]
            entry: dict[str, Any] = {
                "name": h["name"],
                "ssh": ssh,
                "runspec_paths": normalised_paths or ["runspec"],
            }
            if h.get("identityFile"):
                entry["identityFile"] = _normalize_path(h["identityFile"])
            if h.get("group"):
                entry["group"] = h["group"]
            # Chat room hosted on this fleet host: a group-chat ``room`` and/or a
            # helpdesk ``room_queue``, plus the server's localhost port on it. Both
            # ride this host's ssh target + key (see _room_specs / host_rooms); the
            # transport keys (user/via/exec command) are shared between them.
            room = str(h.get("room") or "").strip()
            queue = str(h.get("room_queue") or "").strip()
            room_port = h.get("room_port")
            has_port = isinstance(room_port, int) and not isinstance(room_port, bool)
            if (room or queue) and has_port:
                entry["room_port"] = room_port
                if room:
                    entry["room"] = room
                if queue:
                    entry["room_queue"] = queue
                    # Optional per-queue catalog allowlist (bare runnable names).
                    catalog = h.get("room_catalog")
                    if isinstance(catalog, list):
                        names = [str(n).strip() for n in catalog if str(n).strip()]
                        if names:
                            entry["room_catalog"] = names
                if str(h.get("room_user") or "").strip():
                    entry["room_user"] = h["room_user"].strip()
                # Exec-channel transport (for networks that block port-forwarding).
                if str(h.get("room_via") or "").strip().lower() == "exec":
                    entry["room_via"] = "exec"
                if str(h.get("room_exec_command") or "").strip():
                    entry["room_exec_command"] = h["room_exec_command"].strip()
            # Carry over any unmanaged keys from the existing entry (room_dest_host,
            # role, …) so they survive an editor save.
            for k, v in existing.get(str(h.get("name") or ""), {}).items():
                if k not in self._MANAGED_HOST_KEYS and k not in entry:
                    entry[k] = v
            entries.append(entry)
        save_hosts(hosts_path(), entries)
        self._reload_hosts()
        self.restart_chat()  # re-join rooms with the updated host config
        threading.Thread(target=self._refresh_cycle, daemon=True).start()

    def get_filters(self) -> list[dict[str, Any]]:
        """Named source-side stream filters, for the Settings tab and pickers."""
        self._reload_filters()
        result: list[dict[str, Any]] = []
        for f in self._filters:
            entry: dict[str, Any] = {
                "name": f.get("name", ""),
                "pipeline": f.get("pipeline", ""),
            }
            if f.get("description"):
                entry["description"] = f["description"]
            if f.get("param_label"):
                entry["param_label"] = f["param_label"]
            result.append(entry)
        return result

    def save_filters(self, filters: list[dict[str, Any]]) -> None:
        from .filters_store import save_filters as _save_filters

        entries: list[dict[str, Any]] = []
        for f in filters:
            name = str(f.get("name", "")).strip()
            pipeline = str(f.get("pipeline", "")).strip()
            if not name or not pipeline:
                continue  # both are required; drop incomplete rows
            entry: dict[str, Any] = {"name": name, "pipeline": pipeline}
            if f.get("description"):
                entry["description"] = str(f["description"]).strip()
            if f.get("param_label"):
                entry["param_label"] = str(f["param_label"]).strip()
            entries.append(entry)
        _save_filters(filters_path(), entries)
        self._reload_filters()

    def import_filters(self, toml_content: str) -> list[dict[str, Any]]:
        """Merge filters from an exported ``runspec_filters.toml`` blob, skipping
        names that already exist. Returns the newly-added filters."""
        from .filters_store import save_filters as _save_filters

        try:
            data = tomllib.loads(toml_content)
        except Exception:
            return []
        imported = data.get("filter", [])
        existing = {f.get("name") for f in self._filters}
        new = [
            f
            for f in imported
            if f.get("name") and f.get("pipeline") and f["name"] not in existing
        ]
        if new:
            _save_filters(filters_path(), [*self._filters, *new])
            self._reload_filters()
        return new

    def import_jump_hosts(self, toml_content: str) -> list[dict[str, Any]]:
        try:
            data = tomllib.loads(toml_content)
        except Exception:
            return []
        imported = data.get("host", [])
        existing = {h["name"] for h in self._hosts}
        new = [h for h in imported if h.get("name") and h["name"] not in existing]
        self._hosts.extend(new)
        save_hosts(hosts_path(), self._hosts)
        threading.Thread(target=self._refresh_cycle, daemon=True).start()
        return new

    # ── invocation ────────────────────────────────────────────────────────────

    def _become_params(
        self, cached: dict[str, Any] | None, hostname: str
    ) -> tuple[str, str, str | None]:
        """Resolve run_as + become method/flags for a runnable on a host.

        Reads the runnable's raw spec (carried through discovery as ``rawSpec``)
        and resolves ``run_as`` for this host. Returns ("", ...) when no
        privilege escalation is configured."""
        from runspec.become import resolve_run_as

        raw = (cached or {}).get("rawSpec") or {}
        run_as = resolve_run_as(raw.get("run_as"), hostname)
        return run_as, str(raw.get("become_method") or "sudo"), raw.get("become_flags")

    @staticmethod
    def _positionals_for(
        cached: dict[str, Any] | None, command_path: list[str]
    ) -> dict[str, int]:
        """{arg_name: position} for the node at ``command_path``.

        Positional args (those with a ``position`` in the spec) must be sent as
        bare values, not ``--name`` flags — the executor uses this to place them
        correctly. Resolves the leaf node so a subcommand's positionals are found
        too. Empty when the runnable isn't cached (falls back to all-flags)."""
        node = cached or {}
        for seg in command_path:
            node = (node.get("commands") or {}).get(seg) or {}
        return {
            a["name"]: a["position"]
            for a in node.get("args", [])
            if isinstance(a, dict) and a.get("position") is not None
        }

    @staticmethod
    def _secret_arg_names(
        cached: dict[str, Any] | None, command_path: list[str]
    ) -> set[str]:
        """Names of ``password``-typed args for the node at ``command_path``.

        Secrets must never become CLI flags — the bridge pulls them out of the
        argv args and hands them to the executor as env vars instead. Resolves
        the leaf node so a subcommand's secret args are found too."""
        node = cached or {}
        for seg in command_path:
            node = (node.get("commands") or {}).get(seg) or {}
        return {
            a["name"]
            for a in node.get("args", [])
            if isinstance(a, dict) and a.get("type") == "password"
        }

    def invoke_runnable(
        self,
        host: str,
        runnable: str,
        args: dict[str, Any],
        command_path: list[str] | None = None,
        group: str | None = None,
    ) -> str:
        inv_id = uuid.uuid4().hex[:12]
        cp = command_path or []
        entry = self._host_entry(host)
        cancel_event = threading.Event()

        entry_paths = _paths(entry) if entry else []
        # Look up the runnable's discovery record — needed for its venv (group)
        # and its run_as / become_method (privilege escalation).
        with self._lock:
            cached = next(
                (
                    r
                    for r in self._runnables_cache
                    if r.get("host") == host and r.get("name") == runnable
                ),
                None,
            )
        if group is None and cached:
            group = cached.get("group")
        rp = next(
            (p for p in entry_paths if venv_name(p) == group),
            entry_paths[0] if entry_paths else "",
        )

        run_as, become_method, become_flags = self._become_params(cached, host)

        # Split secret (password-typed) values out of the argv args: they are
        # injected as RUNSPEC_*_ARG_* env vars by the executor, never passed on
        # the command line. The stored/surfaced args record keeps only the
        # non-secret values so a secret never reaches the UI, history, or logs.
        secret_names = self._secret_arg_names(cached, cp)
        secrets = {
            k: v for k, v in args.items() if k in secret_names and v not in (None, "")
        }
        exec_args = {k: v for k, v in args.items() if k not in secret_names}

        with self._lock:
            self._in_flight[inv_id] = {
                "id": inv_id,
                "runnable": runnable,
                "group": venv_name(rp) if rp else "",
                "host": host,
                "operator": self._current_user(),
                "runAs": run_as,
                "startedAt": _iso_now(),
                "args": exec_args,
                _CANCEL_KEY: cancel_event,
            }

        def run() -> None:
            def on_line(line: str, stream: str) -> None:
                self._dispatch(
                    "runspec:output", {"id": inv_id, "line": line, "stream": stream}
                )

            def on_done(exit_code: int, duration_ms: int) -> None:
                with self._lock:
                    self._in_flight.pop(inv_id, None)
                self._dispatch(
                    "runspec:run_end",
                    {"id": inv_id, "exit_code": exit_code, "duration_ms": duration_ms},
                )

            if entry is None or not rp:
                on_line(f"✗  Host '{host}' not found in runspec_hosts.toml", "stderr")
                on_done(-1, 0)
                return

            ssh = entry.get("ssh")
            if ssh:
                # Stream over a pooled transport when pooling is on — the
                # invocation reuses the host's warm connection (one more channel,
                # no new handshake) instead of opening a fresh client.
                pool = self._ssh_pool
                session_factory = None
                if pool is not None:
                    cfg = self.get_config().get("ssh")
                    idf = entry.get("identityFile")

                    def session_factory() -> tuple[Any, Any]:
                        return pool.open_session(
                            ssh, identity_file=idf, global_ssh_config=cfg
                        )

                run_remote(
                    ssh,
                    rp,
                    runnable,
                    exec_args,
                    cp,
                    on_line,
                    on_done,
                    entry.get("identityFile"),
                    cancel_event=cancel_event,
                    global_ssh_config=self.get_config().get("ssh"),
                    run_as=run_as,
                    become_method=become_method,
                    become_flags=become_flags,
                    session_factory=session_factory,
                    positionals=self._positionals_for(cached, cp),
                    secrets=secrets,
                )
            else:
                run_local(
                    rp,
                    runnable,
                    exec_args,
                    cp,
                    on_line,
                    on_done,
                    cancel_event=cancel_event,
                    run_as=run_as,
                    become_method=become_method,
                    become_flags=become_flags,
                    positionals=self._positionals_for(cached, cp),
                    secrets=secrets,
                )

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return inv_id

    def get_in_flight(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {k: v for k, v in inv.items() if k != _CANCEL_KEY}
                for inv in self._in_flight.values()
            ]

    def cancel_invocation(self, inv_id: str) -> None:
        """Signal a running invocation to stop. Best-effort: kill the subprocess."""
        with self._lock:
            inv = self._in_flight.get(inv_id)
        if inv is None:
            return
        ev: threading.Event | None = inv.get(_CANCEL_KEY)
        if ev is not None:
            ev.set()

    # ── chat ──────────────────────────────────────────────────────────────────

    def send_chat(self, message: str, invocation_id: str | None = None) -> str:
        chat_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._chat_cancels[chat_id] = threading.Event()

        def run() -> None:
            try:
                try:
                    adapter = self._get_adapter()
                except Exception as exc:
                    self._dispatch(
                        "runspec:token", {"id": chat_id, "token": f"⚠ {exc}"}
                    )
                    self._dispatch(
                        "runspec:run_end",
                        {"id": chat_id, "exit_code": 1, "duration_ms": 0},
                    )
                    return
                if adapter is None:
                    self._dispatch(
                        "runspec:token",
                        {
                            "id": chat_id,
                            "token": "⚠ No LLM provider configured. Set provider and API key in Settings.",
                        },
                    )
                    self._dispatch(
                        "runspec:run_end",
                        {"id": chat_id, "exit_code": 1, "duration_ms": 0},
                    )
                    return
                asyncio.run(self._agentic_chat_turn(chat_id, message, adapter))
            finally:
                with self._lock:
                    self._chat_cancels.pop(chat_id, None)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return chat_id

    def cancel_chat(self, chat_id: str) -> None:
        """Signal an in-flight chat turn to stop. Best-effort: it halts at the
        next safe checkpoint — no further model calls and no further tool runs.
        A tool already executing (e.g. a remote command) is left to finish."""
        with self._lock:
            event = self._chat_cancels.get(chat_id)
        if event is not None:
            logger.info("chat %s: cancel requested", chat_id)
            event.set()
        # Wake any confirmation prompt waiting on this turn — otherwise Stop does
        # nothing while the Approve/Deny dialog is up (the gate is parked in
        # event.wait, which the cancel flag alone never interrupts). Mark them
        # cancelled so the gate refuses the tool rather than running it.
        with self._lock:
            pending = [
                (confirm_event, holder)
                for cid, confirm_event, holder in self._pending_confirms.values()
                if cid == chat_id
            ]
        for confirm_event, holder in pending:
            holder["cancelled"] = True
            confirm_event.set()

    def clear_chat(self) -> None:
        """Start a fresh conversation — drop the model's memory of prior turns.

        Blocks on the conversation lock so it can't run mid-turn (a turn in
        flight finishes against its history, then the next turn starts empty)."""
        with self._chat_lock:
            self._chat_history = []

    def _trim_chat_history(self, max_messages: int = 60) -> None:
        """Bound the rolling conversation so a long-lived session doesn't grow
        context without limit. Trims oldest messages, then drops any leading
        assistant / tool-result turn so the kept history still starts on a plain
        user message — the API requires that, and it keeps tool_use/tool_result
        pairs intact. Caller must hold self._chat_lock."""
        hist = self._chat_history
        if len(hist) <= max_messages:
            return
        del hist[: len(hist) - max_messages]
        while hist and not (
            hist[0].get("role") == "user" and isinstance(hist[0].get("content"), str)
        ):
            hist.pop(0)

    async def _agentic_chat_turn(
        self, chat_id: str, message: str, adapter: Any
    ) -> None:
        start = time.monotonic()
        tools = self._runnables_to_tools()
        usage_total = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        cancel = self._chat_cancels.get(chat_id) or threading.Event()
        stopped = False

        # Hold the conversation lock for the whole turn: it both serialises
        # concurrent sends and is the single owner of self._chat_history.
        with self._chat_lock:
            history = self._chat_history
            history.append({"role": "user", "content": message})

            for _ in range(10):  # max agentic iterations
                if cancel.is_set():
                    stopped = True
                    break
                response: Any = None
                assistant_text = ""  # this iteration's streamed assistant text
                try:
                    async for event in adapter.stream_with_tools(history, tools):
                        if cancel.is_set():
                            stopped = True
                            break  # abort streaming (closes the provider stream)
                        if event[0] == "text":
                            assistant_text += event[1]
                            self._dispatch(
                                "runspec:token", {"id": chat_id, "token": event[1]}
                            )
                        elif event[0] == "done":
                            response = event[1]
                except Exception as exc:
                    self._dispatch(
                        "runspec:token", {"id": chat_id, "token": f"\n⚠ Error: {exc}"}
                    )
                    break

                if response is not None:
                    for key, value in self._usage_from_response(response).items():
                        usage_total[key] += value

                # Persisting any clean assistant text is always safe (text-only,
                # no tool pairing); do it whenever we're about to leave the loop.
                if stopped:
                    if assistant_text.strip():
                        history.append({"role": "assistant", "content": assistant_text})
                    break

                if (
                    response is None
                    or response.stop_reason != "tool_use"
                    or not response.tool_calls
                ):
                    # Final turn — persist the assistant's reply so the next user
                    # message sees it (tool-calling turns are persisted below via
                    # make_tool_turn, which already carries the assistant turn).
                    if response is not None and assistant_text.strip():
                        history.append({"role": "assistant", "content": assistant_text})
                    break

                # About to act on hosts — honour a stop requested while the model
                # was producing the tool calls, before running any of them.
                if cancel.is_set():
                    stopped = True
                    if assistant_text.strip():
                        history.append({"role": "assistant", "content": assistant_text})
                    break

                # Execute each tool call and dispatch events
                tool_results: list[tuple[Any, str]] = []
                for tc in response.tool_calls:
                    self._dispatch(
                        "runspec:tool_start",
                        {
                            "id": chat_id,
                            "tool_name": tc.name,
                            "tool_input": tc.input,
                        },
                    )
                    if cancel.is_set():
                        # Stopped mid-batch — refuse the rest without prompting
                        # or running, but still record a paired tool_result.
                        output = f"⏹ '{tc.name.split('__', 1)[-1]}' was not run — you stopped the assistant."
                    else:
                        output = await self._gated_run_tool_async(
                            tc.name, tc.input, chat_id
                        )
                    self._dispatch(
                        "runspec:tool_end",
                        {
                            "id": chat_id,
                            "tool_name": tc.name,
                            "output": output[:2000],
                        },
                    )
                    tool_results.append((tc, output))

                history.extend(adapter.make_tool_turn(response, tool_results))

            self._trim_chat_history()

        duration_ms = int((time.monotonic() - start) * 1000)
        if stopped:
            self._dispatch("runspec:token", {"id": chat_id, "token": "\n⏹ Stopped."})
        self._dispatch(
            "runspec:run_end",
            {"id": chat_id, "exit_code": 0, "duration_ms": duration_ms},
        )
        if any(usage_total.values()):
            self._dispatch(
                "runspec:chat_usage",
                {
                    "id": chat_id,
                    # input_tokens is the uncached remainder (full price); the cached
                    # portion is reported separately so the UI can show cache savings.
                    "input_tokens": usage_total["input"],
                    "output_tokens": usage_total["output"],
                    "cache_read_tokens": usage_total["cache_read"],
                    "cache_creation_tokens": usage_total["cache_creation"],
                },
            )

    @staticmethod
    def _usage_from_response(response: Any) -> dict[str, int]:
        """Pull token counts off a provider response. Anthropic/Bedrock split the
        prompt into uncached `input_tokens` plus `cache_read_input_tokens` (served
        at ~0.1x) and `cache_creation_input_tokens` (the first-turn write); OpenAI
        reports only prompt/completion totals (cache fields resolve to 0)."""
        zero = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        usage = getattr(getattr(response, "_raw", None), "usage", None)
        if usage is None:
            return zero

        def field(*names: str) -> int:
            for name in names:
                value = getattr(usage, name, None)
                if value:
                    return int(value)
            return 0

        return {
            "input": field("input_tokens", "prompt_tokens"),
            "output": field("output_tokens", "completion_tokens"),
            "cache_read": field("cache_read_input_tokens"),
            "cache_creation": field("cache_creation_input_tokens"),
        }

    def _runnables_to_tools(
        self, allow: Collection[str] | None = None
    ) -> list[dict[str, Any]]:
        """Convert cached runnables to Anthropic-format tool schemas.

        ``allow`` (when non-empty) scopes the result to an allowlist of runnable
        names — each entry matches a runnable's bare ``name`` or its qualified
        ``host__name`` (raw or the sanitised tool name) — so an agent-mode trigger
        can be limited to a curated set of tools. ``None``/empty = every runnable.
        """
        import re

        allowset = {str(a).strip() for a in allow} if allow else None
        with self._lock:
            runnables = list(self._runnables_cache)
        tools: list[dict[str, Any]] = []
        seen_tool_names: set[str] = set()
        for r in runnables:
            host = r.get("host", "local")
            name = r.get("name", "")
            raw_name = f"{host}__{name}"
            tool_name = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_name)[:64]
            if allowset is not None and not (
                name in allowset or raw_name in allowset or tool_name in allowset
            ):
                continue  # scoped out by the trigger's runnables allowlist
            if tool_name in seen_tool_names:
                sys.stderr.write(
                    f"runspec-console: warning: duplicate tool name '{tool_name}' skipped"
                    f" ('{name}' defined in multiple installed packages)\n"
                )
                sys.stderr.flush()
                continue
            seen_tool_names.add(tool_name)
            description = r.get("description") or f"Run {name} on {host}"
            args = r.get("args") or []
            properties: dict[str, Any] = {}
            required: list[str] = []

            def add_arg(arg: dict[str, Any], note: str = "") -> None:
                """Add one spec arg as a JSON-schema property. ``note`` prefixes the
                description (used to tag a subcommand's own args) and marks the arg
                as conditional — never added to the tool's top-level ``required``."""
                arg_name = arg.get("name", "")
                if not arg_name or arg_name in properties:
                    return
                arg_type = arg.get("type", "str")
                json_type = {
                    "int": "integer",
                    "float": "number",
                    "flag": "boolean",
                    "bool": "boolean",
                    "path": "string",
                }.get(arg_type, "string")
                prop: dict[str, Any] = {"type": json_type}
                # Build description: optional note + user-facing text + default hint
                # so the LLM knows what value will be used when the arg is omitted.
                desc_parts: list[str] = []
                if note:
                    desc_parts.append(note)
                if arg.get("description") or arg.get("help"):
                    desc_parts.append(str(arg.get("description") or arg.get("help")))
                default = arg.get("default")
                if default is not None:
                    if isinstance(default, str) and default.startswith("$"):
                        desc_parts.append(f"(default from env var {default})")
                    else:
                        desc_parts.append(f"(default: {default})")
                if desc_parts:
                    prop["description"] = " ".join(desc_parts)
                if arg.get("options"):
                    prop["enum"] = arg["options"]
                properties[arg_name] = prop
                if arg.get("required") and not note:
                    required.append(arg_name)

            for arg in args:
                add_arg(arg)

            # Surface subcommands so the agent can pick one — and knows when it
            # MUST. Without this the agent only saw the global args and had no way
            # to name a command, so require-command runnables were unrunnable.
            commands = r.get("commands") or {}
            if commands:
                cmd_names = list(commands.keys())
                require_command = bool(r.get("requireCommand"))
                summary = "; ".join(
                    f"'{cn}'"
                    + (
                        f" — {commands[cn].get('description')}"
                        if commands[cn].get("description")
                        else ""
                    )
                    for cn in cmd_names
                )
                properties["command"] = {
                    "type": "string",
                    "enum": cmd_names,
                    "description": (
                        f"Subcommand to run ({summary}). "
                        + (
                            "Required: this runnable cannot run without choosing "
                            "one of these commands."
                            if require_command
                            else "Optional — omit to run the runnable at its top level."
                        )
                    ),
                }
                if require_command:
                    required.append("command")
                # Each command's own (non-inherited) args become optional inputs,
                # tagged with the command they belong to. Globals already added above.
                for cn in cmd_names:
                    for arg in commands[cn].get("args") or []:
                        if arg.get("inherited"):
                            continue
                        add_arg(arg, note=f"(for the '{cn}' command)")

            schema: dict[str, Any] = {"type": "object", "properties": properties}
            if required:
                schema["required"] = required
            tools.append(
                {"name": tool_name, "description": description, "input_schema": schema}
            )
        # Built-in file-transfer tools are additive — never let a problem building
        # them strip the runnable tools the agent actually needs.
        try:
            tools.extend(self._custom_agent_tools())
        except Exception:
            logger.debug("custom agent tools unavailable", exc_info=True)
        return tools

    def _custom_agent_tools(self) -> list[dict[str, Any]]:
        """Schemas for the built-in file-transfer tools — download a file from a
        remote host, or relay one host→host, each with an optional saved filter.
        Host and filter names are surfaced as enums (with the filter descriptions)
        so the model picks valid values."""
        remote = [h["name"] for h in self._hosts if h.get("ssh")]
        if not remote:
            return []  # nothing to transfer to/from
        filters = self._filters
        filter_names = [f["name"] for f in filters if f.get("name")]

        def filter_props() -> dict[str, Any]:
            if not filter_names:
                return {}
            lines = []
            for f in filters:
                line = f"'{f['name']}'"
                if "{{arg}}" in (f.get("pipeline") or ""):
                    line += f" (needs filter_value: {f.get('param_label') or 'value'})"
                if f.get("description"):
                    line += f" — {f['description']}"
                lines.append(line)
            return {
                "filter": {
                    "type": "string",
                    "enum": filter_names,
                    "description": (
                        "Optional saved filter applied on the source host before "
                        "transfer, so only matching output crosses the network "
                        "(ideal for a few lines from a big log). Available: "
                        + "; ".join(lines)
                        + "."
                    ),
                },
                "filter_value": {
                    "type": "string",
                    "description": "Value for a filter that needs one (its {{arg}} placeholder).",
                },
            }

        download = {
            "name": "download_file",
            "description": (
                "Download a file from a remote host to the local machine. Optionally "
                "filter it on the remote host first — only the matching output is "
                "transferred, so pulling a few relevant lines out of a multi-gigabyte "
                "log is cheap."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "enum": remote,
                        "description": "Remote host to download from.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Absolute path of the file on the remote host.",
                    },
                    "dest_dir": {
                        "type": "string",
                        "description": "Local destination directory.",
                    },
                    **filter_props(),
                },
                "required": ["host", "path", "dest_dir"],
            },
        }
        relay = {
            "name": "relay_file",
            "description": (
                "Relay a file from one remote host to another through the console, "
                "optionally filtering it on the source host first."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "source_host": {
                        "type": "string",
                        "enum": remote,
                        "description": "Host to read the file from.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Absolute path of the file on the source host.",
                    },
                    "dest_host": {
                        "type": "string",
                        "enum": remote,
                        "description": "Host to write the file to.",
                    },
                    "dest_dir": {
                        "type": "string",
                        "description": "Destination directory on dest_host.",
                    },
                    **filter_props(),
                },
                "required": ["source_host", "path", "dest_host", "dest_dir"],
            },
        }
        return [download, relay]

    def _run_custom_tool(self, name: str, ti: dict[str, Any]) -> str:
        """Execute a built-in file-transfer tool synchronously and report the
        outcome (the autonomy gate has already approved it). Kicks off the normal
        transfer — so the user sees live progress in the transfer panel — then
        blocks on the result via ``_await_transfer``."""
        filter_name = str(ti.get("filter") or "")
        filter_value = ti.get("filter_value")
        dest_dir = ti.get("dest_dir")
        if name == "download_file":
            host, path = ti.get("host"), ti.get("path")
            if not host or not path or not dest_dir:
                return "Error: host, path and dest_dir are required."
            res = self.start_transfer(
                host, [path], "local", dest_dir, filter_name, filter_value
            )
            label = f"download of {os.path.basename(path)} from {host}"
        elif name == "relay_file":
            sh, path, dh = ti.get("source_host"), ti.get("path"), ti.get("dest_host")
            if not sh or not path or not dh or not dest_dir:
                return "Error: source_host, path, dest_host and dest_dir are required."
            res = self.start_relay(
                {"kind": "host", "host": sh},
                [path],
                dh,
                dest_dir,
                filter_name,
                filter_value,
            )
            label = f"relay of {os.path.basename(path)} from {sh} to {dh}"
        else:
            return f"Error: unknown tool '{name}'"

        if res.get("error"):
            return f"✗ Could not start {label}: {res['error']}"
        tid = res.get("id")
        detail = self._await_transfer(tid) if tid else None
        if detail is None:
            return f"Started {label} (id {tid}); still running — check the transfer panel for the result."
        if detail.get("ok"):
            via = f" through filter '{filter_name}'" if filter_name else ""
            return (
                f"✓ Completed {label}{via}. Wrote {detail.get('files', 0)} file(s) "
                f"to {dest_dir}."
            )
        return f"✗ {label} failed: {detail.get('error') or 'unknown error'}"

    def _effective_tool_autonomy(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> str:
        """Autonomy the agent must honour for this call: the runnable's level,
        escalated by any provided arg's per-arg autonomy (most restrictive wins).
        Unknown runnable → 'confirm' (gated by default — never silently run)."""
        if tool_name in _CUSTOM_TOOL_NAMES:
            return "confirm"  # side-effecting file move — always prompt
        host, runnable = (
            tool_name.split("__", 1) if "__" in tool_name else (None, tool_name)
        )
        with self._lock:
            r = next(
                (
                    r
                    for r in self._runnables_cache
                    if r.get("host") == host and r.get("name") == runnable
                ),
                None,
            )
        if not r:
            return "confirm"
        level = r.get("autonomy") or "confirm"
        for arg in r.get("args") or []:
            if arg.get("name") in (tool_input or {}) and arg.get("autonomy"):
                if _AUTONOMY_RANK.get(arg["autonomy"], 0) > _AUTONOMY_RANK.get(
                    level, 0
                ):
                    level = arg["autonomy"]
        return level

    # Confirmation timeout — no answer from the UI within this window is a deny.
    _CONFIRM_TIMEOUT_S = 300

    def _confirm_decision(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Resolve the gate decision string: 'run' | 'manual' | <level needing prompt>."""
        level = self._effective_tool_autonomy(tool_name, tool_input)
        if level == "autonomous":
            return "run"
        if level == "manual":
            return "manual"
        return level  # confirm / supervised → needs a prompt

    def _register_confirm(
        self, chat_id: str, tool_name: str, tool_input: dict[str, Any], level: str
    ) -> tuple[str, threading.Event, dict[str, bool]]:
        """Register a pending confirmation and emit the UI prompt event.

        Returns (request_id, event, holder). The event is set by
        resolve_tool_confirmation() when the UI answers."""
        request_id = uuid.uuid4().hex
        event = threading.Event()
        holder = {"approved": False, "cancelled": False}
        with self._lock:
            self._pending_confirms[request_id] = (chat_id, event, holder)
            self._pending_confirm_meta[request_id] = {
                "id": chat_id,
                "request_id": request_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "autonomy": level,
                "created_at": time.time(),
            }
        logger.info(
            "autonomy gate: prompting for %s (autonomy=%s) request_id=%s",
            tool_name,
            level,
            request_id,
        )
        self._dispatch(
            "runspec:tool_confirm",
            {
                "id": chat_id,
                "request_id": request_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "autonomy": level,
            },
        )
        self._notify_confirm(tool_name, tool_input, level)
        return request_id, event, holder

    # ── desktop notification for confirmation events ──────────────────────────
    #
    # The in-app Approve/Deny card only helps if the window is in focus. When a
    # tool call parks on the autonomy gate we also raise an OS toast + flash the
    # taskbar so the user is pulled back to answer (it otherwise times out as a
    # deny after _CONFIRM_TIMEOUT_S). Sending a toast is independent of the
    # "read notifications" consent the listener source needs — see notify.py.

    # The native window title, matching app.py's create_window / FindWindow.
    _WINDOW_TITLE = "runspec console"

    def _confirm_notifications_enabled(self) -> bool:
        try:
            return confirm_notifications_enabled()
        except Exception:
            return True

    def _notify_confirm(
        self, tool_name: str, tool_input: dict[str, Any], level: str
    ) -> None:
        """Best-effort desktop toast + taskbar flash for a pending confirm.

        Gates (availability, opt-out) and message-building run synchronously; the
        actual toast/flash is offloaded so the event-loop thread that streams
        tokens isn't blocked. Any failure is swallowed — a missed notification
        must never disturb the confirmation flow."""
        try:
            ok, _ = notify.available()
            if not ok:
                return
            if not self._confirm_notifications_enabled():
                return
            title, body = notify.build_confirm_message(tool_name, tool_input)
        except Exception:
            logger.debug("notify_confirm setup failed", exc_info=True)
            return

        def _raise() -> None:
            try:
                notify.notify(title, body, on_click=self._focus_window)
                self._flash_window()
            except Exception:
                logger.debug("desktop notify failed", exc_info=True)

        threading.Thread(target=_raise, name="notify-confirm", daemon=True).start()

    def _flash_window(self) -> None:
        """Flash the taskbar button until the console is foregrounded. Needs no
        notification permission, so it's the fallback when the toast is
        suppressed. Windows-only, best-effort."""
        if sys.platform != "win32":
            return
        try:
            import win32gui

            hwnd = win32gui.FindWindow(None, self._WINDOW_TITLE)
            if not hwnd:
                return
            # FLASHW_ALL (0x3) | FLASHW_TIMERNOFG (0xC): flash until foreground.
            win32gui.FlashWindowEx(hwnd, 0x0000000F, 0, 0)
        except Exception:
            logger.debug("flash window failed", exc_info=True)

    def _focus_window(self) -> None:
        """Bring the console to the foreground (toast click handler).
        Windows-only, best-effort."""
        if sys.platform != "win32":
            return
        try:
            import win32con
            import win32gui

            hwnd = win32gui.FindWindow(None, self._WINDOW_TITLE)
            if not hwnd:
                return
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            logger.debug("focus window failed", exc_info=True)

    def _window_is_foreground(self) -> bool:
        """True if the console window is currently the foreground window. Lets a
        room toast stay quiet while the user is already in the app (the in-app
        toast covers that). Windows-only; False elsewhere / on error so a missed
        check errs toward notifying."""
        if sys.platform != "win32":
            return False
        try:
            import win32gui

            hwnd = win32gui.FindWindow(None, self._WINDOW_TITLE)
            return bool(hwnd) and win32gui.GetForegroundWindow() == hwnd
        except Exception:
            logger.debug("foreground check failed", exc_info=True)
            return False

    def _notify_room_message(self, payload: dict[str, Any]) -> None:
        """Best-effort desktop toast + taskbar flash for an inbound room message.

        Parity with ``_notify_confirm``: pull the user back to a chat-room /
        helpdesk message they can't see. Suppressed when the console is already
        foreground (the in-app toast handles that) or opted out via
        ``[console] room_notifications``. Every gate and the actual toast are
        swallowed on error — a missed notification must never disturb the chat
        flow. The server drops the console's own echo, so every event here is
        from someone else."""
        try:
            ok, _ = notify.available()
            if not ok:
                return
            if self._window_is_foreground():
                return
            try:
                if not room_notifications_enabled():
                    return
            except Exception:
                pass  # config unreadable → default to notifying
            title, body = notify.build_room_message(payload)
        except Exception:
            logger.debug("notify_room setup failed", exc_info=True)
            return

        def _raise() -> None:
            try:
                notify.notify(title, body, on_click=self._focus_window)
                self._flash_window()
            except Exception:
                logger.debug("desktop room notify failed", exc_info=True)

        threading.Thread(target=_raise, name="notify-room", daemon=True).start()

    def _finish_confirm(
        self, request_id: str, holder: dict[str, bool], timed_out: bool
    ) -> bool:
        with self._lock:
            self._pending_confirms.pop(request_id, None)
            self._pending_confirm_meta.pop(request_id, None)
        if timed_out:
            logger.warning(
                "autonomy gate: request_id=%s timed out after %ds — treating as deny",
                request_id,
                self._CONFIRM_TIMEOUT_S,
            )
        else:
            logger.info(
                "autonomy gate: request_id=%s answered approved=%s",
                request_id,
                holder["approved"],
            )
        return holder["approved"]

    async def _gated_run_tool_async(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        chat_id: str,
        stage: Callable[[str], None] | None = None,
    ) -> str:
        """Async autonomy gate used by the agentic loop.

        The UI prompt is dispatched from the event-loop thread (the same thread
        that streams tokens, which is known to reach the frontend) — only the
        blocking wait is offloaded to a worker thread. Dispatching the prompt
        from a worker thread was unreliable and produced silent timeouts.

        ``stage`` (trigger turns only) reports the card stage: "awaiting_human"
        while a confirm is pending, then "running" once it actually executes."""
        decision = self._confirm_decision(tool_name, tool_input)
        display = tool_name.split("__", 1)[-1]
        logger.info("autonomy gate: tool=%s decision=%s", tool_name, decision)
        if decision == "manual":
            return f"✗ '{display}' is manual-only (autonomy = manual) and cannot be run by the assistant. Ask the user to run it themselves from the Run button."
        if decision != "run":
            if stage is not None:
                stage("awaiting_human")
            request_id, event, holder = self._register_confirm(
                chat_id, tool_name, tool_input, decision
            )
            timed_out = not await asyncio.to_thread(event.wait, self._CONFIRM_TIMEOUT_S)
            approved = self._finish_confirm(request_id, holder, timed_out)
            # Stop pressed while the dialog was up: don't run, and let the loop
            # halt at its next checkpoint (cancel flag is already set).
            if holder.get("cancelled"):
                return f"⏹ '{display}' was not run — you stopped the assistant before approving it."
            if not approved:
                # The tool didn't run, but the turn continues — the agent reasons
                # about the denial (often an analysis). Leave 'awaiting_human' and
                # the card looks stuck; move to 'running' so the work is visible.
                if stage is not None:
                    stage("running")
                return (
                    f"✗ The user declined to run '{display}' (autonomy = {decision})."
                )
        if stage is not None:
            stage("running")
        return await asyncio.to_thread(self._run_tool_sync, tool_name, tool_input)

    def _gated_run_tool(
        self, tool_name: str, tool_input: dict[str, Any], chat_id: str
    ) -> str:
        """Synchronous autonomy gate (kept for direct/unit-test use).

        autonomous → run; confirm/supervised → ask the user (Approve/Deny);
        manual → refuse (human-only; the user must use the Run button)."""
        decision = self._confirm_decision(tool_name, tool_input)
        display = tool_name.split("__", 1)[-1]
        if decision == "manual":
            return f"✗ '{display}' is manual-only (autonomy = manual) and cannot be run by the assistant. Ask the user to run it themselves from the Run button."
        if decision != "run":
            if not self._await_confirmation(tool_name, tool_input, decision, chat_id):
                return (
                    f"✗ The user declined to run '{display}' (autonomy = {decision})."
                )
        return self._run_tool_sync(tool_name, tool_input)

    def _await_confirmation(
        self, tool_name: str, tool_input: dict[str, Any], level: str, chat_id: str
    ) -> bool:
        """Ask the UI to approve an agent tool call; block (in a worker thread)
        until resolve_tool_confirmation() fires. Times out as a deny."""
        request_id, event, holder = self._register_confirm(
            chat_id, tool_name, tool_input, level
        )
        timed_out = not event.wait(timeout=self._CONFIRM_TIMEOUT_S)
        return self._finish_confirm(request_id, holder, timed_out)

    def resolve_tool_confirmation(self, request_id: str, approved: bool) -> None:
        """Frontend callback: resolve a pending agent tool-call confirmation."""
        logger.info(
            "autonomy gate: UI resolved request_id=%s approved=%s", request_id, approved
        )
        with self._lock:
            entry = self._pending_confirms.get(request_id)
        if entry is not None:
            _chat_id, event, holder = entry
            holder["approved"] = bool(approved)
            event.set()
        else:
            logger.warning(
                "autonomy gate: resolve for unknown request_id=%s (already timed out?)",
                request_id,
            )

    def _run_tool_sync(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Blocking runnable execution — call via asyncio.to_thread from the agentic loop."""
        if tool_name in _CUSTOM_TOOL_NAMES:
            return self._run_custom_tool(tool_name, dict(tool_input or {}))
        if "__" not in tool_name:
            return f"Error: invalid tool name '{tool_name}'"
        host, runnable = tool_name.split("__", 1)
        entry = self._host_entry(host)
        if entry is None:
            return f"Error: host '{host}' not found"
        # `command` (when present) names the subcommand to descend into — pull it
        # out of the args and pass it as the command path. Core validates it and
        # enforces require-command, so an unknown/missing command surfaces a clear
        # "Available commands: …" error the agent can read and retry from.
        tool_input = dict(tool_input or {})
        chosen_command = tool_input.pop("command", None)
        command_path = (
            [chosen_command]
            if isinstance(chosen_command, str) and chosen_command
            else []
        )
        output_lines: list[str] = []
        exit_code_holder = [0]

        def on_line(line: str, stream: str) -> None:
            output_lines.append(line)

        def on_done(exit_code: int, duration_ms: int) -> None:
            exit_code_holder[0] = exit_code

        tool_paths = _paths(entry)
        with self._lock:
            cached_r = next(
                (
                    r
                    for r in self._runnables_cache
                    if r.get("host") == host and r.get("name") == runnable
                ),
                None,
            )
        run_group = cached_r.get("group") if cached_r else None
        rp = next(
            (p for p in tool_paths if venv_name(p) == run_group),
            tool_paths[0] if tool_paths else "",
        )
        run_as, become_method, become_flags = self._become_params(cached_r, host)
        ssh = entry.get("ssh")
        if ssh:
            run_remote(
                ssh,
                rp,
                runnable,
                tool_input,
                command_path,
                on_line,
                on_done,
                entry.get("identityFile"),
                timeout=120,
                agent=True,
                global_ssh_config=self.get_config().get("ssh"),
                run_as=run_as,
                become_method=become_method,
                become_flags=become_flags,
                positionals=self._positionals_for(cached_r, command_path),
            )
        else:
            run_local(
                rp,
                runnable,
                tool_input,
                command_path,
                on_line,
                on_done,
                timeout=120,
                agent=True,
                run_as=run_as,
                become_method=become_method,
                become_flags=become_flags,
                positionals=self._positionals_for(cached_r, command_path),
            )

        output = "\n".join(output_lines)
        if len(output) > 16384:
            output = output[:16384] + "\n[output truncated]"
        if exit_code_holder[0] != 0:
            return f"[exit {exit_code_holder[0]}]\n{output}"
        return output or "(no output)"

    # ── terminal sessions ─────────────────────────────────────────────────────

    def launch_terminal(self, host: str) -> None:
        """Launch PuTTY in a separate window connected to the host.

        Fire-and-forget: no stdout/stderr capture, no session tracking. PuTTY
        runs as its own top-level process and the user closes it from there.
        """
        import subprocess as _sp

        entry = self._host_entry(host)
        if entry is None:
            raise ValueError(f"Host '{host}' not found")
        ssh = entry.get("ssh")
        if not ssh:
            raise ValueError(
                "Cannot launch terminal to local host — use a remote jump host"
            )
        putty = _find_putty_exe("putty.exe")
        if putty is None:
            raise ValueError(
                r"putty.exe not found. Install PuTTY from putty.org — "
                r"expected at C:\Program Files\PuTTY\putty.exe."
            )
        # Parse ssh target for hostname/port; get username from host or global config.
        cfg_ssh = self.get_config().get("ssh", {})
        user, hostname, port = _parse_ssh(ssh)
        if not user:
            user = cfg_ssh.get("user", "")

        cmd = [str(putty)]
        # Route through the HTTP proxy by loading a saved session that carries
        # the proxy config (PuTTY has no proxy CLI flag). Host/user/key below
        # still override the session.
        proxy = cfg_ssh.get("proxy") or ""
        if proxy:
            session = _ensure_putty_proxy_session(proxy)
            if session:
                cmd.extend(["-load", session])
        cmd.append("-ssh")
        if user:
            cmd.extend(["-l", user])
        cmd.append(hostname)
        if port:
            cmd.extend(["-P", str(port)])

        # Prefer PPK v2 (works with all PuTTY versions) over raw OpenSSH key.
        idf = cfg_ssh.get("identityFile", "")
        if idf:
            expanded = Path(idf).expanduser()
            ppk = Path(str(expanded) + ".ppk")
            key_file = ppk if ppk.exists() else expanded
            if not key_file.exists():
                raise ValueError(
                    f"SSH key file not found: {idf}\n"
                    "Generate a new key in Settings → SSH."
                )
            cmd.extend(["-i", str(key_file)])
        _sp.Popen(cmd)

    def launch_local_terminal(self) -> None:
        """Open a local terminal window — tries Windows Terminal, then PowerShell, then cmd."""
        import subprocess as _sp

        for exe in ("wt.exe", "powershell.exe", "cmd.exe"):
            try:
                _sp.Popen([exe])
                return
            except FileNotFoundError:
                continue
        raise ValueError("Could not open a terminal window")

    # ── internals ─────────────────────────────────────────────────────────────

    def _reload_hosts(self) -> None:
        self._hosts = load_hosts(hosts_path())

    def _reload_filters(self) -> None:
        from .filters_store import load_filters

        self._filters = load_filters(filters_path())

    def _filter_entry(self, name: str) -> dict[str, Any] | None:
        """The saved filter named ``name`` (case-sensitive), or None."""
        return next((f for f in self._filters if f.get("name") == name), None)

    def _local_host_entry(self) -> dict[str, Any]:
        """Synthetic entry for the venv runspec-console itself runs in."""
        scripts = Path(sys.executable).parent
        # Windows venvs use Scripts\runspec.exe; Linux/Mac use bin/runspec
        for name in ("runspec.exe", "runspec"):
            candidate = scripts / name
            if candidate.exists():
                return {"name": "local", "runspec_paths": [str(candidate)], "ssh": None}
        # Fallback: construct the expected path even if not yet installed
        return {
            "name": "local",
            "runspec_paths": [str(scripts / "runspec.exe")],
            "ssh": None,
        }

    def _host_entry(self, host: str) -> dict[str, Any] | None:
        if host == "local":
            return self._local_host_entry()
        return next((h for h in self._hosts if h.get("name") == host), None)

    def _start_refresh_watcher(self) -> None:
        """Run a full refresh cycle, then repeat every ``[refresh] interval`` s."""
        from .ssh_pool import refresh_settings

        def _loop() -> None:
            while True:
                self._refresh_cycle()
                interval = refresh_settings(self.get_config().get("refresh"))[
                    "interval"
                ]
                time.sleep(interval)

        threading.Thread(target=_loop, daemon=True).start()

    def _refresh_cycle(self) -> None:
        """Probe connectivity, then discover runnables — reusing pooled connections.

        With the pool on, Phase 1 *warms* the per-host transport that Phase 2's
        discovery then reuses, so a host costs one handshake per cycle (often
        zero once warm) instead of the old ``probe + per-path`` handshake storm.
        Per-host jitter spreads the cycle's connects so they don't all fire on
        the same instant; the pool's global semaphore bounds how many connect at
        once; ``reap()`` closes transports gone idle.
        """
        from .ssh_pool import refresh_settings

        self._reload_hosts()
        local = self._local_host_entry()
        all_hosts = [local] + [h for h in self._hosts if h.get("name") != "local"]
        jitter = refresh_settings(self.get_config().get("refresh"))["jitter"]

        # ── Phase 1: connectivity (warms the pooled transport) ─────────────────
        with self._lock:
            self._connected_cache["local"] = True

        def probe(h: dict[str, Any]) -> None:
            if jitter:
                time.sleep(random.uniform(0, jitter))  # spread the connects
            result = self._check_connected(h)
            with self._lock:
                self._connected_cache[h["name"]] = result

        probe_threads = [
            threading.Thread(target=probe, args=(h,), daemon=True)
            for h in all_hosts
            if h.get("ssh")
        ]
        for t in probe_threads:
            t.start()
        for t in probe_threads:
            t.join()
        self._dispatch("runspec:hosts_updated", {})

        # ── Phase 2: runnables discovery (reuses the warm transport) ───────────
        discovered: list[dict[str, Any]] = []
        lock2 = threading.Lock()
        runner = self._ssh_pool.run if self._ssh_pool is not None else None

        def discover(h: dict[str, Any]) -> None:
            paths = _paths(h)
            ssh = h.get("ssh")
            idf = h.get("identityFile")
            name = h["name"]
            with self._lock:
                connected = self._connected_cache.get(name, ssh is None)
            if not connected:
                return
            for rp in paths:
                items = (
                    discover_remote(
                        ssh,
                        rp,
                        name,
                        idf,
                        global_ssh_config=self.get_config().get("ssh"),
                        runner=runner,
                    )
                    if ssh
                    else discover_local(rp, name)
                )
                with lock2:
                    discovered.extend(items)

        disc_threads = [
            threading.Thread(target=discover, args=(h,), daemon=True) for h in all_hosts
        ]
        for t in disc_threads:
            t.start()
        for t in disc_threads:
            t.join()

        seen_keys: set[tuple[str, str]] = set()
        deduped: list[dict[str, Any]] = []
        for r in discovered:
            key = (r.get("host", ""), r.get("name", ""))
            if key in seen_keys:
                sys.stderr.write(
                    f"runspec-console: warning: '{key[1]}' defined in multiple"
                    f" installed packages on host '{key[0]}', keeping first\n"
                )
                sys.stderr.flush()
            else:
                seen_keys.add(key)
                deduped.append(r)
        with self._lock:
            self._runnables_cache = deduped
        self._dispatch("runspec:runnables_updated", {})
        # The fleet's runnables changed — refresh every staffed queue's
        # self-service catalog so visitors are offered what can run *now*.
        try:
            self._publish_catalogs()
        except Exception:
            logger.exception("chat: catalog republish after discovery failed")

        # Close transports that have gone idle beyond their TTL (cheap, per-cycle
        # — no separate timer thread).
        if self._ssh_pool is not None:
            self._ssh_pool.reap()

    def _check_connected(self, host: dict[str, Any]) -> bool:
        ssh = host.get("ssh")
        if not ssh:
            return True
        pool = self._ssh_pool
        if pool is not None:
            # Probe-free: obtaining a live pooled transport IS the connectivity
            # check, and it warms the connection discovery reuses this cycle —
            # no separate ``ssh host true`` handshake.
            return pool.connectable(
                ssh,
                identity_file=host.get("identityFile"),
                global_ssh_config=self.get_config().get("ssh"),
            )
        from .executor import ssh_run

        code, _, _ = ssh_run(
            ssh,
            "true",
            identity_file=host.get("identityFile"),
            global_ssh_config=self.get_config().get("ssh"),
            timeout=5,
        )
        return code == 0

    def _ensure_plugins_imported(self, cfg: dict[str, Any]) -> None:
        """Import any `[plugins] modules` once so their register_adapter() runs."""
        if getattr(self, "_plugins_imported", False):
            return
        modules = cfg.get("plugins", {}).get("modules") or []
        if modules:
            from .adapters.base import import_plugin_modules

            import_plugin_modules(list(modules))
        self._plugins_imported = True

    def list_providers(self) -> list[dict[str, str]]:
        """Resolvable LLM providers as {value, label} — built-ins plus any
        discovered plugins (entry points + `[plugins] modules`). Drives the
        Settings provider dropdown so installed plugin providers show up."""
        cfg = self.get_config()
        self._ensure_plugins_imported(cfg)
        from .adapters.base import available_providers

        labels = {
            "anthropic": "Anthropic",
            "openai": "OpenAI",
            "bedrock": "AWS Bedrock",
            "langserve": "LangServe gateway",
        }
        return [{"value": p, "label": labels.get(p, p)} for p in available_providers()]

    def _get_adapter(self, system_override: str | None = None) -> Any:
        # A system_override (used by trigger turns) builds a one-off adapter with
        # that system prompt — never served from / written to the shared cache.
        if system_override is None and self._adapter is not None:
            return self._adapter
        cfg = self.get_config()
        provider = cfg.get("llm", {}).get("provider")
        if not provider:
            return None
        self._ensure_plugins_imported(cfg)
        kwargs: dict[str, Any] = {}
        llm_cfg = cfg.get("llm", {})
        if llm_cfg.get("api_key"):
            kwargs["api_key"] = llm_cfg["api_key"]
        if llm_cfg.get("model"):
            kwargs["model"] = llm_cfg["model"]
        # Optional standing instructions for the assistant (site policy, host
        # context, tone). Only override when set so adapters keep their default.
        if str(llm_cfg.get("system", "")).strip():
            kwargs["system"] = llm_cfg["system"]
        if system_override is not None:
            kwargs["system"] = system_override  # trigger rules win for this turn
        if llm_cfg.get("aws_region"):
            kwargs["aws_region"] = llm_cfg["aws_region"]
        if llm_cfg.get("base_url"):
            kwargs["base_url"] = llm_cfg["base_url"]
        if llm_cfg.get("api_key_command"):
            kwargs["api_key_command"] = llm_cfg["api_key_command"]
        if llm_cfg.get("api_key_ttl_ms") is not None:
            kwargs["api_key_ttl_ms"] = int(llm_cfg["api_key_ttl_ms"])
        # LangServe-only knobs — describe the gateway's input/output shape. Only
        # forwarded when set (and only meaningful to the langserve adapter), so
        # other providers never see them.
        for key in (
            "auth_header",
            "auth_scheme",
            "input_messages_key",
            "input_tools_key",
        ):
            if llm_cfg.get(key):
                kwargs[key] = llm_cfg[key]
        if llm_cfg.get("tools_in_config") is not None:
            kwargs["tools_in_config"] = bool(llm_cfg["tools_in_config"])
        try:
            from .adapters.base import load_adapter

            adapter = load_adapter(provider, **kwargs)
        except ImportError as exc:
            builtins = {"anthropic", "openai", "bedrock", "langserve"}
            if provider in builtins:
                raise ImportError(
                    f"LLM provider '{provider}' requires its optional dependency. "
                    f"Install it with: pip install runspec-console[{provider}]"
                ) from exc
            # A plugin provider (entry point / dotted path) resolved, but a
            # dependency is missing. Surface the real cause and point at the
            # plugin package — there is no `runspec-console[{provider}]` extra.
            raise ImportError(
                f"LLM provider '{provider}' could not be loaded: {exc}. "
                f"Install the package that provides '{provider}' (and its "
                f"dependencies) into the same environment as runspec-console."
            ) from exc
        except ValueError as exc:
            raise ValueError(f"LLM configuration error: {exc}") from exc
        # When a key-vending command is configured, don't cache the adapter — the
        # adapter's own TTL controls refresh. Static-key configs cache as before.
        # A system_override adapter is one-off and never cached.
        if system_override is None and not llm_cfg.get("api_key_command"):
            self._adapter = adapter
        return adapter

    # ── event triggers ────────────────────────────────────────────────────────

    def _base_system(self) -> str:
        """Base system prompt for trigger agent turns: the configured assistant
        instructions if set, else a sensible default. compose_system() layers
        the per-trigger rules on top of this."""
        configured = str(self.get_config().get("llm", {}).get("system", "")).strip()
        if configured:
            return configured
        return (
            "You are an assistant embedded in runspec-console. You can run "
            "runspec runnables on local and remote hosts via tools. Use tools "
            "when they help, and confirm before anything destructive."
        )

    def start_triggers(self) -> None:
        """Build the trigger engine from config and start its sources. Idempotent
        — safe to call again; it no-ops once an engine is running."""
        with self._lock:
            if self._trigger_engine is not None:
                return

        from . import sources  # noqa: F401 — registers built-in event sources
        from .config import read_triggers, trigger_file_error
        from .trigger_runner import make_handler
        from .triggers import TriggerEngine, load_triggers

        file_error = trigger_file_error()
        if file_error:
            # A TOML syntax error rejects the whole file → no triggers load.
            # Log loudly (the UI surfaces it via list_triggers' errors).
            logger.warning("trigger config: %s", file_error)
        triggers, errors = load_triggers(read_triggers())
        for err in errors:
            logger.warning("trigger config: %s", err)
        # A mapped shared mailbox still needs the engine running (it owns the
        # Outlook source + the new_mail → queue-card fold), so don't bail on zero
        # triggers when a mailbox is configured.
        if not triggers and not self._mailbox_queues():
            return
        handler = make_handler(
            _BridgeTriggerOps(self),
            base_system=self._base_system(),
            # draft_reply (save a reply to Outlook Drafts) is available; the tool
            # is only offered when a trigger enables reply.draft AND the event
            # carries a message to reply to.
            draft_available=True,
        )
        # Start the Outlook source even without a mail/reminder trigger when a
        # shared mailbox is mapped to a queue — new_mail then flows in to surface
        # email queue cards (the email channel; helpdesk-channels.md).
        always_start = {"outlook"} if self._mailbox_queues() else set()
        engine = TriggerEngine(
            triggers,
            handler=handler,
            on_card=self._on_trigger_card,
            on_event=self._on_trigger_event,
            # Fold a shared-mailbox new_mail event onto a queue card, regardless of
            # whether any trigger matches (the email analog of the chat source).
            on_source_event=self._on_engine_event,
            # Claim-gate email agent turns: a help-desk new_mail only routes (and
            # so can start an agent turn) once this console owns the thread — the
            # email analog of _feed_engine_for. Non-queue mail routes ungated.
            feed_gate=self._email_feed_gate,
            clock_ok=self._build_clock_ok(),
            # The chat source is owned by the always-on supervisor (start_chat),
            # not the engine — it pushes messages in via engine.feed(). Marking it
            # external stops the engine constructing/starting a second one. "email"
            # is the bridge-synthesised lifecycle channel (the `claimed` event the
            # bridge feeds on an ownership flip) — likewise fed, never started.
            external_sources={"chat", "email"},
            always_start=always_start,
        )
        problems = engine.start()
        with self._lock:
            self._trigger_engine = engine
        for problem in problems:
            logger.info("trigger source: %s", problem)
        # The email channel is forward-only — it reacts to mail that arrives while
        # the console is running (NewMailEx), with no startup backlog scan. Claim/
        # lifecycle state lives in the mailbox markers and re-coordinates on the
        # next message after a restart.
        # Note: no UI dispatch here. start_triggers() runs from set_window(),
        # before the web view has finished starting, so an evaluate_js push would
        # raise (harmlessly, but noisily). The Triggers tab pulls this state via
        # list_triggers() on mount and after any enable/disable, so a push isn't
        # needed — and restart_triggers() re-runs this once the window is up.

    def stop_triggers(self) -> None:
        # Wake only *trigger* turns parked at the gate (ids that are trigger
        # cards) so the engine's workers aren't stuck in event.wait() when we tear
        # the executor down — without disturbing an in-flight chat turn (restart
        # is also used by the enable/disable toggle). The chat source is not the
        # engine's to stop — it runs always-on (see start_chat/stop_chat).
        with self._lock:
            engine = self._trigger_engine
            self._trigger_engine = None
            card_ids = set(self._trigger_cards)
        self._abort_turns(card_ids)
        if engine is not None:
            engine.stop()

    def _build_clock_ok(self) -> Callable[[], bool]:
        """Build the working-hours rota gate (read fresh each call so a window
        boundary is respected without restarting). No/disabled rota → always-on.
        With source="outlook", availability comes from the calendar (falling back
        to the console grid if it can't be read); otherwise the manual grid. Shared
        by the trigger engine and the chat presence watcher so room presence and
        trigger firing follow the same schedule."""
        from datetime import datetime

        from .config import read_rota
        from .triggers import _DAYS, within_rota

        rota = read_rota()
        grid_configured = any(isinstance(rota.get(d), dict) for d in _DAYS)

        def clock_ok() -> bool:
            now = datetime.now()
            if not rota.get("enabled"):
                return True
            if rota.get("source") == "outlook":
                from .sources.outlook import calendar_clock_ok

                res = calendar_clock_ok(
                    now,
                    str(rota.get("shift_marker") or "shift"),
                    bool(rota.get("hold_during_meetings", False)),
                )
                if res is not None:
                    return res
                # Calendar unreadable → fall back to the console grid if set.
                return within_rota(rota, now) if grid_configured else True
            return within_rota(rota, now)

        return clock_ok

    # ── always-on chat room membership ──────────────────────────────────────────

    def start_chat(self) -> None:
        """Join every room declared on a fleet host (``room`` + ``room_port`` on the
        host entry) — always-on, independent of triggers. One reconnecting
        supervisor per room, each tunnelling over that host's own pooled
        connection. Idempotent; a no-op with no SSH pool or no rooms configured."""
        from .sources.chat_transport import (
            make_host_room_exec_factory,
            make_host_room_factory,
        )

        with self._lock:
            if self._chat_supervisors:
                return  # already running
            pool = self._ssh_pool
        if pool is None:
            return
        specs = self._room_specs()
        if not specs:
            return
        ssh_cfg = self.get_config().get("ssh")
        ssh_cfg = ssh_cfg if isinstance(ssh_cfg, dict) else None
        self._chat_stop.clear()
        threads: dict[str, threading.Thread] = {}
        for spec in specs:
            room = spec["room"]
            if room in threads:
                logger.warning(
                    "chat: duplicate room %r (host %r) — skipping", room, spec["host"]
                )
                continue
            if spec.get("via") == "exec":
                # SSH port-forwarding blocked → run console-room connect over an
                # exec channel instead (no direct-tcpip tunnel).
                factory = make_host_room_exec_factory(
                    pool, spec, global_ssh_config=ssh_cfg
                )
            else:
                factory = make_host_room_factory(pool, spec, global_ssh_config=ssh_cfg)
            threads[room] = threading.Thread(
                target=self._chat_supervise,
                args=(spec, factory),
                name=f"chat-{room}",
                daemon=True,
            )
        with self._lock:
            self._chat_supervisors = threads
        for thread in threads.values():
            thread.start()

    def _room_specs(self) -> list[dict[str, Any]]:
        """Room specs from fleet host entries, with a legacy ``[chat]`` fallback
        (a ``[chat]`` table whose ``host`` names a fleet entry — so older configs
        keep working *and* pick up that host's ssh target + key)."""
        from . import config
        from .sources.chat_transport import default_room_user, host_rooms

        # The configured [ssh] default username — the login the tunnel actually
        # authenticates as. Used as the room display name when a host's ssh target
        # carries no user@ (so a Windows console shows the Linux identity).
        ssh_section = self.get_config().get("ssh")
        ssh_user = str((ssh_section or {}).get("user") or "")

        self._reload_hosts()
        specs = host_rooms(self._hosts, ssh_user=ssh_user)
        if specs:
            return specs
        cfg = config.read_chat()
        room = str(cfg.get("room") or "").strip()
        port = cfg.get("port")
        name = str(cfg.get("host") or "").strip()
        if not (room and name and isinstance(port, int) and not isinstance(port, bool)):
            return []
        entry = next((h for h in self._hosts if str(h.get("name") or "") == name), None)
        ssh_target = str((entry or {}).get("ssh") or "").strip()
        if not ssh_target:
            return []  # legacy host must resolve to a fleet entry with an ssh target
        return [
            {
                "host": name,
                "ssh_target": ssh_target,
                "identity_file": entry.get("identityFile") or None,
                "room": room,
                "port": int(port),
                "dest_host": str(cfg.get("dest_host") or "127.0.0.1"),
                "user": str(cfg.get("user") or "").strip()
                or default_room_user(ssh_target, ssh_user),
            }
        ]

    def stop_chat(self) -> None:
        """Leave every room and stop all reconnect supervisors. Idempotent."""
        self._chat_stop.set()
        with self._lock:
            threads = list(self._chat_supervisors.values())
            self._chat_supervisors = {}
        for thread in threads:
            thread.join(timeout=3)
        with self._lock:
            self._chat_sources = {}

    def restart_chat(self) -> None:
        """Re-join with fresh config (after a host/room, ``[chat]`` or rota change)."""
        self.stop_chat()
        self.start_chat()

    # Backoff bounds for the reconnect loop (seconds); attributes so tests shrink them.
    _CHAT_BACKOFF_START = 2.0
    _CHAT_BACKOFF_MAX = 60.0

    def _chat_supervise(self, spec: dict[str, Any], factory: Any) -> None:
        """Own one room's connection: connect, run until it drops, then reconnect
        with exponential backoff — until stop_chat(). Drives presence off the rota
        and keeps ``self._chat_sources[room]`` pointed at the live source for
        send_room_message / get_room_state."""
        from .sources.chat import ChatSource

        room, user, host = spec["room"], spec["user"], spec["host"]
        # A spec with a queue is helpdesk agent mode: subscribe to the queue
        # namespace (the spec's room key IS the namespace) instead of one room.
        queue = str(spec.get("queue") or "")
        backoff = self._CHAT_BACKOFF_START
        while not self._chat_stop.is_set():
            closed = threading.Event()
            source = ChatSource(
                room="" if queue else room,
                namespace=room if queue else None,
                user=user,
                transport_factory=factory,
                on_closed=closed.set,
            )
            try:
                source.start(self._on_chat_event)
            except Exception as exc:
                # A blocked-forwarding failure is permanent — log one actionable
                # line, not a fresh traceback every backoff. Genuinely unexpected
                # errors still get the full traceback.
                hint = _chat_connect_hint(exc, via=str(spec.get("via") or "tunnel"))
                if hint:
                    logger.error("chat: room %r — %s", room, hint)
                else:
                    logger.exception("chat: connect to room %r failed", room)
                if self._chat_stop.wait(backoff):
                    break
                backoff = min(backoff * 2, self._CHAT_BACKOFF_MAX)
                continue
            with self._lock:
                self._chat_sources[room] = source
                if queue:
                    self._agent_rooms[room] = user
            backoff = self._CHAT_BACKOFF_START
            if queue:
                # Publish the queue's self-service catalog right after subscribing
                # (forced — a fresh connection's hub has no catalog yet; later
                # discovery refreshes republish only on change — _publish_catalogs).
                self._publish_catalog(spec, source, force=True)
            watcher = _RotaPresenceWatcher(self._build_clock_ok(), source.set_presence)
            watcher.start()
            self._dispatch(
                "runspec:room_connection",
                {"room": room, "host": host, "connected": True, "queue": queue},
            )
            # Wait until the connection drops or we're told to stop (poll so stop
            # stays responsive).
            while not self._chat_stop.is_set() and not closed.wait(0.5):
                pass
            watcher.stop()
            source.stop()
            with self._lock:
                self._chat_sources.pop(room, None)
                self._agent_rooms.pop(room, None)
                self._published_catalogs.pop(
                    room, None
                )  # force a fresh publish on reconnect
            self._dispatch(
                "runspec:room_connection",
                {"room": room, "host": host, "connected": False, "queue": queue},
            )
            if self._chat_stop.is_set():
                break
            # Unexpected drop → back off, then reconnect.
            if self._chat_stop.wait(backoff):
                break
            backoff = min(backoff * 2, self._CHAT_BACKOFF_MAX)

    def _publish_catalog(
        self, spec: dict[str, Any], source: Any, *, force: bool = False
    ) -> None:
        """Build and publish one queue's self-service catalog from the discovery
        cache. **Idempotent**: the catalog is only sent (and logged) when its
        contents changed since the last publish for this namespace — so the
        periodic discovery refresh doesn't re-emit an unchanged catalog every
        cycle. ``force`` overrides the diff (the subscribe path always sends, as
        a fresh connection's hub has no catalog yet). Best-effort — a failed
        publish never breaks the connection; the next refresh or reconnect
        republishes."""
        from .catalog import build_catalog

        try:
            key = spec["room"]
            with self._lock:
                runnables = list(self._runnables_cache)
            allowlist = spec.get("catalog_allowlist")
            tools, index = build_catalog(runnables, allowlist=allowlist)
            with self._lock:
                self._catalog_index[key] = index  # enactment map — keep current
                unchanged = not force and self._published_catalogs.get(key) == tools
                if not unchanged:
                    self._published_catalogs[key] = tools
            if unchanged:
                return  # nothing to send — the room already has this catalog
            source.send_catalog(tools)
            logger.info("chat: published %d tool(s) to %r catalog", len(tools), key)
        except Exception:
            logger.exception("chat: catalog publish for %r failed", spec.get("room"))

    def _publish_catalogs(self) -> None:
        """Republish every connected agent queue's catalog — called after a
        discovery refresh so the room always offers what the fleet can run now."""
        specs = [s for s in self._room_specs() if s.get("queue")]
        with self._lock:
            sources = dict(self._chat_sources)
        for spec in specs:
            source = sources.get(spec["room"])
            if source is not None:
                self._publish_catalog(spec, source)

    def _feed_engine_for(self, payload: dict[str, Any]) -> bool:
        """The claim gate for agent (helpdesk) messages. A message in a queue we
        staff feeds the trigger engine — and so can start an agent LLM turn —
        only once the thread is **assigned to this console** (someone clicked
        Claim). Unclaimed-thread messages stay UI-only: the inbox shows them,
        but no model spend happens until a human claims (helpdesk-rooms.md §4).
        Group-chat messages (no covering namespace) always feed."""
        room = str(payload.get("room") or "")
        with self._lock:
            agent_rooms = dict(self._agent_rooms)
            state = self._thread_states.get(room)
        for namespace, agent_user in agent_rooms.items():
            if room == namespace or room.startswith(namespace + "/"):
                return bool(state) and state.get("assignee") == agent_user
        return True

    def _email_feed_gate(self, event: Any) -> bool:
        """The claim gate for email agent turns — the email analog of
        :meth:`_feed_engine_for`. A shared-mailbox (help-desk queue) ``new_mail``
        reaches the trigger engine — and so can start an agent turn that drafts a
        reply — only once this console **owns** the folded conversation (someone
        here clicked Claim and the ack folded back). Until then the mail is
        UI-only: the queue card surfaces it (``_ingest_email`` runs in the
        source-event hook, *before* this gate), but no model spend happens —
        mirroring chat, where an unclaimed thread never feeds the engine.

        Two further suppressions: a claim/lifecycle **marker ack** is internal
        plumbing, never an agent turn; and non-queue mail (personal / other) feeds
        normally — there's no claim concept for it, so a user's own Outlook
        trigger still works ungated."""
        if event.source != "outlook" or event.type != "new_mail":
            return True
        from . import mailbox_claim

        payload = dict(event.payload)
        if self._email_queue_for(payload) is None:
            return True  # not a mapped mailbox — not a help-desk email
        if mailbox_claim.parse_marker(str(payload.get("claim_marker") or "")):
            return False  # a marker ack — plumbing, never an agent turn
        conversation = Bridge._email_conversation_key(payload)
        card_id = f"queue:email:{conversation}"
        with self._lock:
            card = self._trigger_cards.get(card_id)
            detail = card.get("detail") if card else None
        return bool(detail and detail.get("owned_by_me"))

    def _on_chat_event(self, event: Any) -> None:
        """A chat source's emit: fan every event to the Rooms view, and route
        *messages* (not presence) to the live trigger engine so a chat trigger can
        still fire. Presence stays UI-only, out of the trigger audit feed.
        ``conversation`` frames (helpdesk thread lifecycle) update the thread map
        and go to the UI only — the claim gate reads the map (_feed_engine_for)."""
        payload = dict(event.payload)
        if event.type == "presence":
            self._dispatch("runspec:room_presence", payload)
            return
        if event.type == "conversation":
            room = str(payload.get("room") or "")
            if room:
                with self._lock:
                    self._thread_states[room] = {
                        "state": payload.get("state"),
                        "assignee": payload.get("assignee"),
                    }
                self._touch_queue_card(
                    room,
                    state=payload.get("state"),
                    assignee=payload.get("assignee"),
                    have_state=True,
                )
            self._dispatch("runspec:room_conversation", payload)
            return
        self._dispatch("runspec:room_message", payload)
        self._notify_room_message(payload)
        self._touch_queue_card(str(payload.get("room") or ""), text=payload.get("text"))
        if not self._feed_engine_for(payload):
            return
        with self._lock:
            engine = self._trigger_engine
        if engine is not None:
            try:
                engine.feed(event)
            except Exception:
                logger.exception("chat: routing message to the trigger engine failed")

    # State → kanban column for a queue card's StatusBadge fallback (the row's
    # real lifecycle chip is rendered from detail["state"] in the UI).
    _QUEUE_STATE_COLUMN = {
        "open": "triggered",
        "assigned": "triage",
        "resolved": "done",
        "closed": "done",
    }

    @staticmethod
    def _parse_queue_room(room: str) -> tuple[str, str] | None:
        """Split a ``helpdesk/<queue>/<visitor>`` conversation room into
        ``(queue, visitor)``; ``None`` for anything else (e.g. a group-chat room),
        which never becomes a queue card."""
        parts = room.split("/")
        if len(parts) != 3 or parts[0] != "helpdesk" or not parts[1] or not parts[2]:
            return None
        return parts[1], parts[2]

    def _touch_queue_card(
        self,
        room: str,
        *,
        text: Any = None,
        state: Any = None,
        assignee: Any = None,
        have_state: bool = False,
    ) -> None:
        """Create or update the single trigger card that represents a helpdesk
        conversation, so a queue message is a first-class card in the Triggers
        inbox (not a separate tab). Keyed per conversation (``queue:<room>``) so
        repeated messages update one card instead of flooding. ``text`` bumps the
        summary + timeline; ``have_state`` (with ``state``/``assignee``) folds a
        lifecycle change onto the card. Model spend stays gated on Claim — the
        card forms regardless so the operator can see and claim it."""
        parsed = Bridge._parse_queue_room(room)
        if parsed is None:
            return
        queue, visitor = parsed
        card_id = f"queue:{room}"
        now = time.time()
        with self._lock:
            card = self._trigger_cards.get(card_id)
            if card is None:
                card = {
                    "card_id": card_id,
                    "trigger": f"queue:{queue}",
                    "source": "chat",
                    "event_type": "message",
                    "summary": "",
                    "column": "triggered",
                    "detail": {"room": room, "queue": queue, "visitor": visitor},
                    "created_at": now,
                    "timeline": [],
                }
                self._trigger_cards[card_id] = card
            detail = card["detail"]
            if text:
                snippet = str(text)
                card["summary"] = (
                    snippet if len(snippet) <= 120 else snippet[:117] + "…"
                )
                card["timeline"].append(
                    {"ts": now, "text": f"{visitor}: {card['summary']}"}
                )
            if have_state:
                detail["state"] = state
                detail["assignee"] = assignee
                card["column"] = Bridge._QUEUE_STATE_COLUMN.get(
                    str(state or ""), card["column"]
                )
                card["timeline"].append({"ts": now, "text": f"conversation {state}"})
            if not card["summary"]:
                card["summary"] = f"{visitor} · {queue}"
            self._evict_trigger_cards(card_id)
            snapshot = dict(card)
        self._dispatch("runspec:trigger_card", snapshot)

    def _evict_trigger_cards(self, keep_id: str) -> None:
        """Cap ``_trigger_cards`` so a long-running session can't grow without
        bound: when over 200, drop the 50 oldest (never the card just touched).
        The caller holds ``self._lock``. Same bound as ``_on_trigger_card``."""
        if len(self._trigger_cards) <= 200:
            return
        oldest = sorted(
            self._trigger_cards.values(), key=lambda c: c.get("created_at", 0.0)
        )[:50]
        for old in oldest:
            if old["card_id"] != keep_id:
                self._trigger_cards.pop(old["card_id"], None)

    # ── email channel: a shared mailbox as a second queue channel ────────────────
    #
    # The email analog of the chat queue card. A new_mail event addressed to a
    # mapped shared mailbox folds onto one trigger card per email conversation
    # (keyed by Outlook ConversationID), so a help-desk mailbox feeds the same
    # Triggers inbox as a chat room. Claim + lifecycle ride the mailbox itself:
    # an ack email addressed to the queue carries a marker, every console folds
    # the thread's markers (mailbox_claim.fold_conversation), and first ack wins —
    # the email card's state/owner come from that fold. Reply is a later slice
    # (see docs/design/helpdesk-channels.md).

    def _mailbox_queues(self) -> dict[str, str]:
        """The ``[mailboxes]`` shared-mailbox → queue map, read fresh from config
        (lowercased addresses). Empty when no mailbox is mapped — the email
        channel is then dormant and surfaces no cards."""
        from . import config

        return config.read_mailboxes()

    def _email_queue_for(self, payload: dict[str, Any]) -> str | None:
        """The queue a new_mail event belongs to, or ``None`` for personal/other
        mail (which never becomes a queue card — the email analog of a plain,
        non-helpdesk chat room).

        Authoritative path: the real SMTP ``recipients`` parsed from the transport
        headers (To/Cc + the envelope-delivery headers), matched **exactly** against
        a mapped mailbox address — so a lookalike address can't match, and an alias
        / distribution-list delivery is still recognised. Falls back to a substring
        match on the display ``To`` for internal mail that carries no header block.
        First match wins."""
        mapping = self._mailbox_queues()
        if not mapping:
            return None
        for address in payload.get("recipients") or []:
            queue_name = mapping.get(str(address).lower())
            if queue_name:
                return queue_name
        recipients = str(payload.get("to") or "").lower()
        if recipients:
            for address, queue_name in mapping.items():
                if address in recipients:
                    return queue_name
        return None

    @staticmethod
    def _email_conversation_key(payload: dict[str, Any]) -> str:
        """A stable per-conversation key so an email and its replies fold onto one
        card. Outlook's ConversationID when present; otherwise the subject with
        leading reply/forward prefixes stripped (so a ``Re:`` reply still threads)."""
        cid = str(payload.get("conversation_id") or "").strip()
        if cid:
            return cid
        subject = str(payload.get("subject") or "").strip()
        lowered = subject.lower()
        while True:
            for prefix in ("re:", "fw:", "fwd:"):
                if lowered.startswith(prefix):
                    subject = subject[len(prefix) :].strip()
                    lowered = subject.lower()
                    break
            else:
                break
        return subject or "(no subject)"

    def _email_mailbox_for(self, queue: str) -> str:
        """The shared-mailbox address that staffs ``queue`` — the reverse of the
        ``[mailboxes]`` map, i.e. where a claim/lifecycle ack for this queue is
        sent. "" if the queue isn't mapped (shouldn't happen for a live card)."""
        for address, name in self._mailbox_queues().items():
            if name == queue:
                return address
        return ""

    def _email_is_triggered(self, payload: dict[str, Any]) -> bool:
        """Whether a shared-mailbox email matches a configured trigger — the gate
        on **opening a ticket**. The operator curates which mail becomes a
        claimable card via a trigger's ``match`` (so a busy shared mailbox doesn't
        flood the help-desk inbox — we surface the *triggered* flow, not the whole
        mailbox). No engine, or no trigger matches → not a ticket."""
        from .events import Event

        with self._lock:
            engine = self._trigger_engine
        if engine is None:
            return False
        try:
            return engine.would_match(Event.new("outlook", "new_mail", payload))
        except Exception:  # noqa: BLE001 — a match check must never crash ingest
            logger.exception("email: trigger-match check failed")
            return False

    def _ingest_email(self, payload: dict[str, Any]) -> None:
        """Record one shared-mailbox ``new_mail`` and refresh its **ticket** card.

        A ticket is opened only when a requester email **matches a configured
        trigger** (:meth:`_email_is_triggered`); other mail stays ordinary inbox
        mail — no card, no state kept. Mail not addressed to a mapped mailbox is
        never a ticket either. For an existing ticket, an ordinary email bumps the
        summary and can reopen a resolved/closed thread, and a claim/lifecycle ack
        (``claim_marker`` set) folds into ownership/state (plumbing, not a requester
        message — and it never *opens* a ticket).

        When this console **takes ownership** (a claim ack folds us in as owner), a
        synthetic ``email``/``claimed`` event is fed so an agent trigger
        (``source = "email"``, ``event = "claimed"``) can draft a reply *on claim*.
        Forward-only: there is no backlog scan — claim/lifecycle state lives in the
        mailbox markers and re-coordinates on the next message after a restart."""
        from . import mailbox_claim

        queue = self._email_queue_for(payload)
        if queue is None:
            return  # personal / non-queue mail — never a ticket
        conversation = Bridge._email_conversation_key(payload)
        marker = mailbox_claim.parse_marker(str(payload.get("claim_marker") or ""))
        # A triggered requester email may *open* a ticket; a marker never does
        # (it's plumbing for a ticket that already exists).
        triggered = marker is None and self._email_is_triggered(payload)
        order_key = (
            float(payload.get("received_ts") or 0.0),
            str(payload.get("message_id") or ""),
        )
        event: dict[str, Any]
        if marker is not None:
            event = {"order_key": order_key, "kind": marker.kind, "agent": marker.agent}
        else:
            event = {"order_key": order_key, "kind": "message", "from_requester": True}

        card_id = f"queue:email:{conversation}"
        visitor = str(payload.get("from") or "") or "unknown"
        subject = str(payload.get("subject") or "").strip()
        snippet = subject or str(payload.get("body") or "").strip()
        me = self._agent_identity()  # read config outside the lock
        dedup_key = str(payload.get("message_id") or payload.get("entry_id") or "")
        now = time.time()
        claimed_payload: dict[str, Any] | None = None  # set on the ownership flip
        with self._lock:
            if dedup_key and dedup_key in self._email_seen:
                return  # folded already (the live source can re-deliver an item)
            card = self._trigger_cards.get(card_id)
            if card is None and not triggered:
                # Not a ticket: an email that matched no trigger, or a marker for a
                # thread we never surfaced. Leave it in the inbox — no card, and no
                # per-conversation state kept (so non-ticket mail can't grow memory).
                return
            if dedup_key:
                if len(self._email_seen) > 20000:
                    self._email_seen.clear()  # bound a very long session
                self._email_seen.add(dedup_key)
            events = self._email_threads.setdefault(conversation, [])
            events.append(event)
            folded = mailbox_claim.fold_conversation(events)
            state, owner = folded["state"], folded["owner"]
            if card is None:
                # Only a triggered requester message reaches here, so the card is
                # always opened by addressable mail; entry_id is set just below.
                card = {
                    "card_id": card_id,
                    "trigger": f"queue:{queue}",
                    "source": "email",
                    "event_type": "new_mail",
                    "summary": "",
                    "column": "triggered",
                    "detail": {
                        "queue": queue,
                        "visitor": visitor,
                        "conversation_id": conversation,
                        "mailbox": self._email_mailbox_for(queue),
                        "entry_id": "",
                    },
                    "created_at": now,
                    "timeline": [],
                }
                self._trigger_cards[card_id] = card
            detail = card["detail"]
            # A marker ack is internal plumbing — never the addressable mail to
            # reply to, and never a requester message in the summary/timeline.
            if marker is None:
                entry_id = str(payload.get("entry_id") or "")
                if entry_id:
                    detail["entry_id"] = entry_id
                # Retain the latest requester message so a draft-on-claim turn has
                # content to reply to — the claim ack that triggers it carries none.
                detail["req_subject"] = subject
                detail["req_body"] = str(payload.get("body") or "")
                if snippet:
                    text = snippet if len(snippet) <= 120 else snippet[:117] + "…"
                    card["summary"] = text
                    card["timeline"].append({"ts": now, "text": f"{visitor}: {text}"})
            # Lifecycle/ownership come from the fold; log a transition once.
            prev_state = detail.get("state")
            prev_owned = bool(detail.get("owned_by_me"))
            detail["state"] = state
            detail["owner"] = owner
            # Whether *this* console owns it — the reply gate (rule 3) and the
            # UI's "you can reply" affordance read this, not the raw owner string.
            detail["owned_by_me"] = bool(owner) and owner == me
            # Taking ownership (not-ours → ours): build the claimed event so a
            # configured trigger can draft on claim, with a requester message to
            # act on. recipients = [mailbox] so queue_mailbox_for resolves the
            # send-as identity (the agent drafts as the queue).
            if detail["owned_by_me"] and not prev_owned and detail.get("entry_id"):
                claimed_payload = {
                    "queue": queue,
                    "conversation_id": conversation,
                    "from": detail.get("visitor", ""),
                    "subject": detail.get("req_subject", ""),
                    "entry_id": detail.get("entry_id", ""),
                    "recipients": [detail.get("mailbox", "")],
                    "body": detail.get("req_body", ""),
                }
            card["column"] = Bridge._QUEUE_STATE_COLUMN.get(state, card["column"])
            if state != prev_state:
                who = f" by {owner}" if owner else ""
                card["timeline"].append(
                    {"ts": now, "text": f"conversation {state}{who}"}
                )
            if not card["summary"]:
                card["summary"] = f"{visitor} · {queue}"
            self._evict_trigger_cards(card_id)
            snapshot = dict(card)
        self._dispatch("runspec:trigger_card", snapshot)
        if claimed_payload is not None:
            self._feed_email_event("claimed", claimed_payload)

    def _feed_email_event(self, kind: str, payload: dict[str, Any]) -> None:
        """Feed a synthetic ``email`` lifecycle event (e.g. ``claimed``) to the
        trigger engine — the same external-source path chat uses, so the existing
        trigger config drives it. Best-effort: no engine, or a feed failure, is
        logged and never blocks the caller."""
        from .events import Event

        with self._lock:
            engine = self._trigger_engine
        if engine is None:
            return
        try:
            engine.feed(Event.new("email", kind, payload))
        except Exception:
            logger.exception("email: feeding %s event failed", kind)

    def _agent_identity(self) -> str:
        """A stable identity for this console in claim markers — ``[console]
        agent_id`` when set, else ``<osuser>@<host>``. Distinguishes consoles so
        first-ack-wins picks a deterministic winner, and stays consistent across a
        claim and its later lifecycle actions so the owner checks in the fold hold."""
        from . import config

        console = config.read_config().get("console")
        if isinstance(console, dict):
            agent = str(console.get("agent_id") or "").strip()
            if agent:
                return agent
        import getpass
        import socket

        try:
            user = getpass.getuser()
        except Exception:
            user = "agent"
        return f"{user}@{socket.gethostname()}"

    def _email_control(self, kind: str, card_id: str) -> dict[str, Any]:
        """Send one claim/lifecycle marker email (claim/resolve/reopen/close) for
        an email queue card. The ack is addressed to the shared mailbox itself
        (not the requester) and threaded on the conversation; ownership/state
        change only when it lands back and folds (read-after-write — we never
        optimistically set state here). Returns ``{ok}`` / ``{ok: False, error}``."""
        from .mailbox_claim import Marker, format_marker
        from .sources.outlook import send_claim_marker

        with self._lock:
            card = self._trigger_cards.get(card_id)
            detail = dict(card["detail"]) if card else {}
        if not detail:
            return {"ok": False, "error": "unknown card"}
        conversation = str(detail.get("conversation_id") or "")
        queue = str(detail.get("queue") or "")
        mailbox = str(detail.get("mailbox") or "") or self._email_mailbox_for(queue)
        entry_id = str(detail.get("entry_id") or "")
        if not (conversation and queue and mailbox and entry_id):
            return {"ok": False, "error": "card is missing claim routing info"}
        marker = Marker(
            kind=kind,
            queue=queue,
            conversation_id=conversation,
            agent=self._agent_identity(),
        )
        try:
            send_claim_marker(entry_id, format_marker(marker), mailbox)
        except Exception as exc:  # noqa: BLE001 — report to the UI, don't crash
            logger.exception("email: sending %s marker failed", kind)
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def claim_email_thread(self, card_id: str) -> dict[str, Any]:
        """Claim an email queue card (send a self-addressed claim ack)."""
        return self._email_control("claim", card_id)

    def resolve_email_thread(self, card_id: str) -> dict[str, Any]:
        return self._email_control("resolve", card_id)

    def reopen_email_thread(self, card_id: str) -> dict[str, Any]:
        return self._email_control("reopen", card_id)

    def close_email_thread(self, card_id: str) -> dict[str, Any]:
        return self._email_control("close", card_id)

    def reply_email_thread(self, card_id: str, body: str) -> dict[str, Any]:
        """Reply to the requester of an email queue card — draft in Outlook and
        send. **Gated on confirmed ownership** (rule 3): only the console whose
        claim ack is the earliest in the fold may reply, so a losing claimant can
        never message the requester. Human-initiated (the operator typed and
        clicked Send), so it goes straight through ``save_reply_draft`` +
        ``send_draft`` — no autonomy gate, which is for agent-proposed replies.
        Returns ``{ok}`` / ``{ok: False, error}``."""
        from .sources.outlook import save_reply_draft, send_draft

        body = str(body or "").strip()
        if not body:
            return {"ok": False, "error": "empty reply"}
        with self._lock:
            card = self._trigger_cards.get(card_id)
            detail = dict(card["detail"]) if card else {}
        if not detail:
            return {"ok": False, "error": "unknown card"}
        if not detail.get("owned_by_me"):
            return {"ok": False, "error": "claim the conversation before replying"}
        entry_id = str(detail.get("entry_id") or "")
        if not entry_id:
            return {"ok": False, "error": "no requester message to reply to"}
        # Reply *as the shared mailbox* so the requester sees the queue identity,
        # not the operator (Q5). The card carries the mailbox address.
        mailbox = str(detail.get("mailbox") or "") or None
        try:
            draft = save_reply_draft(entry_id, body, from_mailbox=mailbox)
            send_draft(str(draft.get("entry_id") or ""))
        except Exception as exc:  # noqa: BLE001 — report to the UI, don't crash
            logger.exception("email: reply failed")
            return {"ok": False, "error": str(exc)}
        now = time.time()
        with self._lock:
            card = self._trigger_cards.get(card_id)
            if card is None:
                return {"ok": True}
            snippet = body if len(body) <= 120 else body[:117] + "…"
            card["timeline"].append({"ts": now, "text": f"↩ replied: {snippet}"})
            snapshot = dict(card)
        self._dispatch("runspec:trigger_card", snapshot)
        return {"ok": True}

    def _on_engine_event(self, event: Any) -> None:
        """Raw-event observer the trigger engine calls for every event before
        routing. Folds a shared-mailbox ``new_mail`` into a queue card (the email
        analog of ``_on_chat_event``); everything else (reminders, chat feeds,
        other sources) is left for the normal trigger path. Best-effort — a
        failure here never blocks trigger routing."""
        if event.source == "outlook" and event.type == "new_mail":
            try:
                self._ingest_email(dict(event.payload))
            except Exception:
                logger.exception("email: synthesising a queue card failed")

    def send_room_message(self, room: str, text: str) -> dict[str, Any]:
        """Post a human-composed message to ``room`` — the Rooms composer's one
        outbound path. The agent never reaches this: a proposed reply only
        pre-fills the composer (runspec:room_draft); the human reviews and sends.
        Returns ``{ok}`` or ``{ok: False, error}``."""
        room = str(room or "").strip()
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "error": "empty message"}
        target: str | None = None  # the conversation a namespace send must name
        with self._lock:
            src = self._chat_sources.get(room) if room else None
            if src is None and len(self._chat_sources) == 1 and not room:
                src = next(iter(self._chat_sources.values()))
            if src is None and room:
                # A helpdesk conversation (helpdesk/<queue>/<visitor>) isn't a
                # source key — route via the agent source staffing its namespace.
                for namespace in self._agent_rooms:
                    if room.startswith(namespace + "/"):
                        src = self._chat_sources.get(namespace)
                        target = room
                        break
        if src is None:
            return {"ok": False, "error": "not connected to that room"}
        try:
            src.send_message(text, room=target)
        except Exception as exc:  # noqa: BLE001 — report to the UI, don't crash
            logger.exception("send_room_message failed")
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def _thread_control(self, kind: str, room: str) -> dict[str, Any]:
        """Send one conversation-lifecycle control frame (claim/resolve/reopen/
        close) via the agent source staffing the conversation's namespace. The
        hub is the authority — it enforces the queue boundary and broadcasts the
        resulting state, which comes back as a ``conversation`` frame."""
        room = str(room or "").strip()
        if not room:
            return {"ok": False, "error": "no conversation named"}
        with self._lock:
            src = None
            for namespace in self._agent_rooms:
                if room == namespace or room.startswith(namespace + "/"):
                    src = self._chat_sources.get(namespace)
                    break
        if src is None:
            return {"ok": False, "error": "not staffing that conversation's queue"}
        try:
            src.send_control(kind, room)
        except Exception as exc:  # noqa: BLE001 — report to the UI, don't crash
            logger.exception("thread %s failed", kind)
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def claim_thread(self, room: str) -> dict[str, Any]:
        """Claim a helpdesk conversation for this console (→ assigned). This is
        also the gate that lets the thread's messages reach the trigger engine —
        no agent turn runs on an unclaimed thread."""
        return self._thread_control("claim", room)

    def resolve_thread(self, room: str) -> dict[str, Any]:
        """Mark a conversation resolved (the visitor can reopen by replying)."""
        return self._thread_control("resolve", room)

    def reopen_thread(self, room: str) -> dict[str, Any]:
        """Reopen a resolved/closed conversation (back to the queue)."""
        return self._thread_control("reopen", room)

    def close_thread(self, room: str) -> dict[str, Any]:
        """Close a conversation (terminal; a visitor reply still reopens)."""
        return self._thread_control("close", room)

    def get_room_state(self) -> dict[str, Any]:
        """For the Rooms view: every configured room (from the fleet host entries),
        each with its host, this console's display name there, and whether it's
        currently connected. ``{"rooms": []}`` when none are configured — the view
        shows a 'no rooms' state."""
        specs = self._room_specs()
        with self._lock:
            connected = set(self._chat_sources)
            thread_states = {k: dict(v) for k, v in self._thread_states.items()}
        rooms = []
        for s in specs:
            entry: dict[str, Any] = {
                "room": s["room"],
                "host": s["host"],
                "user": s["user"],
                "connected": s["room"] in connected,
                "queue": str(s.get("queue") or ""),
            }
            if entry["queue"]:
                # The inbox's initial threads — the hub's subscribe catch-up plus
                # everything since, keyed by conversation room. Live updates ride
                # runspec:room_conversation.
                prefix = s["room"] + "/"
                entry["threads"] = {
                    room: state
                    for room, state in thread_states.items()
                    if room.startswith(prefix)
                }
            rooms.append(entry)
        return {"rooms": rooms}

    def test_room_connection(self, host: str) -> dict[str, Any]:
        """Diagnose a room/queue's connection for the UI, fast and always-responsive.

        Fast path: if the room is already connected (the live agent/room is up),
        say so immediately — no new connection. Otherwise run an isolated, fresh
        attempt over the *same* paramiko path the supervisor uses (HTTP proxy,
        key, ssh_config, then tunnel/exec relay), but **bounded by a hard
        timeout** so the button always returns a verdict instead of spinning on a
        slow proxied connect. The attempt ignores pool backoff and doesn't disturb
        the live connection; its result carries the localised ``stage`` (ssh /
        channel / room) and a hint."""
        specs = [
            s
            for s in self._room_specs()
            if s.get("host") == host or s.get("ssh_target") == host
        ]
        if not specs:
            return {
                "ok": False,
                "stage": "config",
                "host": host,
                "message": f"No room or queue is configured on host {host!r}.",
                "hint": "Add room/room_port (group chat) or room_queue/room_port (help desk) to this host.",
            }
        spec = specs[0]
        meta = {
            "host": host,
            "room": spec["room"],
            "queue": str(spec.get("queue") or ""),
        }

        # Fast path — the live connection is already up (the common case).
        with self._lock:
            connected = spec["room"] in self._chat_sources
        if connected:
            label = (
                f"queue {spec['queue']}"
                if spec.get("queue")
                else f"room {spec['room']}"
            )
            return {
                **meta,
                "ok": True,
                "stage": "ok",
                "message": f"Connected — the console is currently joined to {label} on {host}.",
                "hint": None,
            }

        cfg = self.get_config().get("ssh")
        cfg = cfg if isinstance(cfg, dict) else None

        # Slow path — a fresh attempt, bounded so the UI never hangs. paramiko's
        # blocking connect can't be cancelled, so we run it in a daemon thread and
        # report a timeout verdict if it overruns (the thread dies on its own).
        result: dict[str, Any] = {}

        def attempt() -> None:
            result.update(self._attempt_room_connection(spec, cfg, meta))

        worker = threading.Thread(target=attempt, name="room-conn-test", daemon=True)
        worker.start()
        worker.join(timeout=12.0)
        if worker.is_alive():
            return {
                **meta,
                "ok": False,
                "stage": "ssh",
                "message": "Timed out (12s) opening a fresh connection — the SSH connect (through the HTTP proxy, if set) is slow or stuck.",
                "hint": "If discovery/transfers work, this is usually transient proxy load — retry. The live room may still connect on its own retry.",
            }
        return result or {
            **meta,
            "ok": False,
            "stage": "ssh",
            "message": "Connection test produced no result.",
            "hint": None,
        }

    def _attempt_room_connection(
        self, spec: dict[str, Any], cfg: dict[str, Any] | None, meta: dict[str, Any]
    ) -> dict[str, Any]:
        """One isolated room-connect attempt (blocking) → a classified result.
        Runs the supervisor's transport: connect+auth (``ssh`` stage), open the
        tunnel / exec relay (``channel``), and for exec confirm the relay didn't
        immediately exit (``room``)."""
        from .executor import _make_ssh_client

        via = str(spec.get("via") or "tunnel")
        client = None
        chan = None
        try:
            try:
                client = _make_ssh_client(
                    str(spec["ssh_target"]), spec.get("identity_file") or None, cfg
                )
            except Exception as exc:  # noqa: BLE001 — report, don't raise
                return {**_classify_room_connection(exc, via=via), **meta}

            transport = client.get_transport()
            if transport is None:
                return {
                    **_classify_room_connection(OSError("transport closed"), via=via),
                    **meta,
                }
            try:
                if via == "exec":
                    chan = transport.open_session()
                    chan.exec_command(str(spec["exec_command"]))
                else:
                    chan = transport.open_channel(
                        "direct-tcpip",
                        (str(spec.get("dest_host") or "127.0.0.1"), int(spec["port"])),
                        ("127.0.0.1", 0),
                    )
            except Exception as exc:  # noqa: BLE001
                return {**_classify_room_connection(exc, via=via), **meta}

            # Exec: the relay (console-room connect / nc) connects to localhost:port
            # on the host. If the room server is down it exits straight away — catch
            # that so "host reachable but room down" isn't reported as success.
            if via == "exec":
                time.sleep(1.5)
                if chan.exit_status_ready():
                    err = ""
                    try:
                        if chan.recv_stderr_ready():
                            err = (
                                chan.recv_stderr(2048)
                                .decode("utf-8", "replace")
                                .strip()
                            )
                    except Exception:
                        pass
                    return {
                        **meta,
                        "ok": False,
                        "stage": "room",
                        "message": (
                            f"Reached the host, but the relay exited — the room "
                            f"server isn't answering on port {spec['port']} "
                            f"(is console-room running there?)."
                            + (f" [{err}]" if err else "")
                        ),
                        "hint": "Start the room server on the host (console-room) and confirm its --ndjson-port matches this host's room_port.",
                    }

            return {
                **meta,
                "ok": True,
                "stage": "ok",
                "message": (
                    f"Connected to the room on {meta['host']} (port {spec['port']}, via {via})."
                    + (f" Queue {spec['queue']}." if spec.get("queue") else "")
                ),
                "hint": None,
            }
        finally:
            try:
                if chan is not None:
                    chan.close()
            except Exception:
                pass
            try:
                if client is not None:
                    client.close()
            except Exception:
                pass

    def _abort_turns(self, ids: set[str] | None) -> None:
        """Unblock in-flight turns: signal each cancel flag and wake each pending
        autonomy-gate confirm (as cancelled). ``ids=None`` aborts everything
        (shutdown); otherwise only turns whose chat_id is in ``ids``. Thread-safe
        and idempotent."""
        with self._lock:
            cancels = [
                ev
                for cid, ev in self._chat_cancels.items()
                if ids is None or cid in ids
            ]
            pending = [
                t for t in self._pending_confirms.values() if ids is None or t[0] in ids
            ]
        for ev in cancels:
            ev.set()
        for _chat_id, event, holder in pending:
            holder["cancelled"] = True
            event.set()

    def _abort_all_turns(self, *_args: Any) -> None:
        """Abort *every* in-flight turn — the window-close / atexit handler, so a
        turn parked at the gate can't hang shutdown."""
        self._abort_turns(None)

    def restart_triggers(self) -> None:
        """Reload trigger config and restart the engine (after a config change)."""
        self.stop_triggers()
        self.start_triggers()

    def list_triggers(self) -> dict[str, Any]:
        """For the Triggers UI: raw trigger tables, parse errors, and the event
        sources available on this machine (with the reason any are unavailable)."""
        from .config import read_rota, read_triggers, trigger_file_error
        from .events import available_sources, create_source
        from .triggers import load_triggers

        raw = read_triggers()
        _, errors = load_triggers(raw)
        # A whole-file TOML error rejects every trigger silently — surface it.
        file_error = trigger_file_error()
        if file_error:
            errors = [file_error, *errors]
        sources_info: list[dict[str, Any]] = []
        for sid in available_sources():
            src = create_source(sid)
            ok, reason = src.available() if src is not None else (False, "unknown")
            sources_info.append(
                {
                    "id": sid,
                    "available": ok,
                    "reason": reason,
                    "event_types": src.event_types() if src is not None else [],
                }
            )
        return {
            "triggers": raw,
            "errors": errors,
            "sources": sources_info,
            "rota": read_rota(),
            "running": self._trigger_engine is not None,
        }

    def set_trigger_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        """Toggle a trigger's enabled flag in config and restart the engine."""
        from .config import read_triggers, write_triggers

        raw = read_triggers()
        found = False
        for trig in raw:
            if trig.get("name") == name:
                trig["enabled"] = bool(enabled)
                found = True
        if found:
            write_triggers(raw)
            self.restart_triggers()
        return {"ok": found}

    def save_rota(self, rota: dict[str, Any]) -> dict[str, Any]:
        """Save the working-hours rota and restart the engine so it takes effect.
        Also re-join the room so chat presence follows the new schedule."""
        from .config import write_rota

        write_rota(dict(rota or {}))
        self.restart_triggers()
        self.restart_chat()
        return {"ok": True}

    def get_trigger_board(self) -> list[dict[str, Any]]:
        """Snapshot of the kanban cards for this session (newest last)."""
        with self._lock:
            cards = list(self._trigger_cards.values())
        return sorted(cards, key=lambda c: c.get("created_at", 0.0))

    def get_pending_confirms(self) -> list[dict[str, Any]]:
        """Snapshot of the autonomy-gate confirms currently parked (oldest first).

        The cold-start seed for the work inbox: a tool call can be waiting on the
        gate before the inbox view mounts (it only hears the live
        ``runspec:tool_confirm`` for gates raised *after* it subscribes). Each row
        mirrors that event's shape (``id`` = the chat/card id, ``request_id``,
        ``tool_name``, ``tool_input``, ``autonomy``) plus ``created_at`` for
        ordering. The frontend folds a confirm whose ``id`` matches a live trigger
        card into that card (same id-match the tab badge uses)."""
        with self._lock:
            meta = list(self._pending_confirm_meta.values())
        return sorted(meta, key=lambda m: m.get("created_at", 0.0))

    def get_trigger_events(self) -> list[dict[str, Any]]:
        """The recent event-audit feed (every event seen + its disposition)."""
        with self._lock:
            return list(self._trigger_events)

    def _on_trigger_event(self, record: dict[str, Any]) -> None:
        """Engine hook: record an event's disposition (matched / skipped / no
        match) and push it to the UI audit feed. May be called from a source
        thread."""
        with self._lock:
            self._trigger_events.append(record)
            # Bounded ring — keep the most recent.
            if len(self._trigger_events) > 300:
                del self._trigger_events[: len(self._trigger_events) - 300]
        self._dispatch("runspec:trigger_event", record)

    def _on_trigger_card(self, card: Any) -> None:
        """Engine hook: record a card update and push it to the UI. May be called
        from a source / worker thread."""
        detail = card.to_dict()
        with self._lock:
            self._trigger_cards[detail["card_id"]] = detail
            # Bound the board so a long session can't grow without limit.
            if len(self._trigger_cards) > 200:
                oldest = sorted(
                    self._trigger_cards.values(),
                    key=lambda c: c.get("created_at", 0.0),
                )[:50]
                for old in oldest:
                    self._trigger_cards.pop(old["card_id"], None)
        self._dispatch("runspec:trigger_card", detail)

    def _trigger_run_agent_turn(
        self,
        chat_id: str,
        system: str,
        message: str,
        *,
        draft: bool,
        reply_to: str | None = None,
        room: str | None = None,
        from_mailbox: str | None = None,
        allowed_runnables: list[str] | None = None,
        record: Callable[[str], None] | None = None,
        stage: Callable[[str], None] | None = None,
    ) -> str:
        """BridgeOps: run an isolated headless agent turn for a trigger card.

        Runs on the engine's worker thread, so it owns its own asyncio loop
        (mirroring send_chat). The card id doubles as the chat_id so the turn's
        token / tool / confirm events attribute to the card on the board.
        ``reply_to`` (when ``draft``) is the Outlook message id the draft_reply
        tool replies to; ``room`` (when ``draft``) is the chat room the
        propose_reply tool drafts into; ``from_mailbox`` (when ``draft``) is the
        shared mailbox a help-desk reply drafts *as*. ``allowed_runnables`` (when
        non-empty) scopes the agent's runnable tools to that allowlist. ``record``
        logs per-tool steps to the card timeline. Returns a summary for the
        card."""
        try:
            adapter = self._get_adapter(system_override=system)
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the worker
            return f"⚠ Trigger could not run: {exc}"
        if adapter is None:
            return (
                "⚠ No LLM provider configured — the trigger matched, but agent mode "
                "needs a provider + API key in Settings to run."
            )
        cancel = threading.Event()
        with self._lock:
            self._chat_cancels[chat_id] = cancel
        try:
            return asyncio.run(
                self._run_isolated_agent_turn(
                    chat_id,
                    message,
                    adapter,
                    reply_to=reply_to if draft else None,
                    room=room if draft else None,
                    from_mailbox=from_mailbox if draft else None,
                    allowed_runnables=allowed_runnables,
                    record=record,
                    stage=stage,
                )
            )
        finally:
            with self._lock:
                self._chat_cancels.pop(chat_id, None)

    async def _run_isolated_agent_turn(
        self,
        chat_id: str,
        message: str,
        adapter: Any,
        reply_to: str | None = None,
        room: str | None = None,
        from_mailbox: str | None = None,
        allowed_runnables: list[str] | None = None,
        record: Callable[[str], None] | None = None,
        stage: Callable[[str], None] | None = None,
    ) -> str:
        """A headless agent loop for a trigger card. Like _agentic_chat_turn but
        against an isolated, throwaway history (never the shared chat) so a
        trigger never pollutes the user's conversation. Same toolset and the same
        autonomy gate (_gated_run_tool_async). When ``reply_to`` is set the
        built-in draft_reply tool is offered; when ``room`` is set the chat
        propose_reply tool is offered (both handled inline).

        Returns a short summary (the model's closing reply + any tool actions)
        for display on the kanban card — trigger turns have no chat transcript,
        so this is where the agent's output becomes visible."""
        from .trigger_runner import draft_reply_schema, propose_reply_schema

        start = time.monotonic()
        tools = self._runnables_to_tools(allow=allowed_runnables)
        if allowed_runnables and not tools:
            # A non-empty allowlist that matched no installed runnable — the agent
            # would run tool-less. Surface it (a config typo / a host not yet
            # discovered) rather than silently doing nothing.
            logger.warning(
                "trigger agent turn: runnables allowlist %s matched no runnable",
                allowed_runnables,
            )
        if reply_to:
            tools = [*tools, draft_reply_schema()]
        if room:
            tools = [*tools, propose_reply_schema()]
        history: list[dict[str, Any]] = [{"role": "user", "content": message}]
        with self._lock:
            cancel = self._chat_cancels.get(chat_id) or threading.Event()
        stopped = False
        final_text = ""  # the model's last assistant message (its conclusion)
        actions: list[str] = []  # one line per tool the agent ran

        for _ in range(10):  # bound agentic iterations, same as the chat loop
            if cancel.is_set():
                stopped = True
                break
            response: Any = None
            assistant_text = ""
            try:
                async for event in adapter.stream_with_tools(history, tools):
                    if cancel.is_set():
                        stopped = True
                        break
                    if event[0] == "text":
                        assistant_text += event[1]
                        self._dispatch(
                            "runspec:token", {"id": chat_id, "token": event[1]}
                        )
                    elif event[0] == "done":
                        response = event[1]
            except Exception as exc:  # noqa: BLE001
                self._dispatch(
                    "runspec:token", {"id": chat_id, "token": f"\n⚠ Error: {exc}"}
                )
                final_text = f"⚠ Error: {exc}"
                break
            if stopped:
                break
            if (
                response is None
                or response.stop_reason != "tool_use"
                or not response.tool_calls
            ):
                final_text = assistant_text  # closing reply, no further tools
                break
            if cancel.is_set():
                stopped = True
                break
            tool_results: list[tuple[Any, str]] = []
            for tc in response.tool_calls:
                self._dispatch(
                    "runspec:tool_start",
                    {"id": chat_id, "tool_name": tc.name, "tool_input": tc.input},
                )
                if cancel.is_set():
                    output = f"⏹ '{tc.name.split('__', 1)[-1]}' was not run — stopped."
                elif tc.name == "draft_reply":
                    output = await self._gated_draft_reply(
                        tc.input, chat_id, reply_to, from_mailbox, stage=stage
                    )
                elif tc.name == "propose_reply":
                    output = await self._gated_propose_reply(
                        tc.input, chat_id, room, stage=stage
                    )
                else:
                    output = await self._gated_run_tool_async(
                        tc.name, tc.input, chat_id, stage=stage
                    )
                self._dispatch(
                    "runspec:tool_end",
                    {"id": chat_id, "tool_name": tc.name, "output": output[:2000]},
                )
                display = tc.name.split("__", 1)[-1]
                first_line = next((ln for ln in output.splitlines() if ln.strip()), "")
                actions.append(f"{display} → {first_line[:160]}")
                if record is not None:
                    record(f"{display} → {first_line[:160]}")
                tool_results.append((tc, output))
            history.extend(adapter.make_tool_turn(response, tool_results))

        duration_ms = int((time.monotonic() - start) * 1000)
        if stopped:
            self._dispatch("runspec:token", {"id": chat_id, "token": "\n⏹ Stopped."})
        self._dispatch(
            "runspec:run_end",
            {"id": chat_id, "exit_code": 0, "duration_ms": duration_ms},
        )

        parts: list[str] = []
        if actions:
            parts.append("\n".join(f"• {a}" for a in actions))
        if final_text.strip():
            parts.append(final_text.strip())
        if stopped:
            parts.append("⏹ Stopped.")
        return "\n\n".join(parts)[:4000]

    async def _gated_draft_reply(
        self,
        tool_input: dict[str, Any],
        chat_id: str,
        reply_to: str | None,
        from_mailbox: str | None = None,
        stage: Callable[[str], None] | None = None,
    ) -> str:
        """Confirm, then save an agent-written reply to Outlook Drafts.

        Always gated (a mailbox write), reusing the same confirm prompt as every
        other tool call so the card parks in 'awaiting human' until approved. The
        draft is saved, never sent. ``from_mailbox`` (a help-desk queue) drafts
        the reply *as the shared mailbox* — the queue identity — matching the
        human reply path; empty/None drafts from the operator."""
        body = str((tool_input or {}).get("body") or "").strip()
        if not reply_to:
            return "✗ draft_reply: no source message to reply to."
        if not body:
            return "✗ draft_reply: empty body — nothing to draft."
        if stage is not None:
            stage("awaiting_human")
        request_id, event, holder = self._register_confirm(
            chat_id, "draft_reply", tool_input, "confirm"
        )
        timed_out = not await asyncio.to_thread(event.wait, self._CONFIRM_TIMEOUT_S)
        approved = self._finish_confirm(request_id, holder, timed_out)
        if holder.get("cancelled"):
            return "⏹ draft_reply was not run — you stopped the assistant."
        if not approved:
            # Declined, but the turn continues — surface that as 'running' rather
            # than leaving the card stuck in 'awaiting_human'.
            if stage is not None:
                stage("running")
            return "✗ You declined to save the draft reply."
        if stage is not None:
            stage("running")
        result = await asyncio.to_thread(
            self._save_reply_draft, reply_to, body, from_mailbox
        )
        # Hand the saved draft to the card so it can offer a Send button — the
        # human reviews the draft and sends it from the card (never the agent).
        if result.get("ok") and result.get("entry_id"):
            self._dispatch(
                "runspec:trigger_draft",
                {
                    "id": chat_id,
                    "entry_id": result["entry_id"],
                    "to": result.get("to", ""),
                    "subject": result.get("subject", ""),
                },
            )
        return str(result.get("message", ""))

    async def _gated_propose_reply(
        self,
        tool_input: dict[str, Any],
        chat_id: str,
        room: str | None,
        stage: Callable[[str], None] | None = None,
    ) -> str:
        """Confirm, then surface an agent-proposed reply into the room composer.

        The chat 'speak' checkpoint (design §2): nothing reaches the room
        automatically. Gated with the same confirm prompt as every other tool, so
        the card parks in 'awaiting human' until approved. On approval the text
        only pre-fills the composer (runspec:room_draft) — the human reviews,
        edits, and sends it (send_room_message). The agent never sends."""
        text = str((tool_input or {}).get("text") or "").strip()
        if not room:
            return "✗ propose_reply: no room to reply to."
        if not text:
            return "✗ propose_reply: empty text — nothing to propose."
        if stage is not None:
            stage("awaiting_human")
        request_id, event, holder = self._register_confirm(
            chat_id, "propose_reply", tool_input, "confirm"
        )
        timed_out = not await asyncio.to_thread(event.wait, self._CONFIRM_TIMEOUT_S)
        approved = self._finish_confirm(request_id, holder, timed_out)
        if holder.get("cancelled"):
            return "⏹ propose_reply was not run — you stopped the assistant."
        if not approved:
            # Declined, but the turn continues — surface that as 'running' rather
            # than leaving the card stuck in 'awaiting_human'.
            if stage is not None:
                stage("running")
            return "✗ You declined to propose the reply."
        if stage is not None:
            stage("running")
        # Pre-fill the room composer; the human reviews and sends from the Rooms
        # view (send_room_message). Nothing goes on the wire here.
        self._dispatch(
            "runspec:room_draft", {"id": chat_id, "room": room, "text": text}
        )
        return (
            f"✓ Proposed a reply into the room composer for {room}. Review it in "
            "the Rooms view, then edit and send it — nothing was sent."
        )

    def _save_reply_draft(
        self, entry_id: str, body: str, from_mailbox: str | None = None
    ) -> dict[str, Any]:
        """Blocking Outlook-COM draft save. Returns ``{ok, message, entry_id, to,
        subject}`` — the ``message`` goes back to the agent; the rest lets the
        card offer a Send button for the saved draft. ``from_mailbox`` drafts the
        reply *as that shared mailbox* (the help-desk queue identity)."""
        try:
            from .sources.outlook import save_reply_draft

            info = save_reply_draft(entry_id, body, from_mailbox=from_mailbox)
        except Exception as exc:  # noqa: BLE001 — report on the card, don't crash
            logger.exception("draft_reply: save failed")
            return {"ok": False, "message": f"✗ Could not save draft: {exc}"}
        to = info.get("to") or "(recipients from original)"
        return {
            "ok": True,
            "message": (
                f"✓ Saved a draft reply to {to} in Outlook Drafts. Review it on "
                "the card and click Send, or send it from Outlook."
            ),
            "entry_id": info.get("entry_id", ""),
            "to": info.get("to", ""),
            "subject": info.get("subject", ""),
        }

    def send_trigger_draft(self, entry_id: str) -> dict[str, Any]:
        """Send a saved draft reply — the card's Send button (human-initiated).

        The agent never sends mail; this is reached only from an explicit click,
        so it isn't gated through the autonomy prompt. Returns ``{ok, to,
        subject}`` or ``{ok: False, error}``."""
        if not entry_id:
            return {"ok": False, "error": "no draft to send"}
        try:
            from .sources.outlook import send_draft

            info = send_draft(entry_id)
        except Exception as exc:  # noqa: BLE001 — report to the UI, don't crash
            logger.exception("send_trigger_draft failed")
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "to": info.get("to", ""),
            "subject": info.get("subject", ""),
        }

    def _dispatch(self, event: str, detail: dict[str, Any]) -> None:
        # Enqueue; the single ui-dispatch thread delivers it (see __init__).
        # Non-blocking, so a caller on the COM pump / a trigger worker never
        # stalls on evaluate_js and concurrent callers can't race the webview.
        self._dispatch_q.put((event, detail))

    def _ui_dispatch_loop(self) -> None:
        """Drain the dispatch queue, delivering each event to the web view via a
        single thread (serialised, in order). Started in __init__; skips while no
        window is attached (matching the old drop-pre-window behaviour)."""
        while True:
            item = self._dispatch_q.get()
            if item is None:
                return  # shutdown sentinel
            event, detail = item
            window = self._window
            if window is None:
                logger.debug("dispatch %s skipped: no window attached", event)
                continue
            try:
                payload = json.dumps(detail)
                js = (
                    "window.dispatchEvent(new CustomEvent("
                    f"{json.dumps(event)},{{detail:{payload}}}))"
                )
                window.evaluate_js(js)
            except Exception:
                # Previously swallowed — that hid event-delivery failures (e.g.
                # the autonomy confirm prompt) and left nothing to diagnose.
                logger.exception("dispatch %s failed (evaluate_js raised)", event)

    @staticmethod
    def _current_user() -> str:
        try:
            import win32api  # type: ignore[import]

            # NameDisplay (3) returns the full display name, e.g. "Jason Finestone"
            return win32api.GetUserNameEx(3)  # type: ignore[attr-defined]
        except Exception:
            import os

            return os.environ.get("USERNAME", "user")


# ── helpers ───────────────────────────────────────────────────────────────────


def _normalize_path(p: str) -> str:
    """Normalise a filesystem path to forward slashes for TOML storage.

    Browse buttons return native Windows paths with backslashes, which the
    custom TOML serialiser must escape (``\\\\``) — those escaped paths are
    valid but ugly. Forward slashes work fine for Windows file APIs and avoid
    escaping entirely. Empty / non-string inputs are passed through unchanged.
    """
    if not isinstance(p, str) or not p:
        return p
    return p.replace("\\", "/")


def _normalize_ssh_section(ssh: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an [ssh] config section with path fields normalised."""
    out = dict(ssh)
    for k in ("binary", "identityFile"):
        if isinstance(out.get(k), str):
            out[k] = _normalize_path(out[k])
    return out


PUTTY_PROXY_SESSION = "runspec-console-proxy"


def _ensure_putty_proxy_session(
    proxy: str, session: str = PUTTY_PROXY_SESSION
) -> str | None:
    """Write a PuTTY saved session carrying an HTTP proxy; return its name.

    PuTTY has no command-line proxy flag — proxy config lives in a saved session
    (Windows registry). We maintain a dedicated session holding only the proxy
    settings; `putty -load <session> -ssh -l user host` then applies the proxy
    while host/user/key still come from the command line.

    Returns the session name, or None if the proxy URL is unusable or winreg is
    unavailable (non-Windows). No proxy auth — front an auth proxy with a local
    bridge (cntlm/px) and point at http://localhost:<port>.
    """
    from urllib.parse import urlparse

    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    phost = parsed.hostname
    pport = parsed.port or 8080
    if not phost:
        return None
    try:
        import winreg  # Windows-only
    except ImportError:
        return None

    key_path = rf"Software\SimonTatham\PuTTY\Sessions\{session}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "ProxyMethod", 0, winreg.REG_DWORD, 3)  # 3 = HTTP
        winreg.SetValueEx(key, "ProxyHost", 0, winreg.REG_SZ, phost)
        winreg.SetValueEx(key, "ProxyPort", 0, winreg.REG_DWORD, pport)
        winreg.SetValueEx(key, "ProxyDNS", 0, winreg.REG_DWORD, 1)  # 1 = Auto
        winreg.SetValueEx(key, "ProxyLocalhost", 0, winreg.REG_DWORD, 0)
        winreg.SetValueEx(key, "ProxyUsername", 0, winreg.REG_SZ, "")
        winreg.SetValueEx(key, "ProxyPassword", 0, winreg.REG_SZ, "")
    return session


def _public_key_from_private(path: Path) -> str:
    """Derive the OpenSSH public-key line from an OpenSSH private key file."""
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_ssh_private_key(path.read_bytes(), password=None)
    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
        .strip()
    )


def _find_putty_exe(name: str = "putty.exe") -> Path | None:
    """Locate a PuTTY-suite executable (putty.exe, plink.exe, puttygen.exe, …).

    Checks common Windows install locations then falls back to PATH.
    Returns the resolved Path or None if not found.
    """
    import shutil

    for install_root in (
        Path(r"C:\Program Files\PuTTY"),
        Path(r"C:\Program Files (x86)\PuTTY"),
    ):
        candidate = install_root / name
        if candidate.exists():
            return candidate

    found = shutil.which(name)
    if found:
        return Path(found)
    return None


def _paths(entry: dict[str, Any]) -> list[str]:
    """Normalize runspec_paths (list) or legacy runspec_path (str) to a list."""
    paths = entry.get("runspec_paths")
    if paths and isinstance(paths, list):
        return [str(p) for p in paths if p]
    rp = entry.get("runspec_path", "")
    return [str(rp)] if rp else []


def _log_globs(runnable: str | None) -> list[str]:
    """Filename patterns to read for a runnable (or all of them).

    Matches the legacy single file (``{runnable}.log``), per-invocation files
    (``{runnable}.{ts}.{run_id}.log``, store="per-run"), and compacted archives
    (``{runnable}.archive.*.log.gz``). Discovery only — the runnable a record
    belongs to is read from the record body (``extra.runnable``), not the
    filename, so over-matching is harmless. ``{runnable}.log`` is listed
    separately because ``{runnable}.*.log`` does not match it.
    """
    if runnable:
        return [f"{runnable}.*.log", f"{runnable}.log", f"{runnable}.*.log.gz"]
    return ["*.log", "*.log.gz"]


def _remote_log_script(log_dir: str | list[str], runnable: str | None) -> str:
    """Shell snippet that frames and streams every matching log file over SSH.

    ``log_dir`` may be a single directory or several (e.g. each venv's ``logs``
    plus ``~/logs``); all are scanned. Each file is preceded by a
    ``\\x00RUNSPEC_LOG:<basename>`` frame (the name is only a fallback —
    attribution comes from the record body). Compacted ``.gz`` archives are
    decompressed with ``gzip -dc``; plain files are ``cat``-ed. Non-matching
    globs expand to themselves and are skipped by the ``[ -f ]`` guard, so no
    ``nullglob`` is required.
    """
    dirs = [log_dir] if isinstance(log_dir, str) else list(log_dir)
    patterns = " ".join(f"{d}/{g}" for d in dirs for g in _log_globs(runnable))
    return (
        f"for f in {patterns}; do "
        f'[ -f "$f" ] || continue; '
        f'printf "\\x00RUNSPEC_LOG:%s\\n" "$(basename "$f" .log)"; '
        f'case "$f" in *.gz) gzip -dc "$f";; *) cat "$f";; esac; '
        f"done 2>/dev/null"
    )


def _write_ppk_v2(
    dest: Path, raw_priv: bytes, raw_pub: bytes, comment: str = "runspec-console"
) -> None:
    """Write a PuTTY PPK v2 file for an ed25519 key pair (no passphrase).

    Generates a PPK v2 file compatible with all PuTTY versions, so launch_terminal
    works even on PuTTY < 0.75 which cannot read OpenSSH new-format keys.
    """
    import base64
    import hashlib
    import hmac as _hmac
    import struct
    import textwrap

    key_type = b"ssh-ed25519"
    enc_type = b"none"
    comment_b = comment.encode()

    # SSH wire-format public blob
    pub_blob = (
        struct.pack(">I", len(key_type))
        + key_type
        + struct.pack(">I", len(raw_pub))
        + raw_pub
    )
    # PPK private blob: seed then public key, each length-prefixed
    priv_blob = (
        struct.pack(">I", len(raw_priv))
        + raw_priv
        + struct.pack(">I", len(raw_pub))
        + raw_pub
    )

    # MAC key = SHA1("putty-private-key-file-mac-key")
    mac_key = hashlib.sha1(b"putty-private-key-file-mac-key").digest()
    mac_data = b"".join(
        struct.pack(">I", len(f)) + f
        for f in (key_type, enc_type, comment_b, pub_blob, priv_blob)
    )
    mac_hex = _hmac.new(mac_key, mac_data, hashlib.sha1).hexdigest()

    def b64lines(data: bytes) -> tuple[int, str]:
        lines = textwrap.wrap(base64.b64encode(data).decode(), 64)
        return len(lines), "\n".join(lines)

    pub_n, pub_s = b64lines(pub_blob)
    priv_n, priv_s = b64lines(priv_blob)

    dest.write_text(
        "\n".join(
            [
                "PuTTY-User-Key-File-2: ssh-ed25519",
                "Encryption: none",
                f"Comment: {comment}",
                f"Public-Lines: {pub_n}",
                pub_s,
                f"Private-Lines: {priv_n}",
                priv_s,
                f"Private-MAC: {mac_hex}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _parse_ssh(ssh: str) -> tuple[str, str, int | None]:
    """Parse 'user@host:port' → (user, host, port). Missing parts → empty string / None."""
    user = ""
    port: int | None = None
    s = ssh
    if "@" in s:
        user, s = s.split("@", 1)
    if ":" in s:
        hostname, port_str = s.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            hostname = s
    else:
        hostname = s
    return user, hostname, port


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _summary_extra(extra: dict[str, Any]) -> dict[str, Any]:
    """Pull the rich run_summary fields the Analytics tab needs.

    These are dropped from the lean HistoryRecord shape (used by the History
    tab) and only attached when ``include_extra=True`` is requested.
    """
    return {
        "autonomy": extra.get("autonomy"),
        "agent": bool(extra.get("agent")),
        "exception": extra.get("exception"),
        "events": extra.get("events", {}),
        "command_path": extra.get("command_path", []),
        "user_target": extra.get("user_target"),
        "invocation_args": extra.get("invocation_args", {}),
    }


def _parse_log(
    log_file: Path, host: str, include_extra: bool = False
) -> list[dict[str, Any]]:
    try:
        if log_file.suffix == ".gz":
            with gzip.open(log_file, "rt", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        else:
            text = log_file.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    # log_file.stem is only a fallback runnable name — attribution comes from
    # extra.runnable in the records (see _parse_log_by_run_id). This keeps the
    # parser correct for per-invocation filenames whose stem isn't the runnable.
    return _parse_log_text(log_file.stem, text, host, include_extra=include_extra)


def _parse_log_text(
    name: str, text: str, host: str, include_extra: bool = False
) -> list[dict[str, Any]]:
    """Parse a log file's text into HistoryRecord dicts.

    When run_id is present in records (runspec >=0.18) each invocation is
    isolated by its UUID — multi-user interleaving is handled cleanly.
    Older logs without run_id fall back to sequential accumulation between
    run_summary markers.

    When ``include_extra`` is set, each record gains a nested ``extra`` dict
    carrying the rich run_summary fields (autonomy, events, exception, …) the
    Analytics tab aggregates over. The default keeps the lean History shape.
    """
    entries: list[dict[str, Any]] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            entries.append(json.loads(raw))
        except json.JSONDecodeError:
            continue

    if not entries:
        return []

    # Detect run_id presence — check first record that has extra.run_id
    has_run_id = any(e.get("extra", {}).get("run_id") for e in entries)
    if has_run_id:
        return _parse_log_by_run_id(name, entries, host, include_extra=include_extra)
    return _parse_log_sequential(name, entries, host, include_extra=include_extra)


def _parse_log_by_run_id(
    name: str, entries: list[dict[str, Any]], host: str, include_extra: bool = False
) -> list[dict[str, Any]]:
    """Group log entries by run_id UUID → one HistoryRecord per invocation."""
    # Preserve insertion order of run_ids so history is chronological
    groups: dict[str, dict[str, Any]] = {}  # run_id → {lines, summary, exc}
    for entry in entries:
        extra = entry.get("extra", {})
        run_id = extra.get("run_id")
        if not run_id:
            continue
        g = groups.setdefault(run_id, {"lines": [], "summary": None, "exc": None})
        if entry.get("exc_structured"):
            # Uncaught-exception record (runspec >=0.26): lift the structured
            # frames out, don't surface it as a generic CRITICAL log line.
            g["exc"] = entry["exc_structured"]
        elif extra.get("event") == "run_summary":
            g["summary"] = entry
        else:
            g["lines"].append(
                {
                    "ts": entry.get("ts", ""),
                    "level": entry.get("level", "INFO"),
                    "message": entry.get("message", ""),
                }
            )

    records: list[dict[str, Any]] = []
    for run_id, g in groups.items():
        summary_entry = g["summary"]
        if summary_entry is None:
            continue  # in-progress run — no summary yet
        extra = summary_entry.get("extra", {})
        # Attribution comes from the record, not the filename — robust to
        # per-invocation filenames; `name` (filename stem) is only a fallback.
        runnable = extra.get("runnable") or name
        ts_raw = summary_entry.get("ts", "")
        stable_id = hashlib.md5((run_id + runnable).encode()).hexdigest()[:12]
        record = {
            "id": stable_id,
            "runnable": runnable,
            "group": "",
            "host": host,
            "operator": extra.get("user", ""),
            "runAs": extra.get("user_target") or "",
            "exitCode": extra.get("exit_code", 0),
            "durationMs": extra.get("duration_ms", 0),
            "ts": ts_raw,
            "args": extra.get("args", {}),
            "argSources": extra.get("arg_sources", {}),
            "logLines": g["lines"],
            "initiatedBy": "llm" if extra.get("agent") else "user",
        }
        exc = g["exc"]
        if exc:
            # Richer than the summary's extra.exception (type/message/traceback):
            # carries the structured, table-ready call frames from #102.
            record["exception"] = {
                "type": exc.get("type"),
                "message": exc.get("message"),
                "module": exc.get("module"),
                "frames": exc.get("frames", []),
            }
        if include_extra:
            record["extra"] = _summary_extra(extra)
        records.append(record)
    return records


def _parse_log_sequential(
    name: str, entries: list[dict[str, Any]], host: str, include_extra: bool = False
) -> list[dict[str, Any]]:
    """Legacy parser for logs without run_id (runspec <0.18)."""
    records: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    for entry in entries:
        extra = entry.get("extra", {})
        if extra.get("event") == "run_summary":
            ts_raw = entry.get("ts", "")
            runnable = extra.get("runnable") or name
            stable_id = hashlib.md5((ts_raw + runnable).encode()).hexdigest()[:12]
            record = {
                "id": stable_id,
                "runnable": runnable,
                "group": "",
                "host": host,
                "operator": extra.get("user", ""),
                "runAs": extra.get("user_target") or "",
                "exitCode": extra.get("exit_code", 0),
                "durationMs": extra.get("duration_ms", 0),
                "ts": ts_raw,
                "args": extra.get("args", {}),
                "argSources": extra.get("arg_sources", {}),
                "logLines": lines,
                "initiatedBy": "llm" if extra.get("agent") else "user",
            }
            if include_extra:
                record["extra"] = _summary_extra(extra)
            records.append(record)
            lines = []
        else:
            lines.append(
                {
                    "ts": entry.get("ts", ""),
                    "level": entry.get("level", "INFO"),
                    "message": entry.get("message", ""),
                }
            )
    return records


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolated percentile over a pre-sorted list. Empty → 0."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac)


_EVENT_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _aggregate_analytics(
    records: list[dict[str, Any]],
    since: str,
    until: str,
    partial: bool = False,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Fold parsed run_summary records into the Analytics data contract."""
    from datetime import date, timedelta

    daily: dict[str, dict[str, Any]] = {}
    buckets: dict[tuple, dict[str, Any]] = {}
    exceptions: dict[tuple, dict[str, Any]] = {}
    hosts_seen: set[str] = set()
    groups_seen: set[str] = set()
    runnables_seen: set[str] = set()
    operators_seen: set[str] = set()
    all_durations: list[float] = []
    total = success = failure = 0

    for r in records:
        day = r.get("ts", "")[:10]
        if not day:
            continue
        ok = r.get("exitCode", 0) == 0
        dur = r.get("durationMs", 0) or 0
        host = r.get("host", "")
        group = r.get("group", "") or ""
        run = r.get("runnable", "")
        operator = r.get("operator", "") or ""
        initiated = r.get("initiatedBy", "user")
        extra = r.get("extra", {}) or {}
        autonomy = extra.get("autonomy") or ""

        total += 1
        success += 1 if ok else 0
        failure += 0 if ok else 1
        all_durations.append(dur)
        hosts_seen.add(host)
        if group:
            groups_seen.add(group)
        runnables_seen.add(run)
        if operator:
            operators_seen.add(operator)

        d = daily.setdefault(
            day, {"total": 0, "success": 0, "failure": 0, "durations": []}
        )
        d["total"] += 1
        d["success"] += 1 if ok else 0
        d["failure"] += 0 if ok else 1
        d["durations"].append(dur)

        bkey = (day, host, group, run, operator, initiated, autonomy)
        b = buckets.get(bkey)
        if b is None:
            b = {
                "date": day,
                "host": host,
                "group": group,
                "runnable": run,
                "operator": operator,
                "initiatedBy": initiated,
                "autonomy": autonomy,
                "total": 0,
                "success": 0,
                "failure": 0,
                "durationMsSum": 0,
                "events": {lvl: 0 for lvl in _EVENT_LEVELS},
            }
            buckets[bkey] = b
        b["total"] += 1
        b["success"] += 1 if ok else 0
        b["failure"] += 0 if ok else 1
        b["durationMsSum"] += dur
        for lvl, n in (extra.get("events") or {}).items():
            if lvl in b["events"]:
                b["events"][lvl] += n

        exc = extra.get("exception")
        if exc:
            msg = exc.get("message") if isinstance(exc, dict) else str(exc)
            etype = exc.get("type", "") if isinstance(exc, dict) else ""
            label = f"{etype}: {msg}" if etype else str(msg)
            ekey = (label, run, host)
            ex = exceptions.get(ekey)
            if ex is None:
                ex = {
                    "exception": label,
                    "runnable": run,
                    "host": host,
                    "count": 0,
                    "lastTs": "",
                }
                exceptions[ekey] = ex
            ex["count"] += 1
            if r.get("ts", "") > ex["lastTs"]:
                ex["lastTs"] = r.get("ts", "")

    # Zero-filled daily series across the whole window
    daily_series: list[dict[str, Any]] = []
    try:
        start = date.fromisoformat(since)
        end = date.fromisoformat(until)
    except ValueError:
        start = end = None
    if start and end and start <= end:
        cur = start
        while cur <= end:
            key = cur.isoformat()
            d = daily.get(key)
            if d:
                durs = sorted(d["durations"])
                daily_series.append(
                    {
                        "date": key,
                        "total": d["total"],
                        "success": d["success"],
                        "failure": d["failure"],
                        "p50DurationMs": round(_percentile(durs, 50)),
                        "p95DurationMs": round(_percentile(durs, 95)),
                    }
                )
            else:
                daily_series.append(
                    {
                        "date": key,
                        "total": 0,
                        "success": 0,
                        "failure": 0,
                        "p50DurationMs": 0,
                        "p95DurationMs": 0,
                    }
                )
            cur += timedelta(days=1)

    durs_sorted = sorted(all_durations)
    duration_stats = {
        "p50": round(_percentile(durs_sorted, 50)),
        "p95": round(_percentile(durs_sorted, 95)),
        "max": round(durs_sorted[-1]) if durs_sorted else 0,
        "mean": round(sum(durs_sorted) / len(durs_sorted)) if durs_sorted else 0,
    }

    exc_list = sorted(exceptions.values(), key=lambda e: e["count"], reverse=True)

    return {
        "since": since,
        "until": until,
        "totalRuns": total,
        "successCount": success,
        "failureCount": failure,
        "durationMs": duration_stats,
        "daily": daily_series,
        "buckets": list(buckets.values()),
        "exceptions": exc_list[:50],
        "dimensions": {
            "hosts": sorted(hosts_seen),
            "groups": sorted(groups_seen),
            "runnables": sorted(runnables_seen),
            "operators": sorted(operators_seen),
            "initiatedBy": ["user", "llm"],
        },
        "partial": partial,
        "errors": errors or [],
    }


def _toml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return f'"{v}"'
    return str(v)


def _write_schedules(path: Path, schedules: list[dict[str, Any]]) -> None:
    lines = []
    for s in schedules:
        lines.append("[[schedule]]")
        for k, v in s.items():
            if isinstance(v, dict):
                inner = ", ".join(f"{ik} = {_toml_scalar(iv)}" for ik, iv in v.items())
                lines.append(f"{k} = {{ {inner} }}")
            else:
                lines.append(f"{k} = {_toml_scalar(v)}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
