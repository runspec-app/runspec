"""bridge_triggers.py — TriggersMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any


from . import config as config_mod
from . import notify

from .bridge_const import (
    logger,
    _AUTONOMY_RANK,
    _OUTLOOK_TOOL_NAMES,
    _ACTION_TOOL_NAMES,
)
from .bridge_helpers import (
    _BridgeTriggerOps,
    _chat_connect_summary,
    _classify_room_connection,
    _RotaPresenceWatcher,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


class TriggersMixin:
    def start_triggers(self) -> None:
        """Build the trigger engine from config and start its sources. Idempotent
        — safe to call again; it no-ops once an engine is running."""
        with self._lock:
            if self._trigger_engine is not None:
                return

        from . import sources  # noqa: F401 — registers built-in event sources
        from .config import (
            read_mailbox,
            read_triggers,
            read_watches,
            trigger_file_error,
            trigger_state_path,
        )
        from .trigger_runner import make_handler
        from .trigger_state import ThreadDedupStore
        from .triggers import TriggerEngine, load_triggers

        file_error = trigger_file_error()
        if file_error:
            # A TOML syntax error rejects the whole file → no triggers load.
            # Log loudly (the UI surfaces it via list_triggers' errors).
            logger.warning("trigger config: %s", file_error)
        triggers, errors = load_triggers(read_triggers())
        for err in errors:
            logger.warning("trigger config: %s", err)
        # A configured mailbox needs the Outlook source running even with zero
        # triggers (new_mail still flows in for any future trigger), so don't bail
        # on zero triggers when a mailbox is configured.
        mailbox = read_mailbox()
        # Watch-only [[watch]] extras (the operator's own Inbox alongside a shared
        # mailbox) also need the Outlook source running, even with no primary
        # [mailbox] and no triggers — their new_mail still flows in for any future
        # trigger.
        watches = read_watches()
        # Schedules (Schedules tab) can run with no triggers at all — a
        # runnable-action schedule fires directly — so they keep the engine alive
        # too (event-action schedules still need it to route their events).
        schedules = self._read_schedules()
        if not triggers and mailbox is None and not watches and not schedules:
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
        # mailbox (primary or a watch-only extra) is configured, so its new_mail
        # events flow in (the email channel; docs/design/teams-and-email-monitoring.md).
        always_start = {"outlook"} if (mailbox is not None or watches) else set()
        engine = TriggerEngine(
            triggers,
            handler=handler,
            on_card=self._on_trigger_card,
            on_event=self._on_trigger_event,
            clock_ok=self._build_clock_ok(),
            lock_ok=self._build_lock_ok(),
            # The chat source is owned by the always-on supervisor (start_chat),
            # not the engine — it pushes messages in via engine.feed(). Marking it
            # external stops the engine constructing/starting a second one. The
            # schedule source is likewise bridge-owned (it needs the trigger list
            # to build its APScheduler jobs) and feeds ticks in via engine.feed().
            external_sources={"chat", "schedule"},
            always_start=always_start,  # schedule source built from the Schedules tab below
            # Durable once_per_thread dedup — so a trigger that fired on a thread's
            # opening message doesn't re-fire on replies after a console restart.
            thread_store=ThreadDedupStore(trigger_state_path()),
            # The mailbox-wide reply-suppression default a trigger inherits when it
            # doesn't set its own once_per_thread.
            default_once_per_thread=bool(mailbox.get("once_per_thread"))
            if mailbox
            else False,
        )
        problems = engine.start()
        with self._lock:
            self._trigger_engine = engine
        for problem in problems:
            logger.info("trigger source: %s", problem)
        # Schedule source: bridge-owned (external). Built from the Schedules tab
        # (runspec_schedules.toml), it fires event-action schedules into
        # engine.feed() — where source="schedule" triggers match them like mail —
        # and runs runnable-action schedules directly via _run_scheduled. Started
        # after the engine so its events route like any other.
        if schedules:
            from .sources.schedule import ScheduleSource

            src = ScheduleSource(
                schedules,
                run_runnable=self._run_scheduled,
                # Prompt/skill schedules run a headless agent turn (isolated, same
                # autonomy gate as a reactive agent-mode trigger).
                run_agent=self._run_scheduled_agent,
                # Same working-hours gate the engine uses, so schedules and
                # triggers honour one shared rota; plus the Schedules event-log hook.
                clock_ok=self._build_clock_ok(),
                on_event=self._on_schedule_event,
            )
            ok, reason = src.available()
            if ok:
                src.start(engine.feed)
                with self._lock:
                    self._schedule_source = src
            else:
                logger.info("schedule source not started: %s", reason)
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
            schedule_source = self._schedule_source
            self._schedule_source = None
            card_ids = set(self._trigger_cards)
        self._abort_turns(card_ids)
        if schedule_source is not None:
            schedule_source.stop()
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
        from .winlock import is_screen_locked

        rota = read_rota()
        grid_configured = any(isinstance(rota.get(d), dict) for d in _DAYS)

        def clock_ok() -> bool:
            now = datetime.now()
            # Rota "Take a break": suspend while the break window is open, then
            # auto-resume — unless the screen is still locked (operator still away),
            # in which case hold until unlock. Self-clears so a *later* normal lock
            # never re-suspends. `now` is live, so no timer/restart is needed.
            bu = self._break_until
            if bu is not None:
                if now < bu:
                    return False
                if is_screen_locked():
                    return False
                self._break_until = None
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

    def _build_lock_ok(self) -> Callable[[], bool]:
        """Build the lock auto-suspend gate: returns True when automation may fire
        re: operator presence. A governance control — reactive triggers don't act
        while the workstation is unattended (screen locked). **Default on** (absent
        ``lock_suspend`` ⇒ enabled), and **fail-closed**: when enabled, an
        ``unknown`` probe (Windows, but the probe raised) counts as locked so an
        uncertain reading can't let automation run unattended. Non-Windows reads
        ``unlocked`` so the gate is a no-op there. The enabled flag is captured at
        build (toggling it goes through ``save_rota`` → restart); the lock *state*
        is read live."""
        from .config import read_rota
        from .winlock import screen_lock_state

        enabled = bool(read_rota().get("lock_suspend", True))

        def lock_ok() -> bool:
            if not enabled:
                return True
            return screen_lock_state() == "unlocked"

        return lock_ok

    def screen_state(self) -> str:
        """Current screen-lock state (`"locked"`/`"unlocked"`/`"unknown"`) for the
        Rota tab's live presence indicator. Cheap — a single desktop probe."""
        from .winlock import screen_lock_state

        return screen_lock_state()

    def start_voice_dictation(self) -> dict[str, bool]:
        """Wake Windows Voice access so it listens and dictates into the focused
        prompt box — the composer's **Speak** button calls this after focusing the
        textarea. Injects the Voice access wake/sleep chord (Alt+Shift+B) at the OS
        level; the WebView2 sandbox can't synthesize a hotkey itself. Returns
        ``{"ok": <bool>}``; a no-op (``False``) off Windows or on failure, leaving
        the operator to drive Voice access from its own bar."""
        from .winkeys import send_voice_access_toggle

        return {"ok": send_voice_access_toggle()}

    def stop_voice_dictation(self) -> dict[str, bool]:
        """Put Windows Voice access back to sleep — the composer calls this when the
        operator sends. Injects the *same* wake/sleep chord (Alt+Shift+B); the UI
        tracks the listening state and presses it to flip. Returns ``{"ok":
        <bool>}``; a no-op (``False``) off Windows or on failure."""
        from .winkeys import send_voice_access_toggle

        return {"ok": send_voice_access_toggle()}

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
    # A connection must stay up at least this long to count as "healthy" and reset
    # the backoff. A room that opens the channel then drops it almost immediately is
    # *flapping* — usually the room rejecting this console after the hello (the queue
    # mailbox can't be watched, or an identity/mailbox mismatch). Treating that as a
    # failure keeps the backoff growing instead of spinning a tight reconnect +
    # catalog-republish loop.
    _CHAT_MIN_HEALTHY = 10.0

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
        # This console's helpdesk identity — the configured [mailbox] address. The
        # room ties one mailbox to one room and refuses a console that doesn't match.
        from .config import read_mailbox

        mb = read_mailbox()
        mailbox = mb["address"] if mb else ""
        backoff = self._CHAT_BACKOFF_START
        while not self._chat_stop.is_set():
            closed = threading.Event()
            source = ChatSource(
                room="" if queue else room,
                namespace=room if queue else None,
                user=user,
                queue=queue,
                queue_email=str(spec.get("queue_email") or ""),
                queue_label=str(spec.get("queue_label") or ""),
                mailbox=mailbox,
                transport_factory=factory,
                on_closed=closed.set,
            )
            try:
                source.start(self._on_chat_event)
            except Exception as exc:
                # Connect failed (the common one: the room server is stopped, or
                # forwarding is blocked). Log a concise summary, and only when it
                # *changes* for this room — so a stopped server logs one line, not a
                # fresh line (or a 20-line traceback) every backoff. Unrecognised
                # failures keep their full traceback, deduped the same way.
                message, want_traceback = _chat_connect_summary(
                    exc, via=str(spec.get("via") or "tunnel")
                )
                with self._lock:
                    already = self._chat_connect_log.get(room) == message
                    self._chat_connect_log[room] = message
                if not already:
                    if want_traceback:
                        logger.exception("chat: connect to room %r failed", room)
                    else:
                        logger.warning("chat: room %r — %s", room, message)
                if self._chat_stop.wait(backoff):
                    break
                backoff = min(backoff * 2, self._CHAT_BACKOFF_MAX)
                continue
            with self._lock:
                self._chat_sources[room] = source
                if queue:
                    self._agent_rooms[room] = user
            connected_at = time.monotonic()
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
            # Reset the backoff only if the connection proved *healthy* (stayed up
            # past the min-healthy window). A connection the room opens then drops
            # almost immediately is flapping — typically the room rejecting this
            # console after the hello (the queue mailbox can't be watched, or an
            # identity/mailbox mismatch). Keep backing off so we don't spin a tight
            # reconnect + catalog-republish loop, and log the reason once.
            if time.monotonic() - connected_at >= self._CHAT_MIN_HEALTHY:
                backoff = self._CHAT_BACKOFF_START
                with self._lock:
                    self._chat_connect_log.pop(room, None)  # a later drop re-logs
            else:
                message = (
                    "room closed the connection right after it opened — it may be "
                    "rejecting this console (the queue mailbox can't be watched, or "
                    "an identity/mailbox mismatch). Backing off."
                )
                with self._lock:
                    already = self._chat_connect_log.get(room) == message
                    self._chat_connect_log[room] = message
                if not already:
                    logger.warning("chat: room %r — %s", room, message)
            # Unexpected drop → back off, then reconnect.
            if self._chat_stop.wait(backoff):
                break
            backoff = min(backoff * 2, self._CHAT_BACKOFF_MAX)

    def _publish_catalog(
        self, spec: dict[str, Any], source: Any, *, force: bool = False
    ) -> None:
        """Build and publish one queue's catalog from the discovery cache — the
        combined, audience-tagged channel the room filters per session: the
        self-service subset for visitors, the full operator catalogue plus published
        runbooks for operators. **Idempotent**: the catalog is only sent (and logged)
        when its contents changed since the last publish for this namespace — so the
        periodic discovery refresh doesn't re-emit an unchanged catalog every cycle.
        ``force`` overrides the diff (the subscribe path always sends, as a fresh
        connection's hub has no catalog yet). Best-effort — a failed publish never
        breaks the connection; the next refresh or reconnect republishes."""
        from .catalog import build_full_catalog

        try:
            key = spec["room"]
            with self._lock:
                runnables = list(self._runnables_cache)
            allowlist = spec.get("catalog_allowlist")
            tools, index = build_full_catalog(
                runnables, self_service_allowlist=allowlist
            )
            skill_tools, skill_index = self._build_room_skills()
            tools = tools + skill_tools
            with self._lock:
                self._catalog_index[key] = index  # runnable enactment map
                self._skill_index[key] = skill_index  # skill enactment map (name→id)
                unchanged = not force and self._published_catalogs.get(key) == tools
                if not unchanged:
                    self._published_catalogs[key] = tools
            if unchanged:
                return  # nothing to send — the room already has this catalog
            source.send_catalog(tools)
            logger.info("chat: published %d tool(s) to %r catalog", len(tools), key)
        except Exception:
            logger.exception("chat: catalog publish for %r failed", spec.get("room"))

    def _build_room_skills(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """The publishable runbooks for the catalog: ``(tools, index)`` where
        ``tools`` are skill catalog entries (``skill:<id>`` names, operator audience)
        and ``index`` maps each catalog name back to the skill id for enactment.
        Skills opt in via their ``discoverable`` audience (default off), so an
        ordinary runbook never lands in the catalog by accident."""
        from . import memory
        from .catalog import build_skill_catalog

        try:
            skills = memory.load_skills()
        except Exception:
            logger.exception("chat: loading skills for catalog failed")
            return [], {}
        return build_skill_catalog(skills)

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

    def _on_chat_event(self, event: Any) -> None:
        """A chat source's emit: fan every event to the Rooms view and drive the
        runnables-portal flow.

        - ``presence`` → UI only (out of the trigger audit feed).
        - ``assignment`` → the hub routed a portal request to *this* console: surface
          it and **enact** the runnable through the autonomy gate (claim-free —
          assignment *is* the gate).
        - ``request_status`` / ``page`` → UI (a request's claim-free lifecycle / a
          chime for an unassigned request); a page also raises a desktop notice.
        - ``message`` → UI + the live trigger engine (a configured chat trigger can
          still fire) + an inbox card. A ``/run …`` message is surfaced to the
          engine as its own ``chat``/``run`` event (the chat analog of ``new_mail``)
          so it lands in the event log; no action is wired to it yet."""
        payload = dict(event.payload)
        etype = event.type
        if etype == "presence":
            self._dispatch("runspec:room_presence", payload)
            return
        if etype == "assignment":
            self._dispatch("runspec:room_assignment", payload)
            self._enact_assignment(payload)
            return
        if etype == "request_status":
            self._dispatch("runspec:room_request_status", payload)
            return
        if etype == "page":
            self._dispatch("runspec:room_page", payload)
            self._notify_page(payload)
            return
        if etype == "teams":
            # The room's reply to a team_list / team_add / team_remove — the Teams
            # settings tab refreshes from this.
            self._dispatch("runspec:teams", payload)
            return
        self._dispatch("runspec:room_message", payload)
        self._notify_room_message(payload)
        self._touch_queue_card(str(payload.get("room") or ""), text=payload.get("text"))
        with self._lock:
            engine = self._trigger_engine
        if engine is not None:
            try:
                engine.feed(self._chat_event_for_engine(event, payload))
            except Exception:
                logger.exception("chat: routing message to the trigger engine failed")

    @staticmethod
    def _chat_event_for_engine(event: Any, payload: dict[str, Any]) -> Any:
        """The event to feed the trigger engine for a chat message. A ``/run …``
        message becomes a distinct ``chat``/``run`` event (first-class, like an
        email's ``new_mail``) so it's logged and a future trigger can match it;
        every other message feeds unchanged."""
        if str(payload.get("text") or "").lstrip().startswith("/run"):
            from .events import Event

            return Event.new("chat", "run", payload)
        return event

    # ── runnables portal: enact an assigned request ───────────────────────────

    @staticmethod
    def _namespace_of(room: str) -> str:
        """The queue namespace (``helpdesk/<queue>``) for a conversation ``room``
        (``helpdesk/<queue>/<visitor>``), or ``""`` if it isn't one."""
        parts = room.split("/")
        if len(parts) >= 2 and parts[0] == "helpdesk" and parts[1]:
            return f"helpdesk/{parts[1]}"
        return ""

    def _enact_assignment(self, payload: dict[str, Any]) -> None:
        """Enact a portal request the hub routed to this console. Runs on a worker
        thread so a (possibly confirm-gated) run never blocks the chat recv loop;
        the actual run goes through the autonomy gate, unchanged."""
        request_id = str(payload.get("id") or "")
        room = str(payload.get("room") or "")
        runnable = str(payload.get("runnable") or "")
        raw_args = payload.get("args")
        args = dict(raw_args) if isinstance(raw_args, dict) else {}
        kind = "skill" if str(payload.get("kind") or "") == "skill" else "runnable"
        # The host the requester picked (a fleet runnable may live on several); blank
        # ⇒ enact on the first discovered host. Skills carry none — they run on the
        # console and target hosts from inside the runbook.
        host = str(payload.get("host") or "")
        namespace = self._namespace_of(room)
        if not (request_id and room and runnable and namespace):
            logger.warning("portal: ignoring malformed assignment %r", payload)
            return
        parts = room.split("/")
        queue = parts[1] if len(parts) > 1 else ""
        visitor = parts[2] if len(parts) > 2 else ""
        # The requester's verified display name + team (email-verify signup), shown
        # on the card beside the email; blank in dev mode.
        visitor_name = str(payload.get("visitor_name") or "")
        team = str(payload.get("team") or "")
        # A skill's wire name is ``skill:<id>``; show the bare id on the card.
        label = runnable.split("skill:", 1)[-1] if kind == "skill" else runnable
        note = f"assigned · {label}" + (f" on {host}" if host else "")
        # Surface the assignment in the Triggers inbox right away (before the
        # possibly-gated run), so the operator sees it land.
        self._portal_card(
            request_id,
            runnable=label,
            args=args,
            host=host,
            visitor=visitor,
            visitor_name=visitor_name,
            team=team,
            room=room,
            queue=queue,
            state="assigned",
            column="triggered",
            note=note,
        )
        # Run off the recv loop so a (possibly confirm-gated) run never blocks it. A
        # skill carries no host (it runs on the console); a runnable carries the
        # operator's chosen target host.
        worker: Callable[..., None]
        if kind == "skill":
            args_tuple: tuple[Any, ...] = (request_id, room, runnable, args, namespace)
            worker = self._run_skill_assignment
        else:
            args_tuple = (request_id, room, runnable, args, namespace, host)
            worker = self._run_assignment
        threading.Thread(
            target=worker, args=args_tuple, name="portal-enact", daemon=True
        ).start()

    def _run_assignment(
        self,
        request_id: str,
        room: str,
        runnable: str,
        args: dict[str, Any],
        namespace: str,
        host: str = "",
    ) -> None:
        """Worker: resolve the runnable (on the requested ``host`` when given, else
        the first discovered), run it through the autonomy gate, deliver the result
        on the thread, and report the request done/failed."""
        with self._lock:
            source = self._chat_sources.get(namespace)
            by_host = dict(self._catalog_index.get(namespace, {}).get(runnable) or {})
        if source is None:
            logger.warning(
                "portal: no chat source for %r — cannot enact %r", namespace, runnable
            )
            self._portal_fail(request_id, None, "operator offline — cannot enact")
            return
        if not by_host:
            self._portal_fail(
                request_id, source, f"'{runnable}' is no longer available"
            )
            return
        # Pick the requested host; fall back to the first discovered when none was
        # named. A named host this console can't reach is a clear, recoverable error.
        if host and host not in by_host:
            self._portal_fail(
                request_id,
                source,
                f"'{runnable}' is not available on {host} from this operator "
                f"(have: {', '.join(h for h in by_host if h) or 'none'})",
            )
            return
        disc = by_host.get(host) if host else next(iter(by_host.values()))
        resolved_host = str((disc or {}).get("host") or "")
        name = str((disc or {}).get("name") or runnable)
        if not resolved_host:
            self._portal_fail(request_id, source, f"'{runnable}' has no host")
            return
        tool_name = f"{resolved_host}__{name}"
        source.send_request_start(request_id)
        self._portal_card(
            request_id, state="in_progress", column="running", note="enacting…"
        )
        try:
            # chat_id == the card_id, so the autonomy gate's Approve/Deny prompt
            # (runspec:tool_confirm id=chat_id) renders on this Triggers card.
            output = self._gated_run_tool(
                tool_name, args, chat_id=self._portal_card_id(request_id)
            )
        except Exception as exc:  # noqa: BLE001 — surface failure to the visitor
            logger.exception("portal: enacting %r failed", runnable)
            self._portal_fail(request_id, source, str(exc))
            return
        # The gate marks a refusal/non-zero run with a leading ✗ / [exit …].
        failed = output.startswith("✗ ") or output.startswith("[exit ")
        try:
            source.send_message(output, room=room)  # deliver the result on the thread
        except Exception:
            logger.exception("portal: posting result for %r failed", runnable)
        source.send_request_complete(
            request_id, error=(output[:500] if failed else None)
        )
        self._portal_card(
            request_id,
            state="failed" if failed else "done",
            column="failed" if failed else "done",
            result=output,
            error=output[:500] if failed else None,
            note="failed" if failed else "done",
        )

    def _run_skill_assignment(
        self,
        request_id: str,
        room: str,
        wire_name: str,
        values: dict[str, Any],
        namespace: str,
    ) -> None:
        """Worker: enact an assigned **skill** (runbook) request. A skill is not a
        single runnable but an agent turn — we render its body with the visitor's
        form ``values`` and run it through the same isolated, autonomy-gated loop a
        trigger/schedule uses (``_trigger_run_agent_turn``), so every runnable the
        runbook reaches still parks at the per-runnable confirm gate on this card. The
        turn is run to completion here (this worker is already off the recv loop), and
        only when it ends do we deliver the summary on the thread and report the
        request done/failed — the visitor sees the real outcome, not a hand-off."""
        from . import memory, skillargs

        with self._lock:
            source = self._chat_sources.get(namespace)
            skill_id = self._skill_index.get(namespace, {}).get(wire_name)
        label = wire_name.split("skill:", 1)[-1]
        if source is None:
            logger.warning(
                "portal: no chat source for %r — cannot enact runbook %r",
                namespace,
                label,
            )
            self._portal_fail(request_id, None, "operator offline — cannot enact")
            return
        if not skill_id:
            self._portal_fail(
                request_id, source, f"runbook '{label}' is no longer available"
            )
            return
        skill = memory.load_skill(skill_id)
        if not skill:
            self._portal_fail(request_id, source, f"runbook '{label}' no longer exists")
            return
        raw = skill.get("args") if isinstance(skill.get("args"), dict) else {}
        errors = skillargs.validate_skill_values(raw, values)
        if errors:
            self._portal_fail(request_id, source, "; ".join(errors))
            return
        body = memory.render_skill_body(str(skill.get("steps") or ""), values)
        if not body.strip():
            self._portal_fail(
                request_id, source, f"runbook '{label}' has an empty body"
            )
            return
        title = skill.get("title") or skill_id
        message = (
            f"Runbook '{title}' — follow these steps now using your tools:\n\n{body}"
        )
        source.send_request_start(request_id)
        self._portal_card(
            request_id, state="in_progress", column="running", note="enacting runbook…"
        )
        card_id = self._portal_card_id(request_id)

        def record(note: str) -> None:
            self._portal_card(request_id, note=note)

        try:
            # The card id doubles as the agent turn's chat_id, so each runnable's
            # autonomy-gate Approve/Deny prompt renders on this portal card.
            summary = self._trigger_run_agent_turn(
                card_id,
                self._base_system(),
                message,
                draft=False,
                record=record,
                trigger="portal-skill",
            )
        except Exception as exc:  # noqa: BLE001 — surface failure to the visitor
            logger.exception("portal: enacting runbook %r failed", label)
            self._portal_fail(request_id, source, str(exc))
            return
        failed = summary.startswith("⚠") or summary.startswith("✗")
        try:
            source.send_message(summary, room=room)  # deliver the result on the thread
        except Exception:
            logger.exception("portal: posting runbook result for %r failed", label)
        source.send_request_complete(
            request_id, error=(summary[:500] if failed else None)
        )
        self._portal_card(
            request_id,
            state="failed" if failed else "done",
            column="failed" if failed else "done",
            result=summary,
            error=summary[:500] if failed else None,
            note="failed" if failed else "done",
        )

    @staticmethod
    def _portal_card_id(request_id: str) -> str:
        """The Triggers-card id for a portal request — kept equal to the autonomy
        gate's ``chat_id`` so the gate's Approve/Deny prompt and the run result
        render on this card (the operator's enact + audit surface)."""
        return f"portal:{request_id}"

    def _portal_fail(self, request_id: str, source: Any, error: str) -> None:
        """Mark a portal request failed on both the visitor thread (if reachable)
        and the operator's Triggers card."""
        if source is not None:
            source.send_request_complete(request_id, error=error)
        self._portal_card(
            request_id, state="failed", column="failed", error=error, note=error
        )

    def _portal_card(
        self,
        request_id: str,
        *,
        runnable: str | None = None,
        args: dict[str, Any] | None = None,
        host: str | None = None,
        visitor: str | None = None,
        visitor_name: str | None = None,
        team: str | None = None,
        room: str | None = None,
        queue: str | None = None,
        state: str | None = None,
        column: str | None = None,
        result: str | None = None,
        error: str | None = None,
        note: str | None = None,
    ) -> None:
        """Create or update one portal request's card in the Triggers inbox and
        dispatch it to the UI. Keyed by ``portal:<request_id>`` so the whole
        lifecycle (assigned → running → done/failed) updates one card."""
        card_id = self._portal_card_id(request_id)
        now = time.time()
        with self._lock:
            card = self._trigger_cards.get(card_id)
            if card is None:
                card = {
                    "card_id": card_id,
                    "trigger": f"portal:{queue}" if queue else "portal",
                    "source": "portal",
                    "event_type": "request",
                    "summary": "",
                    "column": "triggered",
                    "detail": {"request_id": request_id},
                    "created_at": now,
                    "timeline": [],
                }
                self._trigger_cards[card_id] = card
            detail = card["detail"]
            for key, val in (
                ("runnable", runnable),
                ("args", args),
                ("host", host),
                ("visitor", visitor),
                ("visitor_name", visitor_name),
                ("team", team),
                ("room", room),
                ("queue", queue),
                ("state", state),
                ("result", result),
                ("error", error),
            ):
                if val is not None:
                    detail[key] = val
            if column is not None:
                card["column"] = column
            if note is not None:
                card["timeline"].append({"ts": now, "text": note})
            rn = detail.get("runnable") or "request"
            # Prefer the display name on the summary line; the email stays in detail
            # (the UI shows "Name <email>"). Falls back to the email when unnamed.
            vis = detail.get("visitor_name") or detail.get("visitor")
            card["summary"] = f"{rn} · {vis}" if vis else rn
            self._evict_trigger_cards(card_id)
            snapshot = dict(card)
        self._dispatch("runspec:trigger_card", snapshot)

    def _notify_page(self, payload: dict[str, Any]) -> None:
        """Best-effort desktop toast for a page — a chime naming a runnable an
        unassigned request in a queue we staff is waiting on. Same gates as
        ``_notify_room_message`` (skip when foreground / opted out); always
        swallowed on error."""
        try:
            if self._window_is_foreground():
                return
            try:
                if not config_mod.room_notifications_enabled():
                    return
            except Exception:
                pass
            toast_ok, _ = notify.available()
            runnable = str(payload.get("runnable") or "a runnable")
            visitor = str(
                payload.get("visitor_name") or payload.get("visitor") or "Someone"
            )
            title = "Runnable requested — no operator on shift"
            body = f"{visitor} is waiting on {runnable}"
        except Exception:
            logger.debug("notify_page setup failed", exc_info=True)
            return
        try:
            # Toast needs the [notifications] extra; flash + sound don't.
            if toast_ok:
                notify.notify(title, body, on_click=self._focus_window)
            self._flash_window()
            self._play_sound("message")
        except Exception:
            logger.debug("desktop page notify failed", exc_info=True)

    # State → lifecycle status for a queue card's StatusBadge fallback (the row's
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
        parsed = self._parse_queue_room(room)
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
                card["column"] = self._QUEUE_STATE_COLUMN.get(
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

    # ── teams management (Settings → Teams) ──────────────────────────────────────
    # The customer-team list lives in the room DB; the console manages it over its
    # live helpdesk agent connection. Each call is fire-and-forget — the room's
    # reply arrives as a runspec:teams event the Teams tab listens for.

    def _helpdesk_source(self) -> Any:
        """The live agent ChatSource staffing a helpdesk namespace (teams ride this
        connection). The first one — there's a single helpdesk per room."""
        with self._lock:
            for namespace in self._agent_rooms:
                src = self._chat_sources.get(namespace)
                if src is not None:
                    return src
        return None

    def list_teams(self) -> dict[str, Any]:
        """Ask the connected helpdesk room for its team list (reply: runspec:teams).
        ``{ok: False, error}`` when no helpdesk room is connected."""
        src = self._helpdesk_source()
        if src is None:
            return {"ok": False, "error": "Not connected to a helpdesk room."}
        src.send_team_list()
        return {"ok": True}

    def add_team(self, name: str) -> dict[str, Any]:
        """Create/rename a team by display name (the room derives the slug + replies
        with the updated list)."""
        name = str(name or "").strip()
        if not name:
            return {"ok": False, "error": "A team needs a name."}
        src = self._helpdesk_source()
        if src is None:
            return {"ok": False, "error": "Not connected to a helpdesk room."}
        src.send_team_add(name)
        return {"ok": True}

    def remove_team(self, slug: str) -> dict[str, Any]:
        """Remove a team by slug (the room refuses the protected Default + replies
        with the updated list)."""
        slug = str(slug or "").strip()
        src = self._helpdesk_source()
        if src is None:
            return {"ok": False, "error": "Not connected to a helpdesk room."}
        src.send_team_remove(slug)
        return {"ok": True}

    def get_room_state(self) -> dict[str, Any]:
        """For the Rooms view: every configured room (from the fleet host entries),
        each with its host, this console's display name there, and whether it's
        currently connected. ``{"rooms": []}`` when none are configured — the view
        shows a 'no rooms' state."""
        specs = self._room_specs()
        with self._lock:
            connected = set(self._chat_sources)
        rooms = []
        for s in specs:
            entry: dict[str, Any] = {
                "room": s["room"],
                "host": s["host"],
                "user": s["user"],
                "connected": s["room"] in connected,
                "queue": str(s.get("queue") or ""),
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
        # The schedule source's event names come from the Schedules tab (the
        # registry instance is zero-arg and knows none), so a schedule-source
        # trigger's Event field can autocomplete the events schedules actually emit.
        schedule_events = sorted(
            {
                str(s.get("event") or "").strip()
                for s in self._read_schedules()
                if str(s.get("action") or "runnable") == "event" and s.get("event")
            }
        )
        for sid in available_sources():
            src = create_source(sid)
            ok, reason = src.available() if src is not None else (False, "unknown")
            event_types = src.event_types() if src is not None else []
            if sid == "schedule":
                event_types = schedule_events
            sources_info.append(
                {
                    "id": sid,
                    "available": ok,
                    "reason": reason,
                    "event_types": event_types,
                }
            )
        return {
            "triggers": raw,
            "errors": errors,
            "sources": sources_info,
            "rota": read_rota(),
            "running": self._trigger_engine is not None,
            # Active rota "Take a break" return time (ISO), or None — drives the
            # Rota tab's on-break state.
            "break_until": self._break_until.isoformat(timespec="seconds")
            if self._break_until
            else None,
            # Current screen-lock state for the Rota tab's live presence indicator.
            "screen_state": self.screen_state(),
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

    def save_trigger(self, trigger: dict[str, Any]) -> dict[str, Any]:
        """Add or update one ``[triggers.<name>]`` table (upsert by name) in
        runspec_triggers.toml, then restart the engine so it takes effect. The
        ``name`` is required and identifies the table. Empty sub-tables (``match`` /
        ``map`` / ``reply``) and blank scalars are dropped so the written TOML stays
        clean. Returns ``{ok, error?}``; the UI re-reads ``list_triggers`` after."""
        from .config import read_triggers, write_triggers

        name = str(trigger.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "A trigger needs a name."}
        clean = self._clean_trigger(trigger, name)
        raw = [t for t in read_triggers() if t.get("name") != name]
        raw.append(clean)
        write_triggers(raw)
        self.restart_triggers()
        return {"ok": True}

    def delete_trigger(self, name: str) -> dict[str, Any]:
        """Remove the ``[triggers.<name>]`` table and restart the engine."""
        from .config import read_triggers, write_triggers

        raw = read_triggers()
        kept = [t for t in raw if t.get("name") != name]
        if len(kept) != len(raw):
            write_triggers(kept)
            self.restart_triggers()
        return {"ok": len(kept) != len(raw)}

    def list_action_tools(self) -> list[dict[str, str]]:
        """The built-in action tools an agent-mode trigger can be allowed to use,
        for the Triggers editor's per-trigger allowlist. The Outlook tools are
        listed only when Outlook COM is reachable on this machine."""
        items = [
            {"name": "create_document", "label": "Create document"},
            {"name": "run_skill", "label": "Run skill (runbook)"},
            {"name": "create_schedule", "label": "Create schedule"},
            {"name": "emit_event", "label": "Emit event"},
        ]
        if self._outlook_tools():
            items += [
                {"name": "outlook_inbox", "label": "Outlook: read inbox"},
                {"name": "outlook_search", "label": "Outlook: search"},
                {"name": "outlook_get_message", "label": "Outlook: read message"},
                {"name": "outlook_calendar", "label": "Outlook: calendar"},
                {"name": "outlook_reminders", "label": "Outlook: reminders"},
                {
                    "name": "outlook_resolve_recipient",
                    "label": "Outlook: resolve recipient",
                },
                {"name": "outlook_draft", "label": "Outlook: draft mail"},
                {"name": "outlook_send", "label": "Outlook: send mail"},
                {
                    "name": "outlook_save_attachment",
                    "label": "Outlook: save attachment",
                },
                {"name": "outlook_create_event", "label": "Outlook: create event"},
                {
                    "name": "outlook_respond_invite",
                    "label": "Outlook: respond to invite",
                },
                {"name": "outlook_mark_read", "label": "Outlook: mark read/unread"},
                {"name": "outlook_categorize", "label": "Outlook: categorize"},
                {"name": "outlook_move", "label": "Outlook: move to folder"},
            ]
        if self._repo_tools():
            items += [
                {"name": "repo_status", "label": "Repo: list/status"},
                {"name": "repo_sync", "label": "Repo: sync (clone/fetch)"},
                {"name": "repo_list", "label": "Repo: list files"},
                {"name": "repo_grep", "label": "Repo: search"},
                {"name": "repo_read", "label": "Repo: read file"},
                {"name": "repo_diff", "label": "Repo: diff"},
                {"name": "repo_write", "label": "Repo: write file (local)"},
            ]
        return items

    @staticmethod
    def _clean_trigger(trigger: dict[str, Any], name: str) -> dict[str, Any]:
        """Normalise a trigger dict from the UI into the on-disk shape: keep ``name``,
        drop blank scalars / empty sub-tables, coerce the sub-tables (``match`` / ``map``
        / ``reply``) and the ``runnables`` list."""
        out: dict[str, Any] = {"name": name}
        for key in ("source", "event", "mode", "autonomy", "run", "rules", "pre_turn"):
            val = str(trigger.get(key) or "").strip()
            if val:
                out[key] = val
        for key in ("enabled", "post_to_chat", "ignore_rota"):
            if key in trigger:
                out[key] = bool(trigger[key])
        # Tri-state: only an explicit per-trigger override is written; "inherit the
        # mailbox default" (anything non-bool, e.g. omitted/null) leaves it unset.
        if isinstance(trigger.get("once_per_thread"), bool):
            out["once_per_thread"] = trigger["once_per_thread"]
        runnables = trigger.get("runnables")
        if isinstance(runnables, list):
            names = [str(r).strip() for r in runnables if str(r).strip()]
            if names:
                out["runnables"] = names
        for sub in ("match", "map"):
            raw = trigger.get(sub)
            if isinstance(raw, dict):
                pairs = {
                    str(k).strip(): str(v).strip()
                    for k, v in raw.items()
                    if str(k).strip() and str(v).strip()
                }
                if pairs:
                    out[sub] = pairs
        # Declarative multi-runnable action list (rule mode): drop blank steps and
        # empty maps; only emit action_mode when there's a list to qualify.
        actions = trigger.get("actions")
        if isinstance(actions, list):
            cleaned_actions: list[dict[str, Any]] = []
            for item in actions:
                if not isinstance(item, dict):
                    continue
                run = str(item.get("run") or "").strip()
                if not run:
                    continue
                step: dict[str, Any] = {"run": run}
                amap = item.get("map")
                if isinstance(amap, dict):
                    pairs = {
                        str(k).strip(): str(v).strip()
                        for k, v in amap.items()
                        if str(k).strip() and str(v).strip()
                    }
                    if pairs:
                        step["map"] = pairs
                cleaned_actions.append(step)
            if cleaned_actions:
                out["actions"] = cleaned_actions
                am = str(trigger.get("action_mode") or "").strip()
                if am in ("sequential", "parallel"):
                    out["action_mode"] = am
        # Agent-mode allowlist of built-in action tools this trigger may use.
        action_allow = trigger.get("action_allow")
        if isinstance(action_allow, list):
            allow = [str(a).strip() for a in action_allow if str(a).strip()]
            if allow:
                out["action_allow"] = allow
        reply = trigger.get("reply")
        if isinstance(reply, dict) and (reply.get("draft") or reply.get("style")):
            rep: dict[str, Any] = {"draft": bool(reply.get("draft"))}
            style = str(reply.get("style") or "").strip()
            if style:
                rep["style"] = style
            out["reply"] = rep
        return out

    def save_rota(self, rota: dict[str, Any]) -> dict[str, Any]:
        """Save the working-hours rota and restart the engine so it takes effect.
        Also re-join the room so chat presence follows the new schedule."""
        from .config import write_rota

        write_rota(dict(rota or {}))
        self.restart_triggers()
        self.restart_chat()
        return {"ok": True}

    def start_break(self, minutes: int = 60) -> dict[str, Any]:
        """Rota "Take a break": suspend triggers/schedules for ``minutes``, then
        auto-resume (the rota gate honours the in-memory return time live — no
        restart). Also sets a best-effort Outlook reminder at the return time so
        the operator is nudged to come back. Returns the return time + whether a
        reminder was set."""
        mins = max(1, int(minutes or 0))
        until = datetime.now() + timedelta(minutes=mins)
        self._break_until = until

        reminder_set = False
        from .sources import outlook as ol

        if ol.outlook_available():
            try:
                ol.create_reminder("Break over — re-enable runspec automation", until)
                reminder_set = True
            except Exception:
                logger.warning(
                    "rota break: could not set Outlook reminder", exc_info=True
                )

        # Reflect the pause in chat presence immediately (same gate); the trigger
        # engine reads the gate live, so no restart is needed.
        try:
            self.restart_chat()
        except Exception:
            logger.debug("rota break: chat refresh failed", exc_info=True)
        return {
            "break_until": until.isoformat(timespec="seconds"),
            "reminder_set": reminder_set,
        }

    def cancel_break(self) -> dict[str, Any]:
        """End an active break early (manual "I'm back"): clear the return time so
        the rota resumes on the next event."""
        self._break_until = None
        try:
            self.restart_chat()
        except Exception:
            logger.debug("rota break: chat refresh failed", exc_info=True)
        return {"ok": True}

    def test_trigger(
        self, name: str, body: str = "", fields: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Fire a configured trigger now with a synthetic event, for the Triggers
        tab's "Trigger now" button — so a trigger can be tested without waiting for
        a real event. Builds an event from the trigger's own ``source``/``event``
        with the pasted ``body`` (under the ``body`` payload key, overridable) and
        the key=value ``fields``, then dispatches *that one* trigger via the engine
        — bypassing match/rota/dedup. Requires the engine running. Returns
        ``{ok, error?}``; a run card appears in the Triggers inbox on success."""
        from .config import read_triggers
        from .events import Event

        engine = self._trigger_engine
        if engine is None:
            return {"ok": False, "error": "The trigger engine isn't running."}
        raw = next((t for t in read_triggers() if t.get("name") == name), None)
        if raw is None:
            return {"ok": False, "error": f"No trigger named {name!r}."}
        source = str(raw.get("source") or "")
        etype = str(raw.get("event") or "")
        payload: dict[str, Any] = {
            str(k): v for k, v in (fields or {}).items() if str(k).strip()
        }
        if body and "body" not in payload:
            payload["body"] = body
        engine.fire_test(name, Event.new(source, etype, payload))
        return {"ok": True}

    def get_trigger_board(self) -> list[dict[str, Any]]:
        """Snapshot of the run inbox's cards for this session (newest last)."""
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
            prev = self._trigger_cards.get(detail["card_id"], {}).get("column")
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
        # Audio cue once per trigger fire: only on the transition *into* the
        # "triggered" column (a new fire — something happened while you were away).
        # Approval is covered by the confirm cue and completion by the run-outcome
        # cues, so this stays one ping per fire (no per-mutation spam).
        if detail.get("column") == "triggered" and prev != "triggered":
            self._play_sound("trigger")

    # ── schedule run inbox + event log (mirrors the trigger hooks) ──────────────

    def get_schedule_events(self) -> list[dict[str, Any]]:
        """The Schedules event-audit feed (every scheduled fire + disposition)."""
        with self._lock:
            return list(self._schedule_events)

    def _on_schedule_event(self, record: dict[str, Any]) -> None:
        """Record a scheduled fire's disposition and push it to the Schedules
        event log. Mirrors :meth:`_on_trigger_event`; may run on a source thread."""
        with self._lock:
            self._schedule_events.append(record)
            if len(self._schedule_events) > 300:
                del self._schedule_events[: len(self._schedule_events) - 300]
        self._dispatch("runspec:schedule_event", record)

    def get_schedule_board(self) -> list[dict[str, Any]]:
        """Snapshot of the schedule run cards for this session (newest last)."""
        with self._lock:
            cards = list(self._schedule_cards.values())
        return sorted(cards, key=lambda c: c.get("created_at", 0.0))

    def _on_schedule_card(self, card: dict[str, Any]) -> None:
        """Record/advance a schedule run card and push it to the UI. The card is a
        plain dict (TriggerCard.to_dict() shape) built by :meth:`_invoke_scheduled`,
        so this just stores + bounds + dispatches. Kept separate from the trigger
        board so schedule runs never surface in the Triggers tab or its badge."""
        with self._lock:
            self._schedule_cards[card["card_id"]] = card
            if len(self._schedule_cards) > 200:
                oldest = sorted(
                    self._schedule_cards.values(),
                    key=lambda c: c.get("created_at", 0.0),
                )[:50]
                for old in oldest:
                    self._schedule_cards.pop(old["card_id"], None)
        self._dispatch("runspec:schedule_card", dict(card))

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
        action_allow: list[str] | None = None,
        record: Callable[[str], None] | None = None,
        stage: Callable[[str], None] | None = None,
        trigger: str = "",
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
        # An agent trigger turn IS an LLM run; tag every runnable it calls with
        # the causing event source (agent=True). Direct chat turns register no
        # origin and default to the same agent=True with no trigger.
        self._set_run_origin(chat_id, trigger, agent=True)
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
                    action_allow=action_allow,
                    record=record,
                    stage=stage,
                )
            )
        finally:
            self._clear_run_origin(chat_id)
            with self._lock:
                self._chat_cancels.pop(chat_id, None)

    def _group_calls_level(self, calls: list[tuple[str, dict[str, Any]]]) -> str:
        """The autonomy a batch of calls must honour: the most restrictive across
        the steps (a ``manual`` step makes the whole group manual). Empty → run."""
        if not calls:
            return "autonomous"
        rank = max(
            _AUTONOMY_RANK.get(self._effective_tool_autonomy(n, i), 1) for n, i in calls
        )
        return next(k for k, v in _AUTONOMY_RANK.items() if v == rank)

    @staticmethod
    def _apply_group_edit(
        edited: dict[str, Any] | None,
        calls: list[tuple[str, dict[str, Any]]],
    ) -> list[tuple[str, dict[str, Any]]]:
        """Replace ``calls`` with an operator-edited ``run_group`` payload's calls
        (when the UI returned one); otherwise keep the proposed calls."""
        if edited is not None and isinstance(edited.get("calls"), list):
            edited_calls = [
                (str(c.get("name")), dict(c.get("input") or {}))
                for c in edited["calls"]
                if c.get("name")
            ]
            if edited_calls:
                return edited_calls
        return calls

    def _grouped_confirm_run_sync(
        self, chat_id: str, calls: list[tuple[str, dict[str, Any]]], *, noun: str
    ) -> list[str]:
        """Most-restrictive grouped confirm, then run each ``(tool_name, input)``
        via ``_run_tool_sync`` WITHOUT re-gating (the one confirm authorises them
        all). Returns one output per call. Does NOT manage run provenance — the
        caller sets it. ``noun`` names the batch in the decline/refuse messages."""
        if not calls:
            return []
        level = self._group_calls_level(calls)
        if level == "manual":
            return [
                f"✗ '{n.split('__', 1)[-1]}' is manual-only — the {noun} "
                "was not run (a manual step blocks the whole group)."
                for n, _ in calls
            ]
        if level != "autonomous":
            payload = {"calls": [{"name": n, "input": i} for n, i in calls]}
            request_id, event, holder = self._register_confirm(
                chat_id, "run_group", payload, level
            )
            timed_out = not event.wait(timeout=self._CONFIRM_TIMEOUT_S)
            approved, edited = self._finish_confirm(request_id, holder, timed_out)
            if not approved:
                return [f"✗ The user declined to run the {noun} (autonomy = {level})."]
            calls = self._apply_group_edit(edited, calls)
        return self._run_calls_parallel(calls, chat_id)

    def _run_calls_parallel(
        self, calls: list[tuple[str, dict[str, Any]]], chat_id: str
    ) -> list[str]:
        """Run a batch of authorised ``(tool_name, input)`` calls and return their
        outputs in order. A single call runs inline; a batch fans out over a
        bounded thread pool (each host reuses its own pooled SSH transport, so the
        cap just bounds concurrent channels — not a new handshake per call)."""
        if len(calls) <= 1:
            return [self._run_tool_sync(n, i, chat_id) for n, i in calls]
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(
            max_workers=min(len(calls), 8), thread_name_prefix="fanout"
        ) as ex:
            return list(
                ex.map(lambda c: self._run_tool_sync(c[0], c[1], chat_id), calls)
            )

    async def _grouped_confirm_run_async(
        self,
        chat_id: str,
        calls: list[tuple[str, dict[str, Any]]],
        *,
        noun: str,
        stage: Callable[[str], None] | None = None,
    ) -> list[str]:
        """Async sibling of :meth:`_grouped_confirm_run_sync` for the chat agent
        loop. The confirm prompt is registered on the event-loop thread (dispatch
        from a worker thread is unreliable — see :meth:`_gated_run_tool_async`);
        only the blocking wait and the runs are offloaded."""
        if not calls:
            return []
        level = self._group_calls_level(calls)
        if level == "manual":
            return [
                f"✗ '{n.split('__', 1)[-1]}' is manual-only — the {noun} "
                "was not run (a manual step blocks the whole group)."
                for n, _ in calls
            ]
        if level != "autonomous":
            if stage is not None:
                stage("awaiting_human")
            payload = {"calls": [{"name": n, "input": i} for n, i in calls]}
            request_id, event, holder = self._register_confirm(
                chat_id, "run_group", payload, level
            )
            timed_out = not await asyncio.to_thread(event.wait, self._CONFIRM_TIMEOUT_S)
            approved, edited = self._finish_confirm(request_id, holder, timed_out)
            if holder.get("cancelled"):
                return [f"⏹ The {noun} was not run — you stopped the assistant."]
            if not approved:
                return [f"✗ The user declined to run the {noun} (autonomy = {level})."]
            calls = self._apply_group_edit(edited, calls)
        if stage is not None:
            stage("running")
        return await asyncio.to_thread(self._run_calls_parallel, calls, chat_id)

    def _run_rule_group(
        self,
        chat_id: str,
        calls: list[tuple[str, dict[str, Any]]],
        trigger: str = "",
    ) -> list[str]:
        """Run a rule-mode trigger's PARALLEL action list behind a single confirm.

        Group autonomy is the most restrictive across the steps (a ``manual`` step
        refuses the whole group); a single ``run_group`` confirm covers them all,
        and the operator may edit the group's calls before approving. Approved (or
        all-autonomous) steps run via ``_run_tool_sync`` WITHOUT re-gating (the
        group confirm already authorised them). Returns one output per call."""
        # A rule run is a deterministic (non-LLM) call — record agent=False with the
        # causing event source as provenance for each runnable in the group.
        self._set_run_origin(chat_id, trigger, agent=False)
        try:
            return self._grouped_confirm_run_sync(chat_id, calls, noun="parallel group")
        finally:
            self._clear_run_origin(chat_id)

    def _trigger_run_pre_turn(
        self, chat_id: str, instructions: str, event_text: str
    ) -> dict[str, Any]:
        """BridgeOps: one headless LLM extract step ahead of a rule action.

        It is given ONE output tool (``emit_params``) and NO runnable tools, so it
        can only return inferred params — it cannot act, loop, or branch (this is
        what keeps the pre-turn under the "no workflow engine" line). Returns the
        flat params dict, or ``{}`` on any failure (the action runs regardless)."""
        system = (
            "You extract structured parameters from an event for an automated "
            "action. Call emit_params exactly once with the values you infer from "
            "the event. Do not take any other action.\n\n" + (instructions or "")
        )
        try:
            adapter = self._get_adapter(system_override=system)
        except Exception:  # noqa: BLE001
            logger.debug("pre-turn: adapter unavailable", exc_info=True)
            return {}
        if adapter is None:
            return {}
        tool = {
            "name": "emit_params",
            "description": "Return the parameters you inferred from the event.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "params": {
                        "type": "object",
                        "description": "Flat map of param name → string value.",
                    }
                },
                "required": ["params"],
            },
        }
        history = [{"role": "user", "content": event_text}]

        async def _once() -> dict[str, Any]:
            response: Any = None
            async for ev in adapter.stream_with_tools(history, [tool]):
                if ev[0] == "done":
                    response = ev[1]
            if response is None or not getattr(response, "tool_calls", None):
                return {}
            for tc in response.tool_calls:
                if tc.name == "emit_params":
                    params = (tc.input or {}).get("params")
                    if isinstance(params, dict):
                        return {str(k): v for k, v in params.items()}
            return {}

        try:
            return asyncio.run(_once())
        except Exception:  # noqa: BLE001
            logger.debug("pre-turn: extraction failed", exc_info=True)
            return {}

    async def _run_isolated_agent_turn(
        self,
        chat_id: str,
        message: str,
        adapter: Any,
        reply_to: str | None = None,
        room: str | None = None,
        from_mailbox: str | None = None,
        allowed_runnables: list[str] | None = None,
        action_allow: list[str] | None = None,
        record: Callable[[str], None] | None = None,
        stage: Callable[[str], None] | None = None,
    ) -> str:
        """A headless agent loop for a trigger card. Like _agentic_chat_turn but
        against an isolated, throwaway history (never the shared chat) so a
        trigger never pollutes the user's conversation. Same toolset and the same
        autonomy gate (_gated_run_tool_async). When ``reply_to`` is set the
        built-in draft_reply tool is offered; when ``room`` is set the chat
        propose_reply tool is offered (both handled inline).

        ``action_allow`` (non-empty) restricts the built-in action + Outlook tools
        to that set — the operator's per-trigger choice of which actions this
        trigger may take; runnable tools are still governed by ``allowed_runnables``.

        Returns a short summary (the model's closing reply + any tool actions)
        for display on the run card — trigger turns have no chat transcript,
        so this is where the agent's output becomes visible."""
        from .trigger_runner import draft_reply_schema, propose_reply_schema

        start = time.monotonic()
        tools = self._runnables_to_tools(allow=allowed_runnables)
        # Search mode hides runnables behind search_runnables; fold the same
        # always-on menu the chat turn gets so an unrestricted trigger/schedule
        # agent turn discovers what exists identically. Only when search mode is
        # actually active for this turn (no allowlist — an allow-scoped turn falls
        # back to dedup, which carries full schemas inline).
        index_text = (
            self._build_catalog_index()
            if (self._tool_mode() == "search" and not allowed_runnables)
            else ""
        )
        if action_allow:
            # Keep every non-governed tool; among the governed built-in action /
            # Outlook tools keep only the allowlisted ones.
            governed = _ACTION_TOOL_NAMES | _OUTLOOK_TOOL_NAMES
            allow = set(action_allow)
            tools = [
                t for t in tools if t["name"] not in governed or t["name"] in allow
            ]
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
                # A per-call copy with the catalog index folded into the first user
                # message (search mode only); the stored history stays clean.
                model_messages = self._fold_catalog_index(history, index_text)
                async for event in adapter.stream_with_tools(model_messages, tools):
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
        approved, edited = self._finish_confirm(request_id, holder, timed_out)
        if holder.get("cancelled"):
            return "⏹ draft_reply was not run — you stopped the assistant."
        if not approved:
            # Declined, but the turn continues — surface that as 'running' rather
            # than leaving the card stuck in 'awaiting_human'.
            if stage is not None:
                stage("running")
            return "✗ You declined to save the draft reply."
        # The operator may have edited the reply body before approving.
        if edited is not None and str(edited.get("body") or "").strip():
            body = str(edited["body"]).strip()
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
        approved, edited = self._finish_confirm(request_id, holder, timed_out)
        if holder.get("cancelled"):
            return "⏹ propose_reply was not run — you stopped the assistant."
        if not approved:
            # Declined, but the turn continues — surface that as 'running' rather
            # than leaving the card stuck in 'awaiting_human'.
            if stage is not None:
                stage("running")
            return "✗ You declined to propose the reply."
        # The operator may have edited the proposed text before approving.
        if edited is not None and str(edited.get("text") or "").strip():
            text = str(edited["text"]).strip()
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
