"""bridge_autonomy.py — AutonomyMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from . import observability
from . import sounds
from .config import (
    confirm_notifications_enabled,
)

from .bridge_const import (
    logger,
    _AUTONOMY_RANK,
    _CANCEL_KEY,
    _CONFIRM_VERBS,
    _CONFIRM_VERB_DEFAULT,
    _CUSTOM_TOOL_NAMES,
    _READONLY_META,
    _MEMORY_TOOL_NAMES,
    _RUNBOOK_READ_TOOL_NAMES,
    _STATUS_TOOL_NAMES,
    _HISTORY_TOOL_NAMES,
    _FILES_TOOL_NAMES,
    _WEB_TOOL_NAMES,
    _IMPROVE_TOOL_NAMES,
    _OUTLOOK_READ_TOOLS,
    _OUTLOOK_WRITE_TOOLS,
    _OUTLOOK_TOOL_NAMES,
    _REPO_READ_TOOLS,
    _REPO_WRITE_TOOLS,
    _REPO_TOOL_NAMES,
    _ACTION_TOOL_NAMES,
    _CONFIG_TOOL_NAMES,
    _TOOL_HEARTBEAT_SECS,
    _WORKSPACE_TOOL_NAMES,
    _EXEC_TOOL_NAMES,
    _BUILTIN_TOOL_NAMES,
    _TOOL_OUTPUT_SPILL_THRESHOLD,
    _SPILL_ENVELOPE_HEAD_BYTES,
    ToolCancelled,
)
from .bridge_utils import make_truncation_suffix

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


def _command_path(inner_args: dict[str, Any]) -> list[str]:
    """Normalise the call's chosen subcommand to a path, mirroring
    ``_run_tool_sync``: a ``str`` → one-element path, a ``list`` → the path as-is
    (a saved run stores a possibly-nested path), missing/empty → ``[]``."""
    chosen = (inner_args or {}).get("command")
    if isinstance(chosen, list):
        return [str(c) for c in chosen if str(c)]
    if isinstance(chosen, str) and chosen:
        return [chosen]
    return []


def _command_leaf_node(
    cached: dict[str, Any], command_path: list[str]
) -> dict[str, Any]:
    """The runnable node reached by walking ``command_path`` into ``cached``'s
    ``commands``. A bad/absent segment stops the walk and returns the last valid
    node (fail-safe — never raises). With an empty path the leaf *is* ``cached``,
    so non-subcommand runnables resolve exactly as before."""
    node = cached or {}
    for seg in command_path:
        nxt = (node.get("commands") or {}).get(seg)
        if not isinstance(nxt, dict):
            break
        node = nxt
    return node


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
        if tool_name == "describe_tool":
            return "autonomous"  # read-only schema lookup for deferred built-ins
        if tool_name == "use_tool":
            # Resolve the deferral wrapper so the gate reflects the INNER tool's
            # level — exactly like run_runnable below. Unresolvable (unknown /
            # hot / nested name) → gated by default (fail closed).
            unwrapped = self._unwrap_use_tool(tool_input)
            if isinstance(unwrapped, str):
                return "confirm"
            inner_name, inner_ti = unwrapped
            return self._effective_tool_autonomy(inner_name, inner_ti)
        if tool_name == "run_runbook":
            # Reading a procedure is not an action. run_runbook returns *text* —
            # the runbook's rendered steps — and every side effect happens later,
            # when the agent calls the runnables those steps name, each at its own
            # autonomy level. Gating the read protected nothing downstream, cost a
            # prompt on a common path, and (while load_runbook existed) was routed
            # around by an ungated tool returning the same body. It stays in
            # _ACTION_TOOL_NAMES so a trigger's action_allow can still scope it.
            return "autonomous"
        if tool_name == "save_runbook":
            # The one memory-store write the operator must see. A note the agent
            # remembers is advisory; a runbook is a *procedure* it (and every synced
            # console) will later follow, so an unreviewed edit propagates. Special-
            # cased ahead of _MEMORY_TOOL_NAMES rather than removed from that
            # frozenset — the set is also the dispatch key (_run_tool_sync) and the
            # search-mode hot-kernel key, so removing it would strand the tool.
            return "confirm"
        if tool_name == "forget_memory":
            # Deleting a remembered fact is destructive (the content is gone), and the
            # operator explicitly drives the prune ("remove the ones I agree to"), so
            # each forget is confirmed. Same special-case shape as save_runbook: kept
            # in _MEMORY_TOOL_NAMES (the dispatch + hot-kernel key), gated here.
            return "confirm"
        if tool_name in _MEMORY_TOOL_NAMES:
            return "autonomous"  # local memory/runbook store — no model-spend gate
        if tool_name in _RUNBOOK_READ_TOOL_NAMES:
            return "autonomous"  # read-only runbook inspection — no side effects
        if tool_name in _STATUS_TOOL_NAMES:
            return "autonomous"  # read-only status report — no side effects
        if tool_name in _HISTORY_TOOL_NAMES:
            return "autonomous"  # read-only history search — no side effects
        if tool_name in _FILES_TOOL_NAMES:
            return "autonomous"  # read-only directory listing — no side effects
        if tool_name in _WEB_TOOL_NAMES:
            return "autonomous"  # read-only web GET — no side effects
        if tool_name in _IMPROVE_TOOL_NAMES:
            return "autonomous"  # read-only analysis of local transcripts/history
        if tool_name in _WORKSPACE_TOOL_NAMES:
            # read/write inside the current session's workspace only — no prompt
            return "autonomous"
        if tool_name == "run_python_sandboxed":
            # Arbitrary code inside the WASM sandbox (workspace-only, no network,
            # no host access) — still confirm-gated as operator governance on top
            # of the boundary. Always prompt.
            return "confirm"
        if tool_name == "run_sql_sandboxed":
            # Read-only SQL against a workspace sqlite db runs autonomously (it
            # can't mutate — opened mode=ro, in the sandbox); writes/DDL confirm.
            from . import codeexec

            q = str((tool_input or {}).get("query") or "")
            return "autonomous" if codeexec.classify_sql(q) == "read" else "confirm"
        if tool_name == "play_sound":
            # Playing an audio cue on the operator's own machine is benign and the
            # whole point is an unattended alert (a trigger/schedule/agent flagging
            # something worth hearing) — gating it behind a prompt would defeat that.
            # It stays in _ACTION_TOOL_NAMES so a trigger's action_allow can scope
            # it; the master [console.sounds] enabled toggle is the real off switch.
            return "autonomous"
        if tool_name in _ACTION_TOOL_NAMES:
            return "confirm"  # side-effecting action tools — always prompt
        if tool_name in _CONFIG_TOOL_NAMES:
            # Reading config (list/get) is read-only — free; every mutation
            # (add/update/remove/save/rename/enable/disable/set — and an
            # unknown/missing action, gated by default) prompts first.
            action = str((tool_input or {}).get("action") or "").lower()
            return "autonomous" if action in ("list", "get") else "confirm"
        if getattr(self, "_mcp", None) is not None and self._is_mcp_tool(tool_name):
            # External MCP tool — the level set on its server ([mcp] autonomy),
            # defaulting to confirm. An outside tool is a side effect until the
            # operator trusts the server, so it prompts unless opted down.
            return self._mcp.autonomy_for(tool_name)
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
            inner_args = self._coerce_run_args(ti.get("args"))
            # In search mode the chosen subcommand rides the run_runnable envelope
            # as a top-level `command` field (sibling to `args`), NOT inside
            # `args` — so read it from `ti`, exactly as _run_tool_sync (line ~1259)
            # and _confirm_card_view do. Reading it from `inner_args` (the
            # args-only dict) would always miss it and fall back to the top-level
            # runnable's autonomy.
            command_path = _command_path(ti)
        else:
            target = self._resolve_tool_target(tool_name, tool_input)
            if isinstance(target, str):
                return "confirm"  # unresolved (bad host / unknown tool) → gated
            _host, _name, r, _host_key = target
            inner_args = tool_input or {}
            # Dedup / per_host / saved-run calls carry `command` as a top-level
            # property of the call input.
            command_path = _command_path(inner_args)
        if not r:
            return "confirm"
        # Resolve autonomy from the *invoked subcommand leaf*, not the top-level
        # runnable — mirrors core parser._resolve_subcommand/effective_autonomy,
        # which reads the resolved leaf's own autonomy (a leaf's level replaces
        # the parent's; it is not escalated against it). Without a command the
        # leaf is the top node, so plain runnables are unchanged.
        leaf = _command_leaf_node(r, command_path)
        level = leaf.get("autonomy") or "confirm"
        # The leaf's args are the *effective* set (inherited globals + the
        # subcommand's own), so this one loop covers both global and
        # subcommand-scoped per-arg autonomy escalation.
        for arg in leaf.get("args") or []:
            if arg.get("name") in inner_args and arg.get("autonomy"):
                if _AUTONOMY_RANK.get(arg["autonomy"], 0) > _AUTONOMY_RANK.get(
                    level, 0
                ):
                    level = arg["autonomy"]
        return level

    def _confirm_timeout_s(self, headless: bool = False) -> float | None:
        """Seconds a confirm gate waits before treating no-answer as a deny;
        ``None`` = wait indefinitely (never auto-deny).

        Chat uses ``[console] confirm_timeout`` (default 300); unattended
        trigger/schedule turns use ``[console] trigger_confirm_timeout`` (default
        3600, since the operator may be away when the trigger fires). A config
        value of ``0`` means wait indefinitely."""
        t = config_mod.confirm_timeout(headless)
        return None if t <= 0 else t

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
        # The confirm verb follows the *resolved* tool actually being approved
        # (card_tool), so a wrapped/deferred save reads "save", not "run".
        confirm_verb = _CONFIRM_VERBS.get(card_tool, _CONFIRM_VERB_DEFAULT)
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
                "confirm_verb": confirm_verb,
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
                "confirm_verb": confirm_verb,
            },
        )
        self._notify_confirm(card_tool, card_input, level)
        return request_id, event, holder

    def _confirm_card_view(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """The (tool_name, tool_input) to *display* on the confirm card. Identity
        for normal calls; for the ``run_runnable`` / ``use_tool`` wrappers it
        unwraps to the resolved runnable / inner built-in name + its real args
        (so the operator approves what actually runs). A differing display name
        also makes the frontend render the confirm read-only — edits against a
        wrapper's display view would re-wrap lossily."""
        if tool_name == "use_tool":
            unwrapped = self._unwrap_use_tool(tool_input)
            if isinstance(unwrapped, str):
                return tool_name, tool_input  # unresolved — show the raw call
            return unwrapped
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
        view.update(self._coerce_run_args(ti.get("args")))
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
    # deny after the confirm timeout). Sending a toast is independent of the
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

    def _connection_cue(self, kind: str, name: str, connected: bool) -> None:
        """Audio cue for a host/room connection flip, debounced through the
        ConnectionCueGate (first observation silent, per-key cooldown). ``kind``
        is ``"host"`` or ``"room"`` → categories ``host_up``/``host_down`` /
        ``room_up``/``room_down``. Never raises — called from probe workers and
        room threads."""
        try:
            if not self._conn_cue_gate.note(
                f"{kind}:{name}", connected, time.monotonic()
            ):
                return
            self._play_sound(f"{kind}_{'up' if connected else 'down'}")
        except Exception:
            logger.debug("connection cue failed (%s %s)", kind, name, exc_info=True)

    def test_sound(self, category: str, token: str | None = None) -> bool:
        """Preview a cue from Settings: play ``token`` if given (the row's
        unsaved selection), else the category's currently-effective cue. Forced
        past the master toggle so previews work while sounds are disabled."""
        try:
            if category not in sounds.CATEGORIES:
                return False
            return sounds.play(category, force=True, token=token or None)
        except Exception:
            return False

    def sound_pack_info(self, path: str = "") -> dict[str, Any]:
        """Coverage report for a sound-pack folder (``path``, or the configured
        pack when blank): which categories it provides files for, so the
        Settings UI can show "covers N of M" before saving."""
        try:
            d = (path or "").strip() or config_mod.sound_pack()
            from pathlib import Path as _Path

            exists = bool(d) and _Path(d).is_dir()
            covered = sounds.pack_coverage(d) if exists else {}
            return {
                "dir": d,
                "exists": exists,
                "covered": covered,
                "categories": list(sounds.CATEGORIES),
            }
        except Exception:
            return {
                "dir": path,
                "exists": False,
                "covered": {},
                "categories": list(sounds.CATEGORIES),
            }

    # ── named-sound registry (runspec_sounds.toml) ───────────────────────────
    #
    # A named sound is an operator-added audio file given a stable id + label, so
    # it can be referenced by name in Settings, a trigger/schedule action, and the
    # agent's play_sound tool. The registry rides Config Sync (merged by id); the
    # audio files stay machine-local (copied under config.sounds_store_dir()). See
    # sounds_registry.py.

    def get_sounds(self) -> list[dict[str, Any]]:
        """The named-sound registry for the Settings UI: ``[{id, label, file,
        exists}]``. ``exists`` is False for a synced entry whose audio file isn't
        present on this machine (it resolves to silence). Never raises."""
        try:
            from . import sounds_registry

            return [
                {
                    "id": s.id,
                    "label": s.label,
                    "file": s.file,
                    "exists": s.exists(),
                }
                for s in sounds_registry.list_sounds()
            ]
        except Exception:
            logger.debug("get_sounds failed", exc_info=True)
            return []

    def save_sounds(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Replace the whole named-sound registry (Settings edit). Validates every
        entry (id/label/file) before writing; rejects the batch with a message on
        the first bad row or a duplicate id, so a garbage save can't strand cues."""
        from . import sounds_registry

        try:
            clean: list[dict[str, Any]] = []
            seen: set[str] = set()
            for raw in rows or []:
                s = sounds_registry.parse_sound(dict(raw))
                if s.id in seen:
                    raise ValueError(f"duplicate sound id '{s.id}'")
                seen.add(s.id)
                clean.append({"id": s.id, "label": s.label, "file": s.file})
            sounds_registry.save_rows(clean)
            return {"ok": True, "sounds": self.get_sounds()}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            logger.debug("save_sounds failed", exc_info=True)
            return {"ok": False, "error": str(exc)}

    def import_sound(self, source_path: str, label: str = "") -> dict[str, Any]:
        """Add a named sound: copy ``source_path`` (a ``.wav``/``.mp3`` the operator
        picked) into the app-data sounds store so the registry is self-contained,
        register it under a slug id derived from ``label`` (or the file name), and
        return ``{ok, sound}``. The copy keeps the sound working if the original is
        later moved/deleted, and stays machine-local (never synced)."""
        import shutil
        from pathlib import Path

        from . import sounds_registry

        try:
            src = Path(str(source_path or "").strip())
            if not src.is_file():
                return {"ok": False, "error": "file not found"}
            ext = src.suffix.lower()
            if ext not in sounds_registry.FILE_EXTS:
                return {
                    "ok": False,
                    "error": f"must be a {' or '.join(sounds_registry.FILE_EXTS)} file",
                }
            name = str(label or "").strip() or src.stem
            existing = {s.id for s in sounds_registry.list_sounds()}
            sid = sounds_registry.unique_id(name, existing)
            store = config_mod.sounds_store_dir()
            store.mkdir(parents=True, exist_ok=True)
            dest = store / f"{sid}{ext}"
            shutil.copyfile(src, dest)
            rows = sounds_registry.load_rows()
            rows.append({"id": sid, "label": name, "file": str(dest)})
            sounds_registry.save_rows(rows)
            return {
                "ok": True,
                "sound": {"id": sid, "label": name, "file": str(dest), "exists": True},
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("import_sound failed", exc_info=True)
            return {"ok": False, "error": str(exc)}

    def delete_sound(self, sound_id: str) -> dict[str, Any]:
        """Remove a named sound from the registry. If its audio file lives in the
        app-data sounds store (i.e. this console imported it), delete that file too;
        a file the operator pointed at in place is left untouched."""
        from pathlib import Path

        from . import sounds_registry

        try:
            sid = str(sound_id or "").strip()
            rows = sounds_registry.load_rows()
            kept = [r for r in rows if str(r.get("id") or "") != sid]
            removed = next((r for r in rows if str(r.get("id") or "") == sid), None)
            sounds_registry.save_rows(kept)
            if removed:
                try:
                    f = Path(str(removed.get("file") or ""))
                    store = config_mod.sounds_store_dir().resolve()
                    if f.is_file() and store in f.resolve().parents:
                        f.unlink()
                except OSError:
                    logger.debug("delete_sound: file cleanup failed", exc_info=True)
            return {"ok": True, "sounds": self.get_sounds()}
        except Exception as exc:  # noqa: BLE001
            logger.debug("delete_sound failed", exc_info=True)
            return {"ok": False, "error": str(exc)}

    def preview_sound(self, identifier: str) -> bool:
        """Audition a named sound / bundled cue by id or token (the Settings ▶ on a
        registry row). Forced past the master toggle so a preview works while
        sounds are disabled — mirrors ``test_sound``. Never raises."""
        try:
            return sounds.play_identifier(str(identifier or ""), force=True)
        except Exception:
            return False

    def _play_sound_id(self, identifier: str) -> bool:
        """Play a sound named by a registry id / bundled name / token, respecting
        the master ``[console.sounds] enabled`` toggle (unlike ``preview_sound``).
        The path a trigger's / schedule's ``sound`` field and the ``play_sound``
        agent tool go through. Best-effort — never raises."""
        try:
            return sounds.play_identifier(str(identifier or ""), force=False)
        except Exception:
            logger.debug("play sound id failed (%s)", identifier, exc_info=True)
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
        confirmations) or ``None`` to run the call as the agent proposed it.

        Emits ``runspec:confirm_resolved`` (with the owning ``chat_id`` +
        ``request_id``) so the UI can clear this specific gate — the frontend
        otherwise never learns a confirm ended by *timeout* (it only sees the
        ones it answered itself), which used to leave a stale Approve card
        wedging the run card at 'waiting for human'."""
        with self._lock:
            entry = self._pending_confirms.pop(request_id, None)
            self._pending_confirm_meta.pop(request_id, None)
        chat_id = entry[0] if entry else ""
        if timed_out:
            logger.warning(
                "autonomy gate: request_id=%s timed out — treating as deny",
                request_id,
            )
        else:
            logger.info(
                "autonomy gate: request_id=%s answered approved=%s",
                request_id,
                holder["approved"],
            )
        # Authoritative "this confirm is finished" signal for the UI. Harmless for
        # a gate the operator just answered (the frontend already cleared it);
        # essential for a timeout/cancel the frontend was never told about.
        self._dispatch(
            "runspec:confirm_resolved",
            {"id": chat_id, "request_id": request_id},
        )
        edited = holder.get("edited_input") if holder["approved"] else None
        return holder["approved"], edited

    async def _gated_run_tool_async(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        chat_id: str,
        stage: Callable[[str], None] | None = None,
        on_decision: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        """Async autonomy gate used by the agentic loop.

        The UI prompt is dispatched from the event-loop thread (the same thread
        that streams tokens, which is known to reach the frontend) — only the
        blocking wait is offloaded to a worker thread. Dispatching the prompt
        from a worker thread was unreliable and produced silent timeouts.

        ``stage`` (trigger turns only) reports the card stage: "awaiting_human"
        while a confirm is pending, then "running" once it actually executes.

        ``on_decision`` (chat turns only), when a confirm gate actually fires, is
        called with ``{"decision": "approved"|"edited"|"denied", "proposed_input":
        ...}`` so the caller can record the operator's decision on the saved
        transcript's tool entry (for the operator's later review) and echo it into
        the live ``runspec:tool_end`` so the card keeps its badge without a reload.
        Not called for autonomous ("run") or manual-only calls. Separately, an
        approved/edited result is prefixed with a short ``[gate: …]`` note so the
        agent itself knows the step was human-gated (denials already say so)."""
        inner_call: tuple[str, dict[str, Any]] | None = None
        if tool_name == "use_tool":
            # Deferred built-in (search mode). Resolve the inner tool now, but
            # keep gating under the RAW wrapper name: the frontend matches the
            # confirm prompt to the running tool segment by exact name (see
            # _register_confirm), so renaming here leaves the Approve/Deny
            # buttons unrendered and the call times out as a deny. The decision
            # and the confirm card still reflect the inner tool
            # (_effective_tool_autonomy / _confirm_card_view unwrap), and the
            # inner call is applied just before execution below — after which
            # Console tracking and audit see the real tool.
            unwrapped = self._unwrap_use_tool(tool_input)
            if isinstance(unwrapped, str):
                return unwrapped
            inner_call = unwrapped
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
        if tool_name == "run_saved_run":
            # A saved run expands to concrete host__runnable calls and rides the
            # same grouped-confirm machinery as a group fan-out — the gate then
            # reflects the *underlying* runnable's autonomy (most-restrictive
            # across a multi-host preset), not a blanket action-tool confirm.
            expanded = self._saved_run_gate_calls(tool_input)
            if isinstance(expanded, str):
                return expanded
            sid, name, calls = expanded
            outs = await self._grouped_confirm_run_async(
                chat_id, calls, noun=f"saved run '{sid}'", stage=stage
            )
            return self._format_group_output(f"saved run '{sid}'", name, calls, outs)
        decision = self._confirm_decision(tool_name, tool_input)
        display = self._confirm_card_view(tool_name, tool_input)[0].split("__", 1)[-1]
        logger.info("autonomy gate: tool=%s decision=%s", tool_name, decision)
        # The operator's confirm-gate outcome for the tool span (empty when the
        # gate never fired — an autonomous "run" call).
        gate_operator_decision = ""
        if decision == "manual":
            return f"✗ '{display}' is manual-only (autonomy = manual) and cannot be run by the assistant. Ask the user to run it themselves from the Run button."
        if decision != "run":
            if stage is not None:
                stage("awaiting_human")
            request_id, event, holder = self._register_confirm(
                chat_id, tool_name, tool_input, decision
            )
            # stage is set only on trigger/schedule turns — those wait on the
            # longer headless timeout (the operator may be away when it fires).
            timed_out = not await asyncio.to_thread(
                event.wait, self._confirm_timeout_s(stage is not None)
            )
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
                if on_decision is not None:
                    on_decision({"decision": "denied"})
                return (
                    f"✗ The user declined to run '{display}' (autonomy = {decision})."
                )
            gate_operator_decision = "edited" if edited is not None else "approved"
            # The operator may have edited the args before approving — run those.
            # For a wrapped call the card displayed the INNER input, so an edit
            # (defensive: the frontend renders wrapper confirms read-only) is in
            # that same shape and replaces the inner input, not the envelope.
            if on_decision is not None:
                proposed = inner_call[1] if inner_call is not None else tool_input
                on_decision(
                    {
                        "decision": "edited" if edited is not None else "approved",
                        "proposed_input": proposed if edited is not None else None,
                    }
                )
            if edited is not None:
                if inner_call is not None:
                    inner_call = (inner_call[0], edited)
                else:
                    tool_input = edited
        if stage is not None:
            stage("running")
        # The gate has decided — from here on, run the real tool the operator
        # approved (Console card + audit under the inner name).
        if inner_call is not None:
            tool_name, tool_input = inner_call
        # OpenTelemetry tool span (child of the turn span; no-op unless telemetry
        # is on). Opened *before* the asyncio.to_thread dispatch below so the
        # copied context carries it into the worker thread — this is what keeps
        # the runnable execution parented to the turn's trace. Shared by chat and
        # trigger/schedule turns, so one wrap covers every turn type.
        with observability.tool_span(
            tool_name,
            chat_id,
            decision=decision,
            provenance=getattr(self, "_run_origin", {}).get(chat_id),
        ) as _tool_span:
            observability.set_tool_decision(_tool_span, gate_operator_decision)
            observability.add_tool_arguments(_tool_span, tool_input)
            # Built-in tools (repo_*, outlook_*, manage_denylist, memory, status,
            # action tools) run on a worker thread with no native run card — wrap
            # them so they show as a live Console in-flight card, stream progress,
            # and can be stopped mid-run. Runnable executions (run_runnable /
            # search meta-tools) keep their own invoke_runnable card and are
            # excluded.
            if tool_name in _BUILTIN_TOOL_NAMES or (
                getattr(self, "_mcp", None) is not None and self._is_mcp_tool(tool_name)
            ):
                out = await self._run_builtin_tool_tracked(
                    tool_name, tool_input, chat_id
                )
            else:
                out = await asyncio.to_thread(
                    self._run_tool_sync, tool_name, tool_input, chat_id
                )
            observability.set_tool_exit(_tool_span, out)
            # Tell the agent the call went through a human confirm gate and how it
            # was answered (approved / approved-with-edits). Autonomous "run" calls
            # never reach here with a decision set, so an ungated call carries no
            # note. Denials return earlier with their own note. This keeps the
            # agent's account of what happened honest — it can say a human approved
            # the step rather than presenting it as if it had run unsupervised.
            if gate_operator_decision:
                note = (
                    "operator approved this confirm-gated call with edited arguments"
                    if gate_operator_decision == "edited"
                    else "operator approved this confirm-gated call"
                )
                out = f"[gate: {note}]\n{out}"
            return out

    async def _run_builtin_tool_tracked(
        self, tool_name: str, tool_input: dict[str, Any], chat_id: str
    ) -> str:
        """Run a built-in agent tool with a live Console card, cooperative
        cancellation, progress streaming, and a History/Analytics audit record.

        The card lives in the same ``self._in_flight`` dict runnable runs use, so
        it appears in the Console strip with elapsed time and a Cancel button
        (``cancel_invocation`` reaches its event) and the chat Stop button can
        stop it (``cancel_chat`` sets every record whose ``chatId`` matches).
        Per-tool Cancel stops just this tool — the agent gets a "⏹ stopped"
        result and the turn keeps reasoning; the chat Stop also halts the turn at
        its next checkpoint. Any tool failure is surfaced, audited, and returned
        as a normal tool-result string so the turn never dies on a tool error."""
        tool_run_id = uuid.uuid4().hex[:12]
        cancel_event = threading.Event()
        label = self._builtin_tool_label(tool_name, tool_input)
        args_summary = self._builtin_tool_args(tool_input)
        record = {
            "id": tool_run_id,
            "kind": "builtin_tool",
            "runnable": label,
            "tool": tool_name,
            "group": "",
            "host": self._builtin_tool_host(tool_name, tool_input),
            "operator": self._current_user(),
            "runAs": "",
            "startedAt": utils_mod._iso_now(),
            "args": args_summary,
            "chatId": chat_id,
            _CANCEL_KEY: cancel_event,
        }
        with self._lock:
            self._in_flight[tool_run_id] = record
        # Push the card immediately rather than waiting for the 5s in-flight poll.
        self._dispatch("runspec:in_flight_updated", {})

        def on_progress(message: str) -> None:
            # Mirror to the log (DEBUG — producer progress like repo_grep's
            # every-200-files line is bursty, so keep it out of the default log
            # but available under --debug) then drive the UI. Keyed to chat_id
            # (the chat block) + tool_id (the card) so the chat tool segment
            # shows a live progress line and the card reads as alive.
            logger.debug(
                "built-in tool %s progress (id=%s): %s",
                tool_name,
                tool_run_id,
                message,
            )
            self._dispatch(
                "runspec:tool_progress",
                {
                    "id": chat_id,
                    "tool_id": tool_run_id,
                    "tool_name": tool_name,
                    "message": str(message),
                },
            )

        start = time.monotonic()
        logger.info(
            "built-in tool %s started (id=%s) args=%s",
            tool_name,
            tool_run_id,
            args_summary,
        )

        async def _heartbeat() -> None:
            # Keep the log and the chat card alive during producer-silent
            # stretches (a big repo_read, or gaps between repo_grep progress).
            # Elapsed is read from the shared ``start`` so it stays truthful even
            # if a tick is late. The INFO line is the default-visible "still
            # alive" signal (progress mirroring above is DEBUG-only).
            while True:
                await asyncio.sleep(_TOOL_HEARTBEAT_SECS)
                secs = int(time.monotonic() - start)
                logger.info(
                    "built-in tool %s still running, %ds elapsed (id=%s)",
                    tool_name,
                    secs,
                    tool_run_id,
                )
                # chat_id is empty for non-chat turns (trigger/runbook): there is
                # no card to match, so skip the UI event but still log above.
                if chat_id:
                    self._dispatch(
                        "runspec:tool_progress",
                        {
                            "id": chat_id,
                            "tool_id": tool_run_id,
                            "tool_name": tool_name,
                            "message": f"still working… {secs}s elapsed",
                        },
                    )

        hb_task = asyncio.create_task(_heartbeat())
        outcome = "ok"
        try:
            output = await asyncio.to_thread(
                self._run_tool_sync,
                tool_name,
                tool_input,
                chat_id,
                cancel_event,
                on_progress,
            )
        except ToolCancelled:
            outcome = "cancelled"
            output = f"⏹ '{label}' was stopped."
        except Exception as exc:  # noqa: BLE001 — surface + audit, never kill the turn
            outcome = "error"
            logger.warning("built-in tool %s failed", tool_name, exc_info=True)
            output = f"✗ '{label}' failed: {exc}"
        finally:
            # Stop the heartbeat before tearing down the card so no stray
            # tool_progress fires for a gone card; await it (suppressing the
            # expected CancelledError) to avoid a "Task was destroyed" warning.
            hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb_task
            with self._lock:
                self._in_flight.pop(tool_run_id, None)
            self._dispatch("runspec:in_flight_updated", {})
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.info(
                "built-in tool %s finished outcome=%s in %dms (id=%s)",
                tool_name,
                outcome,
                duration_ms,
                tool_run_id,
            )
            self._audit_builtin_tool(
                tool_run_id,
                tool_name,
                label,
                args_summary,
                outcome,
                duration_ms,
                chat_id,
            )
        return output

    @staticmethod
    def _builtin_tool_label(tool_name: str, tool_input: dict[str, Any]) -> str:
        """A short human label for a built-in tool's Console card / audit record,
        e.g. ``repo_sync: runspec`` — the tool plus its most salient argument."""
        ti = tool_input or {}

        def first(*keys: str) -> str:
            for k in keys:
                v = ti.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
                if isinstance(v, (list, tuple)) and v:
                    return ", ".join(str(x) for x in v)
            return ""

        if tool_name.startswith("repo_"):
            key = first("name", "path", "pattern")
        elif tool_name == "manage_denylist":
            key = first("action")
        elif tool_name.startswith("outlook_"):
            key = first("query", "folder", "subject", "to")
        elif tool_name == "run_runbook":
            # The tool's parameter is `runbook_id` — reading only id/name left the
            # card as a bare "run_runbook" with nothing saying which one.
            key = first("runbook_id", "id", "name")
        elif tool_name == "create_document":
            key = first("path", "name")
        elif tool_name == "create_schedule":
            key = first("action", "runnable", "id")
        elif tool_name == "emit_event":
            key = first("source", "type")
        elif tool_name in ("remember", "search_memory"):
            key = first("query", "text", "app")
        elif tool_name == "search_history":
            key = first("runnable", "host")
        elif tool_name == "list_files":
            key = first("path", "source_target", "host")
        elif tool_name == "save_runbook":
            # `id` is blank when the agent creates a new runbook (it's derived from
            # the title), so fall through to the title rather than showing nothing.
            key = first("id", "name", "title")
        else:
            key = ""
        key = key[:60]
        return f"{tool_name}: {key}" if key else tool_name

    @staticmethod
    def _builtin_tool_host(tool_name: str, tool_input: dict[str, Any]) -> str:
        """Host tag for the Console card when the call targets a host (download/
        relay/denylist fan-outs); blank for local-only tools (repo/memory/etc.)."""
        ti = tool_input or {}
        for k in ("host", "dest_host", "source_host"):
            v = ti.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        targets = ti.get("targets")
        if isinstance(targets, (list, tuple)) and targets:
            return ", ".join(str(t) for t in targets)
        if isinstance(targets, str) and targets.strip():
            return targets.strip()
        return ""

    @staticmethod
    def _builtin_tool_args(tool_input: dict[str, Any]) -> dict[str, Any]:
        """A bounded, JSON-safe copy of a built-in tool's input for the card +
        audit record. Long string values (e.g. document bodies) are truncated so
        the record stays small. Built-in tool inputs carry no secrets."""
        out: dict[str, Any] = {}
        for k, v in (tool_input or {}).items():
            if isinstance(v, str) and len(v) > 200:
                out[k] = v[:200] + "…"
            else:
                out[k] = v
        return out

    def _gated_run_tool(
        self, tool_name: str, tool_input: dict[str, Any], chat_id: str
    ) -> str:
        """Synchronous autonomy gate (kept for direct/unit-test use).

        autonomous → run; confirm/supervised → ask the user (Approve/Deny);
        manual → refuse (human-only; the user must use the Run button)."""
        inner_call: tuple[str, dict[str, Any]] | None = None
        if tool_name == "use_tool":
            # Same contract as the async gate: resolve now, gate under the raw
            # wrapper name (frontend segment-matching), execute the inner tool.
            unwrapped = self._unwrap_use_tool(tool_input)
            if isinstance(unwrapped, str):
                return unwrapped
            inner_call = unwrapped
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
        if tool_name == "run_saved_run":
            # Same interception as the async gate: gate on the underlying
            # runnable(s), one grouped confirm, no blanket action-tool prompt.
            expanded = self._saved_run_gate_calls(tool_input)
            if isinstance(expanded, str):
                return expanded
            sid, name, calls = expanded
            outs = self._grouped_confirm_run_sync(
                chat_id, calls, noun=f"saved run '{sid}'"
            )
            return self._format_group_output(f"saved run '{sid}'", name, calls, outs)
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
            # Edits are made against the card's display view — for a wrapped
            # call that's the inner input (defensive: wrapper confirms render
            # read-only), so they replace the inner input, not the envelope.
            if edited is not None:
                if inner_call is not None:
                    inner_call = (inner_call[0], edited)
                else:
                    tool_input = edited
        if inner_call is not None:
            tool_name, tool_input = inner_call
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

    def _tag_run_origin_runbook(self, chat_id: str, runbook: str) -> None:
        """Record that the rest of this turn is being driven by ``runbook``, so the
        runnables it calls are attributed to it in History/Analytics — the same
        provenance the Forms path sets via ``_spawn_chat_turn``.

        Deliberately *not* ``_set_run_origin``: that replaces the whole record,
        including ``trigger``, and ``_run_config_tool`` refuses when ``trigger`` is
        set. Clearing it here would let a rule trigger reach the config tools by
        first following a runbook. This only ever adds the runbook field."""
        if not chat_id:
            return
        with self._lock:
            origin = self._run_origin.get(chat_id)
            if origin is None:
                self._run_origin[chat_id] = {
                    "trigger": "",
                    "agent": True,
                    "runbook": runbook,
                }
            else:
                origin["runbook"] = runbook

    def _clear_run_origin(self, chat_id: str) -> None:
        with self._lock:
            self._run_origin.pop(chat_id, None)

    def _await_confirmation(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        level: str,
        chat_id: str,
        headless: bool = True,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Ask the UI to approve an agent tool call; block until
        resolve_tool_confirmation() fires. Times out as a deny. Returns
        ``(approved, edited_input)``.

        Only the synchronous gate (``_gated_run_tool``, the declarative
        rule-trigger path) uses this, so ``headless`` defaults to True — the
        longer trigger timeout."""
        request_id, event, holder = self._register_confirm(
            chat_id, tool_name, tool_input, level
        )
        timed_out = not event.wait(timeout=self._confirm_timeout_s(headless))
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
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        chat_id: str = "",
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> str:
        """Blocking runnable execution — call via asyncio.to_thread from the agentic loop.

        ``chat_id`` keys the turn's run provenance (set by the trigger handler):
        a rule trigger run is recorded with its source and ``agent=False`` (not an
        LLM run), an agent trigger run with its source and ``agent=True``; a direct
        chat turn (no registered origin) defaults to an agent run with no trigger.

        ``cancel_event``/``on_progress`` (set only when wrapped by
        ``_run_builtin_tool_tracked``) let the slow built-in tools cooperatively
        abort (raising ``ToolCancelled``) and stream progress; tools that don't
        accept them ignore them harmlessly.
        """
        if tool_name in _CUSTOM_TOOL_NAMES:
            return self._run_custom_tool(
                tool_name,
                dict(tool_input or {}),
                cancel_event=cancel_event,
                on_progress=on_progress,
            )
        if tool_name in _OUTLOOK_TOOL_NAMES:
            return self._run_outlook_tool(tool_name, dict(tool_input or {}))
        if tool_name in _REPO_TOOL_NAMES:
            return self._run_repo_tool(
                tool_name,
                dict(tool_input or {}),
                cancel_event=cancel_event,
                on_progress=on_progress,
            )
        if tool_name in _MEMORY_TOOL_NAMES:
            return self._run_memory_tool(tool_name, dict(tool_input or {}))
        if tool_name in _RUNBOOK_READ_TOOL_NAMES:
            return self._run_load_runbook(dict(tool_input or {}), chat_id)
        if tool_name in _STATUS_TOOL_NAMES:
            return self._run_status_tool(dict(tool_input or {}))
        if tool_name in _HISTORY_TOOL_NAMES:
            return self._run_history_tool(dict(tool_input or {}))
        if tool_name in _FILES_TOOL_NAMES:
            return self._run_files_tool(dict(tool_input or {}))
        if tool_name in _WEB_TOOL_NAMES:
            return self._run_web_tool(dict(tool_input or {}))
        if tool_name in _IMPROVE_TOOL_NAMES:
            return self._run_improve_tool(dict(tool_input or {}))
        if tool_name in _WORKSPACE_TOOL_NAMES:
            return self._run_workspace_tool(
                tool_name,
                dict(tool_input or {}),
                chat_id=chat_id,
                cancel_event=cancel_event,
                on_progress=on_progress,
            )
        if tool_name in _EXEC_TOOL_NAMES:
            return self._run_exec_tool(
                tool_name,
                dict(tool_input or {}),
                chat_id=chat_id,
                cancel_event=cancel_event,
                on_progress=on_progress,
            )
        if tool_name in _ACTION_TOOL_NAMES:
            return self._run_action_tool(tool_name, dict(tool_input or {}), chat_id)
        if tool_name in _CONFIG_TOOL_NAMES:
            return self._run_config_tool(tool_name, dict(tool_input or {}), chat_id)
        if getattr(self, "_mcp", None) is not None and self._is_mcp_tool(tool_name):
            return self._run_mcp_tool(tool_name, dict(tool_input or {}))
        # Read-only discovery meta-tools (search mode) — no side effects, return JSON.
        if tool_name == "search_runnables":
            return self._run_search_tool(dict(tool_input or {}))
        if tool_name == "describe_runnable":
            return self._run_describe_tool(dict(tool_input or {}))
        if tool_name == "describe_tool":
            return self._run_describe_builtin_tool(dict(tool_input or {}))
        if tool_name == "use_tool":
            # Fail closed: use_tool must be unwrapped by the autonomy gate (which
            # then gates/tracks the INNER tool). Reaching dispatch still wrapped
            # means a call site skipped the gate — refuse rather than execute.
            return (
                "Error: use_tool was not resolved by the autonomy gate — "
                "call the inner tool through the normal gated path."
            )
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
            call_args = self._coerce_run_args(ti.get("args"))
            credential_id = str(ti.get("credential_id") or "")
            credential_id_list: list[str] = []
            # Normalise `command` (str OR nested list path) with the SAME helper
            # the autonomy gate uses (_effective_tool_autonomy → _command_path),
            # so the node the gate approved is the node that runs. A bare
            # `[chosen_command] if str` dropped a list-valued command (the shape
            # saved runs and nested paths use) to `[]`, which ran the top-level
            # runnable while the gate had resolved the subcommand leaf's autonomy.
            command_path = _command_path(ti)
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
            # A saved-run call may carry several (its stored credential id list) —
            # popped the same way so they never reach the runnable as args.
            credential_id = str(call_args.pop("credential_id", "") or "")
            credential_id_list = [
                str(c).strip()
                for c in (call_args.pop("credential_ids", None) or [])
                if str(c).strip()
            ]
            if isinstance(chosen_command, list):
                # A saved run stores its subcommand as a path (possibly nested).
                command_path = [str(c) for c in chosen_command if str(c)]
            else:
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
        for cid in credential_id_list:
            self._apply_credentials(
                host,
                runnable,
                exec_args,
                secrets,
                explicit_id=cid,
                valid_args=self._declared_arg_names(cached_r, command_path),
                secret_args=secret_names,
            )
        # …and ALSO inject the name-derived plain env vars (e.g. JIRA_USERNAME) so
        # env-only runnables (jsm/snow style) work for the agent. In-scope
        # credentials apply automatically; an explicit credential_id (or a saved
        # run's stored id list) is merged in (it may be out of scope).
        cred_env, _ = self._credential_env(host, runnable, ids=None)
        if credential_id:
            explicit_env, _ = self._credential_env(host, runnable, [credential_id])
            cred_env.update(explicit_env)
        if credential_id_list:
            explicit_env, _ = self._credential_env(host, runnable, credential_id_list)
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
                jump=str(entry.get("jump") or "").strip(),
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
        orig_len = len(output)
        # Workspace spill: above the threshold, write the full raw output to the
        # active session's workspace/tool-results/ folder and hand the model a
        # short envelope + head preview. The model then reaches for the
        # workspace_* tools to interrogate the file at its own pace — the raw
        # bytes never enter the model context. Below the threshold, or when
        # there is no active workspace id, fall through to the classic
        # cap-and-truncate behaviour so nothing regresses.
        spilled = False
        if orig_len > _TOOL_OUTPUT_SPILL_THRESHOLD:
            spill_threshold = _TOOL_OUTPUT_SPILL_THRESHOLD
            try:
                ws_cfg = config_mod.read_workspace()
                spill_threshold = ws_cfg.get(
                    "spill_threshold", _TOOL_OUTPUT_SPILL_THRESHOLD
                )
            except (OSError, ValueError, TypeError):
                pass
            with self._lock:
                ws_map = getattr(self, "_workspace_by_chat", {})
                workspace_id = ws_map.get(chat_id, "")
            if not workspace_id:
                workspace_id = getattr(self, "_conversation_id", "")
            if spill_threshold and orig_len > spill_threshold and workspace_id:
                try:
                    from . import workspaces as ws_mod

                    ws_root = config_mod.workspace_dir(workspace_id)
                    envelope, _rel_path = ws_mod.spill_output(
                        ws_root,
                        tool_name,
                        uuid.uuid4().hex[:12],
                        output,
                        head_bytes=_SPILL_ENVELOPE_HEAD_BYTES,
                    )
                    output = envelope
                    spilled = True
                except OSError as e:
                    logger.warning(
                        "workspace spill failed (%s) — falling back to inline cap",
                        e,
                    )
        if not spilled:
            cap = self._tool_output_cap()
            if orig_len > cap:
                output = output[:cap] + make_truncation_suffix(orig_len, cap)
        if exit_code_holder[0] != 0:
            return f"[exit {exit_code_holder[0]}]\n{output}"
        return output or "(no output)"

    # ── search mode (tool_mode = "search") ────────────────────────────────────
