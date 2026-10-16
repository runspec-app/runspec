"""
bridge_mcp_server.py — Bridge glue for the console's own MCP server.

Wires :class:`runspec_console.mcp_server.ConsoleMcpServer` into the Bridge:

* ``tools_provider`` → the visibility-filtered discovered runnables to publish.
* ``execute(tool, arguments)`` → the **gated, blocking** run of one external MCP
  tool call, returning ``(text, is_error)``: resolve autonomy, park a
  ``confirm``/``supervised`` call in the console's confirm-card UI (bounded wait,
  then a graceful resumable "awaiting approval" result), refuse ``manual``, then
  run the runnable via :meth:`invoke_runnable` (tagged ``trigger="mcp"`` so the
  run lands in History as MCP-initiated) and collect its output.
* the public JS API — ``mcp_server_status`` / ``mcp_server_start`` /
  ``get_mcp_server_config`` / ``save_mcp_server_config`` — and the discovery file
  the client's MCP config points at.

The transport/listing/heartbeat lives in ``mcp_server`` (SDK-only); the autonomy
gate + executor live here (the Bridge owns ``_register_confirm`` / ``invoke_runnable``).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

from . import config as config_mod
from . import bridge_utils as utils_mod
from . import mcp_server as mcp_server_mod
from .bridge_const import _MCP_SERVER_BUILTIN_NAMES, logger
from .session import new_mcp

if TYPE_CHECKING:
    from .mcp_server import ExecTool

# A stable, non-chat "chat id" so parked MCP confirms route through the same UI
# event path (and cancel_chat) as the agent's own gate without colliding with a
# real conversation id. NOTE: each parked MCP call now gets its OWN per-call card
# id (see :meth:`_mcp_gate_with_card`) so the confirm has a visible home — this
# constant survives only as the workspace/chat id for exposed built-in tool runs
# that need one.
_MCP_SERVER_CHAT_ID = "__mcp_server__"
_DISCOVERY_FILE = "runspec_mcp_server.json"


class McpServerMixin:
    # -- injected callbacks for ConsoleMcpServer ----------------------------

    def _mcp_server_runnables(self) -> list[dict[str, Any]]:
        """tools_provider: the discovered runnables to publish (agent-surface
        visibility applied, so ``[tool_visibility]`` hides carry over)."""
        return self._visible_runnables("agent")

    def _mcp_server_execute(
        self, tool: "ExecTool", arguments: dict[str, Any]
    ) -> tuple[str, bool]:
        """Gate + run one external MCP tool call → ``(text, is_error)``.

        Runs on a worker thread (the server marshals it off its event loop and
        heartbeats progress meanwhile)."""
        # Resolve the concrete host + subcommand + autonomy from the call. In `leaf`
        # style these are the tool's fixed host/command_path/autonomy; in `runnable`
        # style the `host` and `command` arguments pick them (the command maps to its
        # own autonomy, so a read verb stays autonomous while a write verb gates).
        host, command_path, level, args, resolve_error = tool.resolve_call(arguments)
        if resolve_error:
            return (resolve_error, True)
        level = (level or "confirm").lower()
        display = "_".join([tool.runnable] + list(command_path))
        if host and tool.host_options:
            display = f"{display}@{host}"  # multi-host runnable: show the target

        if level == "manual":
            return (
                f"'{display}' is autonomy = 'manual' — operator-only; the agent "
                "may not run it. Run it yourself in the console.",
                True,
            )

        card_id, approved, args, timed_out, reason = self._mcp_gate_with_card(
            display=display,
            args=args,
            level=level,
            runnable=tool.runnable,
            host=host or tool.host,
        )
        if not approved:
            if timed_out:
                return (
                    f"⏳ Awaiting operator approval in the console. Approve "
                    f"'{display}' there, then call this tool again to run it.",
                    False,
                )
            return (
                f"Operator declined '{display}'." + utils_mod.decline_note(reason),
                True,
            )

        return self._mcp_server_run_blocking(
            tool, args, command_path, host, card_id=card_id
        )

    # -- built-in agent tools (opt-in: [mcp_server] expose_builtins) ---------

    def _mcp_server_builtins(self) -> list[dict[str, Any]]:
        """builtins_provider: the console-specific built-in tool schemas to publish
        over MCP — **only** when ``[mcp_server] expose_builtins`` is on. Filtered
        from the **live** :meth:`_custom_agent_tools` by ``_MCP_SERVER_BUILTIN_NAMES``
        (status / run-history / memory + runbooks / host→local transfer / Outlook /
        action tools), so a tool unavailable on this install (e.g. Outlook
        off-Windows) simply isn't offered, and the never-expose sets (config +
        search-meta) and the client-redundant ones (workspace/exec/web/files/repo)
        are absent by construction."""
        try:
            if not config_mod.read_mcp_server().get("expose_builtins"):
                return []
            tools = self._custom_agent_tools()
        except Exception:  # noqa: BLE001 — never break the tool list
            logger.debug("mcp_server builtins list failed", exc_info=True)
            return []
        return [t for t in tools if t.get("name") in _MCP_SERVER_BUILTIN_NAMES]

    def _mcp_server_execute_builtin(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[str, bool]:
        """Gate + run one built-in agent tool over MCP → ``(text, is_error)``.

        Reuses the chat agent's own autonomy resolver (reads run; writes park in the
        console confirm card, same bounded-wait-then-resumable contract as a
        runnable) and dispatch (:meth:`_run_tool_sync`), under the stable
        ``__mcp_server__`` chat id — so a tool that needs a workspace gets a
        dedicated MCP one. Defence-in-depth: refuse anything not in the exposed
        allowlist even if a stale name reaches here."""
        if name not in _MCP_SERVER_BUILTIN_NAMES:
            return (f"'{name}' is not exposed over MCP.", True)
        args = dict(arguments or {})
        try:
            level = (self._effective_tool_autonomy(name, args) or "confirm").lower()
        except Exception:  # noqa: BLE001 — fail closed
            level = "confirm"
        if level == "manual":
            return (
                f"'{name}' is autonomy = 'manual' — operator-only; run it in the "
                "console.",
                True,
            )
        card_id, approved, args, timed_out, reason = self._mcp_gate_with_card(
            display=name, args=args, level=level, runnable=name, host=""
        )
        if not approved:
            if timed_out:
                return (
                    f"⏳ Awaiting operator approval in the console. Approve "
                    f"'{name}' there, then call this tool again to run it.",
                    False,
                )
            return (
                f"Operator declined '{name}'." + utils_mod.decline_note(reason),
                True,
            )
        try:
            text = self._run_tool_sync(name, args, _MCP_SERVER_CHAT_ID)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mcp_server builtin %s failed", name, exc_info=True)
            if card_id:
                self._mcp_server_card(
                    card_id, runnable=name, column="failed", error=str(exc)
                )
            return (f"'{name}' failed: {exc}", True)
        if card_id:
            self._mcp_server_card(
                card_id, runnable=name, column="done", result=str(text)
            )
        return (str(text), False)

    def _mcp_gate_with_card(
        self,
        *,
        display: str,
        args: dict[str, Any],
        level: str,
        runnable: str,
        host: str,
    ) -> tuple[str, bool, dict[str, Any], bool, str]:
        """Gate an MCP call behind a **visible** Console card, returning
        ``(card_id, approved, args, timed_out, decline_reason)`` — the reason is
        the operator's optional free text on a Deny (``""`` otherwise).

        Without a card the parked confirm had no home: its chat id was a fixed
        sentinel with no session/block, so the Approve/Deny prompt never rendered
        anywhere — worst of all with ``[console] ai = false``, where there is no
        chat session to fall back on. Each gated call now mints its own card
        (``card_id`` == the confirm chat id == the run inv_id) that surfaces as a
        read-only run block in the always-present Console tab, so the operator can
        approve it whether or not AI is enabled.

        An ``autonomous`` call has nothing to approve: it still gets a card, made
        straight in ``running``, so the operator sees every external call land in
        the Console (it used to run with no card at all). The
        caller runs it at once and finalises the card. ``args`` comes back with
        any operator edit applied."""
        card_id = uuid.uuid4().hex[:12]
        # Open (or reuse) the calling client's own chat before the first card
        # event, so the UI already knows the session the card names.
        self._mcp_client_session()
        if level not in ("confirm", "supervised"):
            self._mcp_server_card(
                card_id,
                runnable=runnable,
                args=args,
                host=host,
                column="running",
                note="autonomous — running",
            )
            return card_id, True, args, False, ""
        self._mcp_server_card(
            card_id,
            runnable=runnable,
            args=args,
            host=host,
            column="awaiting_human",
            note="awaiting operator approval",
        )
        request_id, event, holder = self._register_confirm(
            card_id, display, args, level
        )
        approved_in_time = event.wait(timeout=self._mcp_server_confirm_wait())
        approved, edited = self._finish_confirm(
            request_id, holder, timed_out=not approved_in_time
        )
        if not approved_in_time:
            self._mcp_server_card(
                card_id,
                runnable=runnable,
                column="failed",
                error="No operator response — the agent may call again to retry.",
                note="expired (no response)",
            )
            return card_id, False, args, True, ""
        if holder.get("cancelled") or not approved:
            reason = str(holder.get("decline_reason") or "")
            self._mcp_server_card(
                card_id,
                runnable=runnable,
                column="failed",
                error="Operator declined this call."
                + (f" Reason: {reason}" if reason else ""),
                note="declined",
            )
            return card_id, False, args, False, reason
        if isinstance(edited, dict):
            args = edited
        self._mcp_server_card(
            card_id,
            runnable=runnable,
            args=args,
            column="running",
            note="approved — running",
        )
        return card_id, True, args, False, ""

    def _mcp_server_card(
        self,
        card_id: str,
        *,
        runnable: str,
        args: dict[str, Any] | None = None,
        host: str | None = None,
        column: str,
        result: str | None = None,
        error: str | None = None,
        note: str | None = None,
    ) -> None:
        """Create/advance the Console card for one MCP-server tool call and push it
        to the UI. Keyed by ``card_id`` (== the confirm chat id == the run inv_id),
        so the parked gate, any streamed output and ``run_end`` all attach to the
        one read-only run block on the Console tab.

        Reuses the trigger inbox card shape — ``source="mcp"`` and
        ``detail.mode="rule"`` mark it a plain run (a run block, not an agent
        turn). Stored on the trigger board so a UI reload mid-approval replays it
        (the gate's own cold-start seed is :meth:`get_pending_confirms`). It opens
        no session of its own: it names the calling client's chat in ``session``
        (see :meth:`_mcp_client_session`), where the Console shows it."""
        now = time.time()
        session_id = self._mcp_client_session_id()
        with self._lock:
            card = self._trigger_cards.get(card_id)
            if card is None:
                card = {
                    "card_id": card_id,
                    "trigger": runnable,
                    "source": "mcp",
                    # The client's own chat (see _mcp_client_session); "" when
                    # it couldn't be opened → the UI falls back to the
                    # operator's current chat.
                    "session": session_id,
                    "event_type": "request",
                    "summary": f"/{runnable}" + (f" on {host}" if host else ""),
                    "column": column,
                    "detail": {
                        "mode": "rule",
                        "runnable": runnable,
                        "args": dict(args or {}),
                        "host": host or "",
                    },
                    "created_at": now,
                    "timeline": [],
                }
                self._trigger_cards[card_id] = card
            card["column"] = column
            detail = card["detail"]
            if args is not None:
                detail["args"] = dict(args)
            if host is not None:
                detail["host"] = host
            if result is not None:
                detail["result"] = result
            if error is not None:
                detail["error"] = error
            if note:
                card["timeline"].append({"ts": now, "text": note})
            self._evict_trigger_cards(card_id)
            snapshot = dict(card)
        # The block lands in the calling client's own chat (`session`), not one
        # session per card (a trigger fire's model) and no longer the
        # operator's current chat: each MCP client session gets one chat.
        self._dispatch("runspec:trigger_card", snapshot)

    def _mcp_client_session(self) -> str:
        """The read-only chat for the MCP client session making the current call
        — opened on the client's first call and reused for every later one, so
        an external agent's actions don't mix into whatever chat the operator
        has open. Keyed by the client's ``mcp-session-id`` (a client that
        reconnects gets a new chat); dismissing the chip makes the client's next
        call open a fresh one. Returns the session id ("" on failure)."""
        client = mcp_server_mod.current_client()
        key = client.key or "default"
        created = None
        try:
            with self._lock:
                owners = self._mcp_client_sessions
                sid = owners.get(key, "")
                session = self._sessions.get(sid) if sid else None
                if session is None:
                    label = f"MCP · {client.name}" if client.name else "MCP client"
                    session = new_mcp(label)
                    self._sessions[session.id] = session
                    owners[key] = session.id
                    created = session
                sid = session.id
        except Exception:  # noqa: BLE001 — never block a call on chat bookkeeping
            logger.debug("mcp_server client session open failed", exc_info=True)
            return ""
        if created is not None:
            self._emit_session(created)
        return sid

    def _mcp_client_session_id(self) -> str:
        """The calling client's chat id if it is open, without opening one."""
        key = mcp_server_mod.current_client().key or "default"
        with self._lock:
            sid = self._mcp_client_sessions.get(key, "")
            return sid if sid in self._sessions else ""

    def _mcp_server_confirm_wait(self) -> float:
        """The bounded confirm wait: the smaller of ``[mcp_server] confirm_wait``
        and the chat confirm timeout (so it never exceeds either)."""
        try:
            wait = float(config_mod.read_mcp_server().get("confirm_wait") or 50.0)
        except Exception:  # noqa: BLE001
            wait = 50.0
        ceiling = self._confirm_timeout_s(headless=False)
        return min(wait, ceiling) if ceiling else wait

    def _mcp_server_scoped_credential_ids(self, host: str, runnable: str) -> list[str]:
        """The ids of the credentials in scope for ``(host, runnable)``.

        The external MCP client can't select a credential (no `credential_id` in
        the tool schema) and never sees a secret, so — like the console's own
        agent — credentials apply **by scope** (`scope_hosts`/`scope_runnables`
        globs; empty = match-all). Passing these ids to :meth:`invoke_runnable`
        (rather than leaving it ``None``) makes it apply **both** channels: the
        name-derived env vars (`JIRA_USERNAME`/`JIRA_PASSWORD`, `*_TOKEN`, …) *and*
        the arg-bindings (`username_arg`→argv, `secret_arg`→secret env channel),
        so an arg-interface runnable (`--user`/`--password`) gets its credential
        over MCP just as an env-var one does. (The sole-password fallback — a lone
        `password` arg with no `secret_arg` — stays chat-agent-only.)"""
        try:
            self._reload_credentials()
            return [
                str(c["id"])
                for c in self._scoped_credentials(host, runnable)
                if c.get("id")
            ]
        except Exception:  # noqa: BLE001 — credential resolution never breaks a run
            logger.debug("mcp_server scoped-credential lookup failed", exc_info=True)
            return []

    def _mcp_server_run_blocking(
        self,
        tool: "ExecTool",
        args: dict[str, Any],
        command_path: list[str] | None = None,
        host: str | None = None,
        card_id: str | None = None,
    ) -> tuple[str, bool]:
        """Run the runnable via :meth:`invoke_runnable` and block for its output +
        exit code (invoke_runnable is fire-and-stream; we join on run_end).
        ``command_path`` is the resolved subcommand and ``host`` the resolved target
        (both default to the tool's own, for ``leaf`` style).

        ``card_id`` (set for a gated call) is passed as the run's ``inv_id`` so the
        streamed output / ``run_end`` route into the confirm card's run block, and
        the card is finalised to ``done``/``failed`` from the exit code.

        In-scope credentials are applied server-side (see
        :meth:`_mcp_server_scoped_credential_ids`) — the client supplies no
        secret."""
        resolved_path = list(
            command_path if command_path is not None else tool.command_path
        )
        resolved_host = host or tool.host
        done = threading.Event()
        state: dict[str, Any] = {"lines": [], "exit": None}

        def on_progress(kind: str, data: dict[str, Any]) -> None:
            if kind == "line":
                state["lines"].append(str(data.get("line", "")))
            elif kind == "done":
                state["exit"] = int(data.get("exit_code", -1))
                done.set()

        credential_ids = self._mcp_server_scoped_credential_ids(
            resolved_host, tool.runnable
        )
        try:
            self.invoke_runnable(
                resolved_host,
                tool.runnable,
                args,
                command_path=resolved_path,
                group=tool.group or None,
                credential_ids=credential_ids or None,
                trigger="mcp",
                inv_id=card_id,
                on_progress=on_progress,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "mcp_server run of %s failed to start", tool.name, exc_info=True
            )
            if card_id:
                self._mcp_server_card(
                    card_id,
                    runnable=tool.runnable,
                    column="failed",
                    error=f"Failed to start: {exc}",
                )
            return (f"Failed to start '{tool.runnable}': {exc}", True)

        done.wait()
        text = "\n".join(state["lines"]).strip() or "(no output)"
        exit_code = state["exit"] if state["exit"] is not None else -1
        if card_id:
            self._mcp_server_card(
                card_id,
                runnable=tool.runnable,
                column="done" if exit_code == 0 else "failed",
                result=text,
                **({} if exit_code == 0 else {"error": text or f"exit {exit_code}"}),
            )
        if exit_code != 0:
            return (f"{text}\n\n[exit code {exit_code}]", True)
        return (text, False)

    # -- public JS API ------------------------------------------------------

    def mcp_server_status(self) -> dict[str, Any]:
        """Live status of the console's MCP server (running / url / auth / tool
        count / last error). The token is never included."""
        return self._mcp_server.status()

    def mcp_server_start(self) -> dict[str, Any]:
        """Start the server if ``[mcp_server] enabled`` (best-effort; called at
        app bring-up). Writes the discovery file when it comes up."""
        try:
            self._mcp_server.start()
            self._write_mcp_server_discovery()
        except Exception:  # noqa: BLE001
            logger.warning("mcp_server start failed", exc_info=True)
        return self._mcp_server.status()

    def get_mcp_server_config(self) -> dict[str, Any]:
        """The ``[mcp_server]`` settings for the Settings tab (never the token's
        value beyond what the operator typed — it round-trips as-is)."""
        return config_mod.read_mcp_server()

    def save_mcp_server_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate + persist ``[mcp_server]`` into config.toml (atomic, preserving
        every other section so it rides Config Sync / the seed), then restart the
        server to apply. Raises ``ValueError`` (rejecting the JS promise) on invalid
        input; returns the fresh :meth:`mcp_server_status`."""
        data = dict(payload or {})
        section: dict[str, Any] = {"enabled": bool(data.get("enabled", False))}

        host = str(data.get("host") or "").strip()
        if host:
            section["host"] = host
        if data.get("port") not in (None, ""):
            try:
                port = int(data["port"])
            except (TypeError, ValueError) as exc:
                raise ValueError("port must be a number") from exc
            if not (1 <= port <= 65535):
                raise ValueError("port must be between 1 and 65535")
            section["port"] = port
        token = str(data.get("token") or "").strip()
        if token:
            section["token"] = token
        autonomy = str(data.get("default_autonomy") or "").strip().lower()
        if autonomy:
            if autonomy not in ("autonomous", "confirm", "supervised", "manual"):
                raise ValueError(f"invalid default_autonomy: {autonomy}")
            section["default_autonomy"] = autonomy
        if data.get("confirm_wait") not in (None, ""):
            try:
                section["confirm_wait"] = max(1.0, float(data["confirm_wait"]))
            except (TypeError, ValueError) as exc:
                raise ValueError("confirm_wait must be a number") from exc
        tool_style = str(data.get("tool_style") or "").strip().lower()
        if tool_style:
            if tool_style not in ("leaf", "runnable"):
                raise ValueError(f"invalid tool_style: {tool_style}")
            section["tool_style"] = tool_style
        if data.get("expose_builtins"):
            section["expose_builtins"] = True

        def _mutate(cfg: dict[str, Any]) -> None:
            cfg["mcp_server"] = section

        config_mod.update_config(_mutate)
        try:
            self._mcp_server.restart()
            self._write_mcp_server_discovery()
        except Exception:  # noqa: BLE001
            logger.warning("mcp_server restart after save failed", exc_info=True)
        self._dispatch("runspec:mcp_server_changed", {})
        return self._mcp_server.status()

    def _write_mcp_server_discovery(self) -> None:
        """Write ``{app_dir}/runspec_mcp_server.json`` with the live URL (+ token,
        when set) so a client's MCP config need not hard-code the port. Removed
        when the server isn't running.

        Written **owner-only (0600)**: on a loopback server the bearer token's real
        exposure is this file at rest (loopback traffic never touches a NIC, so
        there's no network MITM), so it must not be group/world-readable. On POSIX
        the perms are enforced before the secret is written (so there's no window
        where it's readable); on Windows ``chmod`` is a best-effort no-op (ACLs on
        the per-user app dir govern instead)."""
        path = config_mod.app_dir() / _DISCOVERY_FILE
        st = self._mcp_server.status()
        try:
            if not st.get("running"):
                path.unlink(missing_ok=True)
                return
            entry: dict[str, Any] = {"url": st.get("url", "")}
            token = config_mod.read_mcp_server().get("token")
            if token:
                entry["token"] = token
                entry["header"] = f"Authorization: Bearer {token}"
            # Create/truncate with 0600 so the token is never briefly world-readable;
            # also chmod an already-existing file down (O_CREAT won't relax perms,
            # but it also won't tighten a pre-existing looser file).
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, json.dumps(entry, indent=2).encode("utf-8"))
            finally:
                os.close(fd)
            try:
                os.chmod(
                    path, 0o600
                )  # tighten a pre-existing looser file; no-op on Windows
            except OSError:
                pass
        except Exception:  # noqa: BLE001 — discovery file is a convenience, never fatal
            logger.debug("writing MCP server discovery file failed", exc_info=True)
