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
import threading
from typing import TYPE_CHECKING, Any

from . import config as config_mod
from .bridge_const import logger

if TYPE_CHECKING:
    from .mcp_server import ExecTool

# A stable, non-chat "chat id" so parked MCP confirms route through the same UI
# event path (and cancel_chat) as the agent's own gate without colliding with a
# real conversation id.
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
        level = (tool.autonomy or "confirm").lower()
        display = "_".join([tool.runnable] + list(tool.command_path))
        args = dict(arguments or {})

        if level == "manual":
            return (
                f"'{display}' is autonomy = 'manual' — operator-only; the agent "
                "may not run it. Run it yourself in the console.",
                True,
            )

        if level in ("confirm", "supervised"):
            request_id, event, holder = self._register_confirm(
                _MCP_SERVER_CHAT_ID, display, args, level
            )
            bound = self._mcp_server_confirm_wait()
            approved_in_time = event.wait(timeout=bound)
            approved, edited = self._finish_confirm(
                request_id, holder, timed_out=not approved_in_time
            )
            if not approved_in_time:
                return (
                    f"⏳ Awaiting operator approval in the console (request "
                    f"{request_id[:8]}). Approve it there, then call this tool "
                    "again to run it.",
                    False,
                )
            if holder.get("cancelled") or not approved:
                return (f"Operator declined '{display}'.", True)
            if isinstance(edited, dict):
                args = edited

        return self._mcp_server_run_blocking(tool, args)

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
        self, tool: "ExecTool", args: dict[str, Any]
    ) -> tuple[str, bool]:
        """Run the runnable via :meth:`invoke_runnable` and block for its output +
        exit code (invoke_runnable is fire-and-stream; we join on run_end).

        In-scope credentials are applied server-side (see
        :meth:`_mcp_server_scoped_credential_ids`) — the client supplies no
        secret."""
        done = threading.Event()
        state: dict[str, Any] = {"lines": [], "exit": None}

        def on_progress(kind: str, data: dict[str, Any]) -> None:
            if kind == "line":
                state["lines"].append(str(data.get("line", "")))
            elif kind == "done":
                state["exit"] = int(data.get("exit_code", -1))
                done.set()

        credential_ids = self._mcp_server_scoped_credential_ids(
            tool.host, tool.runnable
        )
        try:
            self.invoke_runnable(
                tool.host,
                tool.runnable,
                args,
                command_path=list(tool.command_path),
                group=tool.group or None,
                credential_ids=credential_ids or None,
                trigger="mcp",
                on_progress=on_progress,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "mcp_server run of %s failed to start", tool.name, exc_info=True
            )
            return (f"Failed to start '{tool.runnable}': {exc}", True)

        done.wait()
        text = "\n".join(state["lines"]).strip() or "(no output)"
        exit_code = state["exit"] if state["exit"] is not None else -1
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
        when the server isn't running."""
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
            path.write_text(json.dumps(entry, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001 — discovery file is a convenience, never fatal
            logger.debug("writing MCP server discovery file failed", exc_info=True)
