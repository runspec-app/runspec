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

import json
import queue
import threading
from datetime import datetime
from typing import Any

from .bridge_const import logger
from .bridge_helpers import _ParamikoDevHandler


from .bridge_infra import InfraMixin
from .bridge_hosts import HostsMixin
from .bridge_transfer import TransferMixin
from .bridge_reporting import ReportingMixin
from .bridge_schedules import SchedulesMixin
from .bridge_saved_runs import SavedRunsMixin
from .bridge_config_ssh import ConfigSshMixin
from .bridge_fleet import FleetMixin
from .bridge_invoke import InvokeMixin
from .bridge_agent_tools import AgentToolsMixin
from .bridge_autonomy import AutonomyMixin
from .bridge_config_tools import ConfigToolsMixin
from .bridge_search import SearchMixin
from .bridge_internals import InternalsMixin
from .bridge_triggers import TriggersMixin


class Bridge(
    InfraMixin,
    HostsMixin,
    TransferMixin,
    ReportingMixin,
    SchedulesMixin,
    SavedRunsMixin,
    ConfigSshMixin,
    FleetMixin,
    InvokeMixin,
    AgentToolsMixin,
    AutonomyMixin,
    ConfigToolsMixin,
    SearchMixin,
    InternalsMixin,
    TriggersMixin,
):
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
        # Serialises appends to the built-in-tool audit log (a single JSON line
        # per built-in agent tool call → ~/logs/runspec-console-tools.log, read
        # by History/Analytics). Kept separate from self._lock so an audit write
        # from a worker thread never contends with _in_flight bookkeeping.
        self._audit_lock = threading.Lock()
        # Cached ad-hoc SFTP connections for the relay source browser, keyed by
        # (jump, host, user, port) — so navigating directories on a slow link
        # handshakes the target once, then reuses the warm connection. Torn down
        # by close_adhoc() (relay dialog close) and on shutdown.
        self._adhoc_conns: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._adapter: Any = None  # ModelAdapter, loaded on demand
        # White-label identity derived from the active LLM plugin's distribution
        # (see branding.py). Memoised; invalidated in _after_config_change since a
        # provider switch in Settings changes the brand. The native window title
        # is set by app.py via set_window_title once branding is resolved there.
        self._branding: Any = None  # Branding | None
        self._window_title: str = "runspec console"
        # Event-trigger engine (built lazily in start_triggers) and the in-memory
        # run inbox: card_id → card.to_dict(). Cards are not persisted — a
        # missed event while the console was closed is intentionally not replayed.
        self._trigger_engine: Any = None
        self._trigger_cards: dict[str, dict[str, Any]] = {}
        # Audit feed: every event the engine sees + its disposition (bounded ring).
        self._trigger_events: list[dict[str, Any]] = []
        # Schedules mirror the trigger run inbox + event log with their own stores
        # (kept separate so schedule runs never leak into the Triggers tab/badge):
        # card_id → card.to_dict()-shaped run card, and a bounded event-audit ring.
        self._schedule_cards: dict[str, dict[str, Any]] = {}
        self._schedule_events: list[dict[str, Any]] = []
        # Chat rooms are a property of fleet host entries (room + room_port in
        # runspec_hosts.toml). The console joins each declared room always-on,
        # independent of triggers, via a reconnecting supervisor per room — using
        # that host's own ssh target + key so the tunnel reuses the pooled
        # connection. Keyed by room name: live ChatSource while connected.
        self._chat_sources: dict[str, Any] = {}
        # The bridge-owned schedule source. Built from the Schedules tab and
        # started alongside the engine in start_triggers; stopped in
        # stop_triggers. None when no schedule is configured.
        self._schedule_source: Any | None = None
        # Per-turn run provenance, keyed by chat_id (the card id for triggers).
        # The trigger handler registers {"trigger": <source>, "agent": <bool>}
        # before a rule/agent run so _run_tool_sync can tag the runnable's
        # run_summary (RUNSPEC_TRIGGER) and set the right agent flag. Absent for a
        # direct chat turn (→ agent run, no trigger). Guarded by self._lock.
        self._run_origin: dict[str, dict[str, Any]] = {}
        # Per-turn workspace id, keyed by chat_id. Trigger/schedule handlers
        # register e.g. {"trigger-<chat_id>"} before an isolated agent turn so
        # _run_tool_sync can spill big outputs into that session's own
        # workspace. Absent for a chat turn (→ falls back to _conversation_id,
        # the chat's persistent workspace). Guarded by self._lock.
        self._workspace_by_chat: dict[str, str] = {}
        self._chat_stop = threading.Event()
        self._chat_supervisors: dict[str, threading.Thread] = {}
        # Per-room last logged connect-failure summary, so the reconnect loop logs a
        # stopped/unreachable room server once — not a fresh line (or traceback)
        # every backoff. Cleared on a successful connect so a later failure re-logs.
        self._chat_connect_log: dict[str, str] = {}
        # Helpdesk agent mode (a host entry with room_queue). Agent namespaces
        # we're connected under → our display user there (the identity the hub
        # records as the assignee when it routes a request to us). Catalog index:
        # namespace → bare tool name → {host → discovered runnable}, so a portal
        # request the hub assigns us maps back to (host, runnable) — on the host the
        # requester picked when a fleet exposes the runnable on several — and enacts.
        self._agent_rooms: dict[str, str] = {}
        self._catalog_index: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
        # namespace → published runbook catalog name (``runbook:<id>``) → runbook id, so a
        # runbook request the hub assigns us maps back to the runbook to render + run.
        self._runbook_index: dict[str, dict[str, str]] = {}
        # namespace → last catalog actually published, so the periodic refresh
        # only re-sends (and logs) when the contents change. Cleared on disconnect
        # so the next subscribe republishes from scratch.
        self._published_catalogs: dict[str, list[dict[str, Any]]] = {}
        self._hosts: list[dict[str, Any]] = []
        # Named source-side stream filters (runspec_filters.toml). Loaded once at
        # startup and reloaded on save; resolved by name when a relay/download
        # selects one. See filters_store.py for the schema.
        self._filters: list[dict[str, Any]] = []
        # Named credentials (runspec_credentials.toml — metadata only; secret
        # values live in the OS keyring). Loaded at startup and on save; resolved
        # by id when a runnable/relay applies one. See credentials.py.
        self._credentials: list[dict[str, Any]] = []
        # tid → final transfer_end detail, so a synchronous caller (the agent tool)
        # can await a transfer's outcome. Bounded; oldest entries are evicted.
        self._recent_transfer_results: dict[str, dict[str, Any]] = {}
        self._connected_cache: dict[str, bool] = {}  # host name → last known state
        # Real remote log dirs per venv runspec path (asked of the remote, cached
        # for the session — they don't move). Avoids guessing the dir from the
        # binary path, which is wrong for pipx / ~/.local / system installs.
        self._remote_logdir_cache: dict[str, list[str]] = {}
        # History parse cache: log-file path str → (mtime, size, records). The
        # History tab re-reads on an interval; immutable files (compacted
        # archives, completed per-run files) are parsed once and reused, so a
        # re-poll only re-parses files whose (mtime, size) changed. Guarded by
        # _history_cache_lock (a dedicated lock so a slow parse never contends
        # with dispatch / _in_flight bookkeeping on self._lock).
        self._history_parse_cache: dict[
            str, tuple[float, int, list[dict[str, Any]]]
        ] = {}
        self._history_cache_lock = threading.Lock()
        self._runnables_cache: list[dict[str, Any]] = []
        # Fleet-discovery readiness, so the agent isn't "blind" on the first turn
        # after launch (search mode's catalog index is empty until discovery runs)
        # and the UI can show a "discovering N/M hosts…" indicator. ``_discovery_ready``
        # flips true once the first full refresh cycle finishes; ``_discovery_progress``
        # tracks (done, total) hosts of the current/most-recent cycle. Guarded by
        # ``self._lock`` alongside ``_runnables_cache``.
        self._discovery_ready = False
        self._discovery_progress: tuple[int, int] = (0, 0)
        # Persistent agent conversation so the model remembers prior turns. One
        # rolling conversation per app session; reset via clear_chat(). The lock
        # serialises turns so two quick sends can't interleave the history.
        self._chat_history: list[dict[str, Any]] = []
        self._chat_lock = threading.Lock()
        # The saved-conversation id the current rolling chat persists to (chat
        # history). Minted lazily on the first turn and re-minted by clear_chat();
        # set by resume_conversation when continuing a past chat. Distinct from the
        # per-turn chat_id (a UI correlation key). Guarded by self._chat_lock.
        self._conversation_id: str = ""
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
        # Rota "Take a break": an in-memory return time. While set, the rota gate
        # treats automation as outside-hours; it auto-resumes once `now` passes it
        # (the gate evaluates `now` live) and self-clears. Held only in memory so a
        # break is naturally transient (lost on restart, which is fine).
        self._break_until: datetime | None = None
        self._install_ssh_log_forwarder()
        # Apply any baseline config bundled in the deployment package before the
        # first load, so a fresh / air-gapped install is configured with no git
        # host or PyPI index reachable. A no-op unless the seed's version changed
        # (re-assert-on-upgrade); never fatal — a broken seed must not stop the
        # console starting. See docs/design/config-seed.md.
        try:
            self.seed_apply()
        except Exception:
            logger.warning("config seed apply at startup failed", exc_info=True)
        # Drop workspaces whose owning session (a chat conversation, an old
        # trigger/schedule turn) is gone — a cheap belt-and-braces on the
        # inline retention already run by conv.prune and delete_conversation.
        try:
            self._sweep_workspaces()
        except Exception:
            logger.warning("workspace orphan sweep failed", exc_info=True)
        self._reload_hosts()
        self._reload_filters()
        self._reload_credentials()
        import atexit

        atexit.register(self._close_ssh_pool)
        atexit.register(self.stop_chat)  # stop the room reconnect supervisor
        atexit.register(self.stop_triggers)  # quit any source pump threads cleanly
        # Unblock any in-flight turn parked at the autonomy gate so closing the
        # app can't hang waiting to join a worker stuck in event.wait(). Registered
        # last so it runs first (LIFO) — before concurrent.futures joins workers.
        atexit.register(self._abort_all_turns)

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

    def _sweep_workspaces(self) -> int:
        """Drop workspace directories whose owning session is gone.

        * Chat workspaces (dir name = a conversation id): removed if the
          conversation file no longer exists in ``conversations_dir()``.
          Symmetric with ``conv.prune`` — one entity, one lifetime.
        * Headless workspaces (``trigger-…`` / ``schedule-…``): removed when
          their mtime is older than ``[workspace] headless_retention_days``.

        Returns the number removed (0 when the store is empty)."""
        from . import conversations as conv
        from . import workspaces as ws
        from . import config as config_mod

        cfg = config_mod.read_workspace()
        try:
            alive = {m["id"] for m in conv.list_meta(config_mod.conversations_dir())}
        except OSError:
            alive = set()
        return ws.sweep_orphans(
            config_mod.workspaces_root(),
            alive,
            headless_retention_days=int(cfg.get("headless_retention_days", 7)),
        )


# ── helpers ───────────────────────────────────────────────────────────────────
