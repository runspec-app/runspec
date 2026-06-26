"""bridge_autonomy.py — AutonomyMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any


from . import bridge_utils as utils_mod
from . import config as config_mod
from . import executor as executor_mod
from . import hosts as hosts_mod
from . import notify
from . import sounds
from .config import (
    confirm_notifications_enabled,
)

from .bridge_const import (
    logger,
    _AUTONOMY_RANK,
    _CUSTOM_TOOL_NAMES,
    _READONLY_META,
    _MEMORY_TOOL_NAMES,
    _STATUS_TOOL_NAMES,
    _OUTLOOK_READ_TOOLS,
    _OUTLOOK_WRITE_TOOLS,
    _OUTLOOK_TOOL_NAMES,
    _REPO_READ_TOOLS,
    _REPO_WRITE_TOOLS,
    _REPO_TOOL_NAMES,
    _ACTION_TOOL_NAMES,
    _AGENT_TOOL_OUTPUT_CAP,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


class AutonomyMixin:
    def _effective_tool_autonomy(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> str:
        """Autonomy the agent must honour for this call: the runnable's level,
        escalated by any provided arg's per-arg autonomy (most restrictive wins).
        Unknown runnable → 'confirm' (gated by default — never silently run)."""
        if tool_name == "manage_denylist":
            # Reading the denylist (action='list') is read-only — let the agent
            # consult it freely so it always knows what's disabled; mutations
            # (add/all/clear/mode) stay confirm-gated.
            action = str((tool_input or {}).get("action") or "").lower()
            return "autonomous" if action == "list" else "confirm"
        if tool_name in _OUTLOOK_READ_TOOLS:
            return "autonomous"  # read-only Outlook reads (local COM) — no prompt
        if tool_name in _OUTLOOK_WRITE_TOOLS:
            return "confirm"  # draft/send mail — always prompt (send is irreversible)
        if tool_name in _REPO_READ_TOOLS:
            return "autonomous"  # read-only repo reads (local checkout) — no prompt
        if tool_name in _REPO_WRITE_TOOLS:
            return "confirm"  # clone/fetch+checkout or local file write — always prompt
        if tool_name in _CUSTOM_TOOL_NAMES:
            return "confirm"  # side-effecting file move — always prompt
        if tool_name in _READONLY_META:
            return "autonomous"  # search/describe are read-only discovery
        if tool_name in _MEMORY_TOOL_NAMES:
            return "autonomous"  # local memory/runbook store — no model-spend gate
        if tool_name in _STATUS_TOOL_NAMES:
            return "autonomous"  # read-only status report — no side effects
        if tool_name in _ACTION_TOOL_NAMES:
            return "confirm"  # side-effecting action tools — always prompt
        if tool_name == "run_runnable":
            # Resolve the inner runnable so the gate reflects what actually runs —
            # not the generic wrapper. The level + per-arg escalation come from the
            # resolved runnable and the call's `args`, exactly as for a direct call.
            ti = dict(tool_input or {})
            resolved = self._resolve_id_host(
                str(ti.get("id") or ""), str(ti.get("host") or "")
            )
            if isinstance(resolved, str):
                return "confirm"
            _host, _name, r = resolved
            inner_args = dict(ti.get("args") or {})
        else:
            target = self._resolve_tool_target(tool_name, tool_input)
            if isinstance(target, str):
                return "confirm"  # unresolved (bad host / unknown tool) → gated
            _host, _name, r, _host_key = target
            inner_args = tool_input or {}
        if not r:
            return "confirm"
        level = r.get("autonomy") or "confirm"
        for arg in r.get("args") or []:
            if arg.get("name") in inner_args and arg.get("autonomy"):
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
    ) -> tuple[str, threading.Event, dict[str, Any]]:
        """Register a pending confirmation and emit the UI prompt event.

        Returns (request_id, event, holder). The event is set by
        resolve_tool_confirmation() when the UI answers."""
        # The confirm event carries TWO name/input pairs that serve different
        # roles, and conflating them is a bug:
        #   * tool_name / tool_input  — the *raw* tool call, kept verbatim so the
        #     frontend can match this confirm to the running tool-call segment
        #     (segment.entry.name == tc.name, set by the runspec:tool_start event).
        #   * display_name / display_input — the human-readable view from
        #     _confirm_card_view: for the search-mode `run_runnable` wrapper this
        #     unwraps to the real runnable + args the operator is approving.
        # Sending the unwrapped name as tool_name (the old behaviour) made it
        # `run_runnable` != resolved-name, so the segment never matched and the
        # Approve/Deny buttons never rendered for search-mode calls.
        card_tool, card_input = self._confirm_card_view(tool_name, tool_input)
        request_id = uuid.uuid4().hex
        event = threading.Event()
        # ``edited_input`` (Phase: editable confirmations) carries an operator-edited
        # tool_input back from the UI — None means "run as proposed".
        holder: dict[str, Any] = {
            "approved": False,
            "cancelled": False,
            "edited_input": None,
        }
        with self._lock:
            self._pending_confirms[request_id] = (chat_id, event, holder)
            self._pending_confirm_meta[request_id] = {
                "id": chat_id,
                "request_id": request_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "display_name": card_tool,
                "display_input": card_input,
                "autonomy": level,
                "created_at": time.time(),
            }
        logger.info(
            "autonomy gate: prompting for %s (autonomy=%s) request_id=%s",
            card_tool,
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
                "display_name": card_tool,
                "display_input": card_input,
                "autonomy": level,
            },
        )
        self._notify_confirm(card_tool, card_input, level)
        return request_id, event, holder

    def _confirm_card_view(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """The (tool_name, tool_input) to *display* on the confirm card. Identity
        for normal calls; for ``run_runnable`` it unwraps to the resolved runnable
        name + its real args (so the operator approves what actually runs)."""
        if tool_name != "run_runnable":
            return tool_name, tool_input
        ti = dict(tool_input or {})
        resolved = self._resolve_id_host(
            str(ti.get("id") or ""), str(ti.get("host") or "")
        )
        if isinstance(resolved, str):
            return tool_name, tool_input  # unresolved — show the raw call
        host, name, _cached = resolved
        view: dict[str, Any] = {"host": host}
        view.update(dict(ti.get("args") or {}))
        if ti.get("command"):
            view["command"] = ti["command"]
        if ti.get("credential_id"):
            view["credential_id"] = ti["credential_id"]
        return name, view

    # ── desktop notification for confirmation events ──────────────────────────
    #
    # The in-app Approve/Deny card only helps if the window is in focus. When a
    # tool call parks on the autonomy gate we also raise an OS toast + flash the
    # taskbar so the user is pulled back to answer (it otherwise times out as a
    # deny after _CONFIRM_TIMEOUT_S). Sending a toast is independent of the
    # "read notifications" consent the listener source needs — see notify.py.

    def set_window_title(self, title: str) -> None:
        """Record the native window title app.py gave ``create_window``.

        The taskbar flash / focus helpers below locate the window by title
        (``FindWindow``), so this must stay byte-identical to what app.py passed —
        both come from the same ``resolve_branding().title`` call in main()."""
        if isinstance(title, str) and title.strip():
            self._window_title = title

    def _confirm_notifications_enabled(self) -> bool:
        try:
            return confirm_notifications_enabled()
        except Exception:
            return True

    def _play_sound(self, category: str, *, force: bool = False) -> bool:
        """Best-effort audio cue — never raises, no-op off Windows / when muted."""
        try:
            return sounds.play(category, force=force)
        except Exception:
            logger.debug("play sound failed (category=%s)", category, exc_info=True)
            return False

    def _notify_confirm(
        self, tool_name: str, tool_input: dict[str, Any], level: str
    ) -> None:
        """Best-effort desktop toast + taskbar flash + audio cue for a pending
        confirm.

        The opt-out gate runs synchronously; the toast/flash/sound is offloaded so
        the token-streaming thread isn't blocked. **The toast depends on the
        ``[notifications]`` extra (winrt), but the taskbar flash (pywin32, a core
        dep) and the audio cue do NOT** — so they fire even when toasts are
        unavailable. Any failure is swallowed — a missed cue must never disturb the
        confirmation flow."""
        try:
            if not self._confirm_notifications_enabled():
                return
            toast_ok, _ = notify.available()
            # Only build the toast text when we can actually show one; the flash +
            # sound below don't need it.
            title, body = (
                notify.build_confirm_message(tool_name, tool_input)
                if toast_ok
                else ("", "")
            )
        except Exception:
            logger.debug("notify_confirm setup failed", exc_info=True)
            return

        def _raise() -> None:
            try:
                if toast_ok:
                    notify.notify(title, body, on_click=self._focus_window)
                self._flash_window()
                self._play_sound("confirm")
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

            hwnd = win32gui.FindWindow(None, self._window_title)
            if not hwnd:
                # The native window title didn't match self._window_title — flash
                # (and focus) can't locate the window. Surfaced by the Settings
                # "Test" button's window_found=False.
                logger.debug("flash window: no window titled %r", self._window_title)
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

            hwnd = win32gui.FindWindow(None, self._window_title)
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

            hwnd = win32gui.FindWindow(None, self._window_title)
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
            if self._window_is_foreground():
                return
            try:
                if not config_mod.room_notifications_enabled():
                    return
            except Exception:
                pass  # config unreadable → default to notifying
            toast_ok, _ = notify.available()
            title, body = notify.build_room_message(payload) if toast_ok else ("", "")
        except Exception:
            logger.debug("notify_room setup failed", exc_info=True)
            return

        def _raise() -> None:
            try:
                # Toast needs the [notifications] extra; flash + sound don't.
                if toast_ok:
                    notify.notify(title, body, on_click=self._focus_window)
                self._flash_window()
                self._play_sound("message")
            except Exception:
                logger.debug("desktop room notify failed", exc_info=True)

        threading.Thread(target=_raise, name="notify-room", daemon=True).start()

    def _window_found(self) -> bool:
        """Whether the native window can be located by title (the flash/focus
        prerequisite). Windows-only; False elsewhere / on error."""
        if sys.platform != "win32":
            return False
        try:
            import win32gui

            return bool(win32gui.FindWindow(None, self._window_title))
        except Exception:
            return False

    def test_notification(self) -> dict[str, Any]:
        """Fire a test toast + taskbar flash + confirm sound so the operator can
        verify each attention channel from Settings → Sounds & Notifications.
        Returns which channels are available; ``window_found`` surfaces the
        title-match prerequisite for the flash/focus. Best-effort; never raises."""
        toast_available, toast_reason = notify.available()
        sound_available, _ = sounds.available()
        window_found = self._window_found()

        def _raise() -> None:
            try:
                if toast_available:
                    notify.notify(
                        self._window_title or "runspec console",
                        "Test notification — if you can see this, toasts work.",
                        on_click=self._focus_window,
                    )
                self._flash_window()
                self._play_sound("confirm", force=True)
            except Exception:
                logger.debug("test notification failed", exc_info=True)

        threading.Thread(target=_raise, name="notify-test", daemon=True).start()
        return {
            "toast_available": toast_available,
            "toast_reason": toast_reason,
            "sound_available": sound_available,
            "window_found": window_found,
        }

    def _finish_confirm(
        self, request_id: str, holder: dict[str, Any], timed_out: bool
    ) -> tuple[bool, dict[str, Any] | None]:
        """Clean up a resolved confirm and return ``(approved, edited_input)``.

        ``edited_input`` is the operator-edited tool_input (Phase: editable
        confirmations) or ``None`` to run the call as the agent proposed it."""
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
        edited = holder.get("edited_input") if holder["approved"] else None
        return holder["approved"], edited

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
        if tool_name == "run_runnable":
            fan = self._group_fanout_calls(tool_input)
            if fan is not None:
                group, name, calls = fan
                if not calls:
                    return f"✗ No host in group '{group}' has '{name}'."
                outs = await self._grouped_confirm_run_async(
                    chat_id, calls, noun=f"group '{group}' run", stage=stage
                )
                return self._format_group_output(group, name, calls, outs)
        decision = self._confirm_decision(tool_name, tool_input)
        display = self._confirm_card_view(tool_name, tool_input)[0].split("__", 1)[-1]
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
            approved, edited = self._finish_confirm(request_id, holder, timed_out)
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
            # The operator may have edited the args before approving — run those.
            if edited is not None:
                tool_input = edited
        if stage is not None:
            stage("running")
        return await asyncio.to_thread(
            self._run_tool_sync, tool_name, tool_input, chat_id
        )

    def _gated_run_tool(
        self, tool_name: str, tool_input: dict[str, Any], chat_id: str
    ) -> str:
        """Synchronous autonomy gate (kept for direct/unit-test use).

        autonomous → run; confirm/supervised → ask the user (Approve/Deny);
        manual → refuse (human-only; the user must use the Run button)."""
        if tool_name == "run_runnable":
            fan = self._group_fanout_calls(tool_input)
            if fan is not None:
                group, name, calls = fan
                if not calls:
                    return f"✗ No host in group '{group}' has '{name}'."
                outs = self._grouped_confirm_run_sync(
                    chat_id, calls, noun=f"group '{group}' run"
                )
                return self._format_group_output(group, name, calls, outs)
        decision = self._confirm_decision(tool_name, tool_input)
        display = self._confirm_card_view(tool_name, tool_input)[0].split("__", 1)[-1]
        if decision == "manual":
            return f"✗ '{display}' is manual-only (autonomy = manual) and cannot be run by the assistant. Ask the user to run it themselves from the Run button."
        if decision != "run":
            approved, edited = self._await_confirmation(
                tool_name, tool_input, decision, chat_id
            )
            if not approved:
                return (
                    f"✗ The user declined to run '{display}' (autonomy = {decision})."
                )
            if edited is not None:
                tool_input = edited
        return self._run_tool_sync(tool_name, tool_input, chat_id)

    def _set_run_origin(
        self, chat_id: str, trigger: str, agent: bool, runbook: str = ""
    ) -> None:
        """Register a turn's run provenance so _run_tool_sync can tag its runnable
        runs (the trigger source, whether it's an LLM/agent turn, and the runbook that
        drove it when invoked via run_runbook). Keyed by the card/chat id; cleared with
        _clear_run_origin in the handler's finally."""
        if not chat_id:
            return
        with self._lock:
            self._run_origin[chat_id] = {
                "trigger": trigger,
                "agent": agent,
                "runbook": runbook,
            }

    def _clear_run_origin(self, chat_id: str) -> None:
        with self._lock:
            self._run_origin.pop(chat_id, None)

    def _await_confirmation(
        self, tool_name: str, tool_input: dict[str, Any], level: str, chat_id: str
    ) -> tuple[bool, dict[str, Any] | None]:
        """Ask the UI to approve an agent tool call; block (in a worker thread)
        until resolve_tool_confirmation() fires. Times out as a deny. Returns
        ``(approved, edited_input)``."""
        request_id, event, holder = self._register_confirm(
            chat_id, tool_name, tool_input, level
        )
        timed_out = not event.wait(timeout=self._CONFIRM_TIMEOUT_S)
        return self._finish_confirm(request_id, holder, timed_out)

    def resolve_tool_confirmation(
        self,
        request_id: str,
        approved: bool,
        edited_input: dict[str, Any] | None = None,
    ) -> None:
        """Frontend callback: resolve a pending agent tool-call confirmation.

        ``edited_input`` (optional) is an operator-edited tool_input — when
        approving, the call runs with these values instead of the agent's proposal
        (Phase: editable confirmations). Ignored on a deny."""
        logger.info(
            "autonomy gate: UI resolved request_id=%s approved=%s edited=%s",
            request_id,
            approved,
            edited_input is not None,
        )
        with self._lock:
            entry = self._pending_confirms.get(request_id)
        if entry is not None:
            _chat_id, event, holder = entry
            holder["approved"] = bool(approved)
            if approved and isinstance(edited_input, dict):
                holder["edited_input"] = edited_input
            event.set()
        else:
            logger.warning(
                "autonomy gate: resolve for unknown request_id=%s (already timed out?)",
                request_id,
            )

    def _run_tool_sync(
        self, tool_name: str, tool_input: dict[str, Any], chat_id: str = ""
    ) -> str:
        """Blocking runnable execution — call via asyncio.to_thread from the agentic loop.

        ``chat_id`` keys the turn's run provenance (set by the trigger handler):
        a rule trigger run is recorded with its source and ``agent=False`` (not an
        LLM run), an agent trigger run with its source and ``agent=True``; a direct
        chat turn (no registered origin) defaults to an agent run with no trigger.
        """
        if tool_name in _CUSTOM_TOOL_NAMES:
            return self._run_custom_tool(tool_name, dict(tool_input or {}))
        if tool_name in _OUTLOOK_TOOL_NAMES:
            return self._run_outlook_tool(tool_name, dict(tool_input or {}))
        if tool_name in _REPO_TOOL_NAMES:
            return self._run_repo_tool(tool_name, dict(tool_input or {}))
        if tool_name in _MEMORY_TOOL_NAMES:
            return self._run_memory_tool(tool_name, dict(tool_input or {}))
        if tool_name in _STATUS_TOOL_NAMES:
            return self._run_status_tool(dict(tool_input or {}))
        if tool_name in _ACTION_TOOL_NAMES:
            return self._run_action_tool(tool_name, dict(tool_input or {}), chat_id)
        # Read-only discovery meta-tools (search mode) — no side effects, return JSON.
        if tool_name == "search_runnables":
            return self._run_search_tool(dict(tool_input or {}))
        if tool_name == "describe_runnable":
            return self._run_describe_tool(dict(tool_input or {}))
        if tool_name == "run_runnable":
            # The generic executor (search mode): resolve {id, host} → runnable, then
            # validate the free-form args against the cached spec before dispatch.
            # Search surfaces no per-runnable JSON schema, so we pre-flight here to
            # catch mistakes in the same turn instead of after an SSH round-trip.
            ti = dict(tool_input or {})
            resolved = self._resolve_id_host(
                str(ti.get("id") or ""), str(ti.get("host") or "")
            )
            if isinstance(resolved, str):
                return resolved
            host, runnable, cached_r = resolved
            call_args = dict(ti.get("args") or {})
            chosen_command = ti.get("command")
            credential_id = str(ti.get("credential_id") or "")
            command_path = (
                [chosen_command]
                if isinstance(chosen_command, str) and chosen_command
                else []
            )
            preflight = self._preflight_validate(cached_r, command_path, call_args)
            if preflight:
                return preflight
        else:
            target = self._resolve_tool_target(tool_name, tool_input)
            if isinstance(target, str):
                return target  # routing error (bad/missing host, unknown tool)
            host, runnable, cached_r, host_key = target
            # `command` (when present) names the subcommand to descend into — pull it
            # out of the args and pass it as the command path. Core validates it and
            # enforces require-command, so an unknown/missing command surfaces a clear
            # "Available commands: …" error the agent can read and retry from.
            call_args = dict(tool_input or {})
            if host_key:
                call_args.pop(host_key, None)  # host selector, not a runnable arg
            chosen_command = call_args.pop("command", None)
            # An explicit credential the agent chose to apply (in-scope ones apply
            # automatically regardless). Pulled out so it isn't treated as an arg.
            credential_id = str(call_args.pop("credential_id", "") or "")
            command_path = (
                [chosen_command]
                if isinstance(chosen_command, str) and chosen_command
                else []
            )
        entry = self._host_entry(host)
        if entry is None:
            return f"Error: host '{host}' not found"
        output_lines: list[str] = []
        exit_code_holder = [0]

        def on_line(line: str, stream: str) -> None:
            output_lines.append(line)

        def on_done(exit_code: int, duration_ms: int) -> None:
            exit_code_holder[0] = exit_code

        tool_paths = utils_mod._paths(entry)
        run_group = cached_r.get("group") if cached_r else None
        rp = next(
            (p for p in tool_paths if hosts_mod.venv_name(p) == run_group),
            tool_paths[0] if tool_paths else "",
        )
        run_as, become_method, become_flags = self._become_params(cached_r, host)
        # password-typed args must never become argv — on the remote they'd leak
        # into ps / shell history. Split them into the secret env channel exactly
        # as the manual path (invoke_runnable) does; the executor injects them as
        # RUNSPEC_*_ARG_* env vars instead.
        secret_names = self._secret_arg_names(cached_r, command_path)
        secrets = {
            k: v
            for k, v in call_args.items()
            if k in secret_names and v not in (None, "")
        }
        exec_args = {k: v for k, v in call_args.items() if k not in secret_names}
        # Fill bound credential values into args from secure storage (the agent
        # keeps arg-binding: in-scope credentials auto-apply, credential_id
        # overrides). Unlike the human/form path, the agent may inject into args.
        self._apply_credentials(
            host,
            runnable,
            exec_args,
            secrets,
            explicit_id=credential_id,
            valid_args=self._declared_arg_names(cached_r, command_path),
            secret_args=secret_names,
        )
        # …and ALSO inject the name-derived plain env vars (e.g. JIRA_USERNAME) so
        # env-only runnables (jsm/snow style) work for the agent. In-scope
        # credentials apply automatically; an explicit credential_id is merged in
        # (it may be out of scope).
        cred_env, _ = self._credential_env(host, runnable, ids=None)
        if credential_id:
            explicit_env, _ = self._credential_env(host, runnable, [credential_id])
            cred_env.update(explicit_env)
        # Run provenance for this turn (set by the trigger handler; absent → a
        # direct chat agent run). `agent` distinguishes an LLM turn (chat / agent
        # trigger) from a fixed rule run; `trigger` is the causing event source.
        with self._lock:
            origin = getattr(self, "_run_origin", {}).get(chat_id, {})
        run_agent = bool(origin.get("agent", True))
        run_trigger = str(origin.get("trigger", ""))
        # The saved runbook that drove this turn (set by run_runbook) → tags each
        # runnable's run_summary as extra.runbook for History/Analytics attribution.
        run_runbook = str(origin.get("runbook", ""))
        ssh = entry.get("ssh")
        if ssh:
            executor_mod.run_remote(
                ssh,
                rp,
                runnable,
                exec_args,
                command_path,
                on_line,
                on_done,
                entry.get("identityFile"),
                timeout=120,
                agent=run_agent,
                global_ssh_config=self.get_config().get("ssh"),
                run_as=run_as,
                become_method=become_method,
                become_flags=become_flags,
                positionals=self._positionals_for(cached_r, command_path),
                secrets=secrets,
                cred_env=cred_env,
                trigger=run_trigger,
                runbook=run_runbook,
            )
        else:
            executor_mod.run_local(
                rp,
                runnable,
                exec_args,
                command_path,
                on_line,
                on_done,
                timeout=120,
                agent=run_agent,
                run_as=run_as,
                become_method=become_method,
                become_flags=become_flags,
                positionals=self._positionals_for(cached_r, command_path),
                secrets=secrets,
                cred_env=cred_env,
                trigger=run_trigger,
                runbook=run_runbook,
            )

        output = "\n".join(output_lines)
        if len(output) > _AGENT_TOOL_OUTPUT_CAP:
            output = (
                output[:_AGENT_TOOL_OUTPUT_CAP]
                + f"\n[output truncated at {_AGENT_TOOL_OUTPUT_CAP} chars — "
                "incomplete; any JSON above is cut off mid-structure]"
            )
        if exit_code_holder[0] != 0:
            return f"[exit {exit_code_holder[0]}]\n{output}"
        return output or "(no output)"

    # ── search mode (tool_mode = "search") ────────────────────────────────────
