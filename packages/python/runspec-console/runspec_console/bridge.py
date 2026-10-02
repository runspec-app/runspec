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

from . import config as config_mod
from .bridge_const import logger
from .bridge_helpers import _ParamikoDevHandler


from .bridge_infra import InfraMixin
from .bridge_hosts import HostsMixin
from .bridge_transfer import TransferMixin
from .bridge_history_sync import HistorySyncMixin
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
from .bridge_improve import ImproveMixin
from .bridge_evals import EvalsMixin
from .bridge_mcp_server import McpServerMixin
from .bridge_voice import VoiceMixin
from .bridge_marketplace import MarketplaceMixin
from .bridge_provision import ProvisionMixin
from .bridge_turn import TurnMixin
from .dictation import DictationSession
from .session import ChatSession


class Bridge(
    InfraMixin,
    HostsMixin,
    TransferMixin,
    ReportingMixin,
    HistorySyncMixin,
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
    ImproveMixin,
    EvalsMixin,
    McpServerMixin,
    VoiceMixin,
    MarketplaceMixin,
    ProvisionMixin,
    TurnMixin,
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
        # Chat sessions (session.py): every conversation the console runs —
        # concurrent interactive chats the operator swaps between, plus one
        # read-only automation session per trigger / schedule card (id == card
        # id == the headless turn's chat_id). Each owns its own rolling model
        # history + lock; the registry is in-memory only (nothing survives a
        # restart). _chat_session_id is the interactive session sends, operator
        # runs and the workspace fallback target (set by select_chat_session /
        # new_chat_session; minted lazily). _turn_sessions maps a live turn's
        # chat_id → session id so tool workers resolve their workspace. All
        # guarded by self._lock (never a session's own lock, which a running
        # turn holds end-to-end).
        self._sessions: dict[str, ChatSession] = {}
        self._chat_session_id: str = ""
        self._turn_sessions: dict[str, str] = {}
        # Serialises saved-conversation file writes (read-modify-write) between
        # a turn's persist and an operator run's append from the invoke worker.
        self._conv_store_lock = threading.Lock()
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
        # Marketplace tab: serialises catalogue reads (so two tab mounts don't
        # double-fetch a 45 MB index), holds the running / recent install ops,
        # and memoises the last installed-state probe (see bridge_marketplace).
        self._marketplace_lock = threading.Lock()
        self._marketplace_ops: dict[str, dict[str, Any]] = {}
        self._marketplace_installed: dict[str, Any] | None = None
        # Per-target installed-probe cache: target id → (wall_ts, ok-row). The
        # probe fan-out reuses a target's row within _PROBE_TTL_S, so the
        # Marketplace tab's frequent focus/connect refreshes re-SSH only stale or
        # newly connected targets — not the whole fleet — while the assembled
        # result (_marketplace_installed) serves repeat opens instantly. Cleared
        # on install/uninstall + clear-cache (a real change → force a fresh probe).
        self._marketplace_probe_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._marketplace_probe_lock = threading.Lock()
        # Debounce gate for the host_up/host_down + room_up/room_down audio cues:
        # first observation silent, flips cue, per-key cooldown so a flapping
        # host can't become a metronome. See sounds.ConnectionCueGate.
        from . import sounds as _sounds

        self._conn_cue_gate = _sounds.ConnectionCueGate()
        # Real remote log dirs per venv runspec path (asked of the remote, cached
        # for the session — they don't move). Avoids guessing the dir from the
        # binary path, which is wrong for pipx / ~/.local / system installs.
        self._remote_logdir_cache: dict[str, list[str]] = {}
        # Remote History read cache: (host, runnable) → (monotonic_ts, records).
        # The History tab polls; a connected remote's stream+parse is reused
        # within _REMOTE_HISTORY_TTL so idle polls don't re-SSH every host.
        # Busted on run completion (_invalidate_remote_history) so a just-ended
        # run still shows promptly. Guarded by its own lock (the SSH read runs
        # outside it — a miss-race just re-reads, same result).
        self._remote_history_cache: dict[
            tuple[str, str], tuple[float, list[dict[str, Any]]]
        ] = {}
        self._remote_history_lock = threading.Lock()
        # Analytics result cache: (hosts, since_days, runnable) → (monotonic_ts,
        # result). The Analytics tab re-scans on a 45s poll + every filter change;
        # a repeat within _ANALYTICS_TTL is served from here instead of
        # re-streaming every host. Guarded by _remote_history_lock; busted on run
        # completion alongside the history cache.
        self._analytics_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
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
        # host name → (monotonic ts, pool transport generation, discovery key) of
        # the host's last successful runnable discovery. The refresh cycle re-runs
        # discovery only when a host has no stamp, its transport generation moved
        # (reconnect), its discovery key changed (paths / identity edited), or
        # ``[refresh] discovery_interval`` has elapsed — the connectivity probe
        # itself stays on every cycle. Guarded by ``self._lock``.
        self._discovery_stamp: dict[str, tuple[float, int, tuple]] = {}
        # Serialises overlapping refresh cycles (the 30 s watcher vs. a forced
        # cycle kicked by a host save / profile switch) so two cycles never
        # interleave their cache splices. Acquired inside the cycle, never at
        # spawn, so a UI call that kicks a cycle returns immediately.
        self._refresh_cycle_lock = threading.Lock()
        # Bumped by every kicked (non-periodic) refresh so a cycle already in
        # progress notices it has been superseded and bails out at its next task
        # boundary instead of making the kick wait a full fleet probe. Guarded by
        # ``self._lock``.
        self._refresh_epoch = 0
        # event name → monotonic ts of its last coalesced dispatch (see
        # _dispatch_coalesced). Guarded by ``self._lock``.
        self._coalesced_at: dict[str, float] = {}
        # The rolling model history, per-turn tags, "in context" exclusions and
        # the saved-conversation id all live on the ChatSession (see
        # self._sessions above) — one per concurrent conversation.
        # chat_id → cancel Event, so a turn can be stopped mid-run (guarded by
        # self._lock, never a session lock — cancel must fire while a turn holds
        # that).
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
        # Parallel bounded pool for *ad-hoc* SSH targets (not in the fleet) — the
        # persistent backing for the built-in file tools' ad-hoc source path so a
        # repeat download/relay reuses a warm transport instead of handshaking per
        # call. None when pooling is off (falls back to connect-per-call).
        self._adhoc_pool: Any = self._build_adhoc_pool()
        # Rota "Take a break": an in-memory return time. While set, the rota gate
        # treats automation as outside-hours; it auto-resumes once `now` passes it
        # (the gate evaluates `now` live) and self-clears. Held only in memory so a
        # break is naturally transient (lost on restart, which is fine).
        self._break_until: datetime | None = None
        # In-app dictation (bridge_voice.py): the one live session, or None.
        self._dictation: DictationSession | None = None
        # External MCP servers the chat agent connects to ([mcp] / [[mcp.servers]]).
        # Constructed cheaply (no SDK import, no connection) so the Bridge always
        # has one; app.py calls mcp_start() at bring-up to actually connect. A
        # no-op unless servers are configured and the `mcp` extra is installed.
        from .mcp_client import McpManager

        self._mcp = McpManager(credential_resolver=self._mcp_credential)
        # The console's own MCP *server* ([mcp_server]): publishes the fleet's
        # runnables to an external coding agent (Claude Code) over streamable-HTTP
        # on localhost, with confirm-level calls parking in the console's confirm
        # UI. Constructed cheaply (no SDK import, no socket); app.py calls
        # mcp_server_start() at bring-up — a no-op unless [mcp_server] enabled.
        from . import config as _config
        from .mcp_server import ConsoleMcpServer

        self._mcp_server = ConsoleMcpServer(
            config_reader=_config.read_mcp_server,
            tools_provider=self._mcp_server_runnables,
            execute=self._mcp_server_execute,
            brand_name=lambda: str(_config.theme_config().get("brand_name") or ""),
            builtins_provider=self._mcp_server_builtins,
            execute_builtin=self._mcp_server_execute_builtin,
        )
        self._install_ssh_log_forwarder()
        # Apply any baseline config bundled in the deployment package before the
        # first load, so a fresh / air-gapped install is configured with no git
        # host or PyPI index reachable. A no-op unless the seed's version changed
        # (re-assert-on-upgrade); never fatal — a broken seed must not stop the
        # console starting. See docs/design/config-seed.md.
        self._seed_last_error = ""
        try:
            seed_res = self.seed_apply()
        except Exception:
            self._seed_last_error = "config seed apply crashed (see log)"
            logger.warning("config seed apply at startup failed", exc_info=True)
        else:
            # seed_apply returns {ok: False} (it doesn't raise) when a bundled
            # file is malformed — which aborts the *whole* seed. Log it instead
            # of silently discarding the result and leaving a fresh install
            # unconfigured with no trace. Surfaced in seed_status too.
            if not seed_res.get("ok", True):
                logger.error("config seed not applied — %s", seed_res.get("error"))
        # Warn on typo'd keys in the ad-hoc config.toml sections that have no
        # dedicated reader ([ssh]/[refresh]) — otherwise a misspelled key is
        # valid TOML and silently ignored. Warn-only; never blocks startup.
        for _w in config_mod.validate_config(self.get_config()):
            logger.warning("config.toml: %s", _w)
        # The AI off-switch (`[console] ai`), read once — after the seed so a
        # seeded value applies on first launch — and held for the whole session:
        # every agent surface (chat, triggers, schedules, evals, improve, the
        # adapter itself) and the UI read this one flag, so they can't disagree
        # mid-session. Restart to apply a change.
        self._ai_enabled: bool = config_mod.ai_enabled()
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
        # Close any live MCP client sessions (terminating stdio subprocesses).
        atexit.register(self._mcp.stop)
        # Stop the console's own MCP server and wait (bounded) for its loop thread
        # to unwind — `stop()` alone only signals uvicorn, leaving the background
        # loop to be torn down mid-serve with pending tasks ("Task was destroyed
        # but it is pending!"). `shutdown()` joins so the loop is drained + closed.
        atexit.register(self._mcp_server.shutdown)
        # Unblock any in-flight turn parked at the autonomy gate so closing the
        # app can't hang waiting to join a worker stuck in event.wait(). Registered
        # last so it runs first (LIFO) — before concurrent.futures joins workers.
        atexit.register(self._abort_all_turns)

    def _dispatch(self, event: str, detail: dict[str, Any]) -> None:
        # Enqueue; the single ui-dispatch thread delivers it (see __init__).
        # Non-blocking, so a caller on the COM pump / a trigger worker never
        # stalls on evaluate_js and concurrent callers can't race the webview.
        self._dispatch_q.put((event, detail))
        # When fleet discovery updates, tell any connected MCP client to re-list —
        # otherwise a client that connected before discovery finished keeps showing
        # a stale/empty tool set until it manually reconnects. Best-effort.
        if event == "runspec:runnables_updated":
            server = getattr(self, "_mcp_server", None)
            if server is not None:
                server.notify_tools_changed()

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
