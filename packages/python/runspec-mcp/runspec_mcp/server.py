"""
server.py — the fleet MCP gateway (JSON-RPC 2.0 over stdio).

A single MCP stdio server that aggregates every configured host. Unlike
``runspec serve`` it advertises ``tools.listChanged`` and pushes
``notifications/tools/list_changed`` from a background discovery thread, so Claude
Code re-fetches automatically as hosts connect/disconnect or their runnables
change — no restart, no manual refresh. Credentials are injected per call; a
``manual`` runnable is refused server-side (the client-side hook denies it too).
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any

from . import manifest as manifest_mod
from .catalog import Catalog
from .config import FleetConfig, manifest_path
from .runner import run_tool

MCP_PROTOCOL_VERSION = "2024-11-05"

_ERR_PARSE = -32700
_ERR_METHOD_NOT_FOUND = -32601
_ERR_INVALID_PARAMS = -32602

_DECISION_MANUAL = "manual"

# Control tools — always autonomous, always present, let the agent inspect and
# nudge the fleet from inside the session.
_CONTROL_TOOLS: list[dict[str, Any]] = [
    {
        "name": "fleet_hosts",
        "description": "List the configured fleet hosts with their live connection status and tool counts.",
        "x-autonomy": "autonomous",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fleet_refresh",
        "description": "Re-discover runnables across all fleet hosts now (discovery is also automatic).",
        "x-autonomy": "autonomous",
        "inputSchema": {"type": "object", "properties": {}},
    },
]
_CONTROL_NAMES = {t["name"] for t in _CONTROL_TOOLS}


class FleetServer:
    def __init__(self, cfg: FleetConfig, pool: Any):
        self._cfg = cfg
        self._pool = pool
        self._write_lock = threading.Lock()
        self._manifest_path = manifest_path()
        # Notifications are suppressed until the initial discovery + serve loop is
        # up, so the client never sees a tools/list_changed before initialize.
        self._notify_enabled = False
        self._catalog = Catalog(cfg, pool, on_change=self._on_catalog_change)

    # ── framing ────────────────────────────────────────────────────────────────

    def _write(self, message: dict[str, Any]) -> None:
        with self._write_lock:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    # ── discovery-change hook ──────────────────────────────────────────────────

    def _rewrite_manifest(self) -> None:
        manifest = manifest_mod.build_manifest(self._catalog.tools(), self._cfg.server_name)
        for t in _CONTROL_TOOLS:
            manifest["tools"][t["name"]] = {"autonomy": "autonomous"}
        try:
            self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._manifest_path.with_suffix(self._manifest_path.suffix + ".tmp")
            tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self._manifest_path)
        except OSError:
            pass

    def _on_catalog_change(self) -> None:
        """Called by the catalog thread when the tool set changes."""
        self._rewrite_manifest()
        if not self._notify_enabled:
            return  # initial population — the client hasn't initialized yet
        # notifications have no id and no response expected
        self._write({"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {}})

    # ── request handling ───────────────────────────────────────────────────────

    def _all_schemas(self) -> list[dict[str, Any]]:
        return [*_CONTROL_TOOLS, *self._catalog.list_schemas()]

    def _handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method", "")
        req_id = request.get("id")
        if req_id is None:
            return None  # a notification — no response

        if method == "initialize":
            from . import __version__

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": self._cfg.server_name, "version": __version__},
                },
            }
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": self._all_schemas()}}
        if method == "tools/call":
            return self._handle_call(req_id, request.get("params", {}) or {})
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": _ERR_METHOD_NOT_FOUND, "message": f"Method not found: {method}"}}

    def _text_result(self, req_id: Any, text: str, is_error: bool = False, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
        if meta is not None:
            result["_meta"] = {"runspec": meta}
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _handle_call(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not name:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": _ERR_INVALID_PARAMS, "message": "missing tool name"}}

        if name in _CONTROL_NAMES:
            return self._handle_control(req_id, name)

        tool = self._catalog.get(name)
        if tool is None:
            return self._text_result(req_id, f"Unknown tool '{name}' — it may have gone away; discovery refreshes automatically.", is_error=True)

        if tool.autonomy == _DECISION_MANUAL:
            return self._text_result(
                req_id,
                f"'{name}' is autonomy = 'manual' — operator-only. Run it yourself on the host; the agent may not.",
                is_error=True,
            )

        try:
            code, text = run_tool(tool, arguments, self._cfg, self._pool)
        except Exception as exc:  # never crash the loop on one bad call
            return self._text_result(req_id, f"execution error: {exc}", is_error=True)
        return self._text_result(
            req_id,
            text or ("(no output)" if code == 0 else f"(no output; exit code {code})"),
            is_error=code != 0,
            meta={"tool": name, "host": tool.host.get("name"), "exit_code": code},
        )

    def _handle_control(self, req_id: Any, name: str) -> dict[str, Any]:
        if name == "fleet_refresh":
            changed = self._catalog.refresh()
            n = len(self._catalog.tools())
            return self._text_result(req_id, f"Refreshed: {n} tool(s) across the fleet{' (changed)' if changed else ''}.")
        if name == "fleet_hosts":
            rows = [
                {
                    "name": s.name,
                    "ssh": s.ssh or "(local)",
                    "connected": s.connected,
                    "venvs": s.venvs,
                    "tools": s.tools,
                    **({"error": s.error} if s.error else {}),
                }
                for s in self._catalog.status()
            ]
            return self._text_result(req_id, json.dumps(rows, indent=2))
        return self._text_result(req_id, f"unknown control tool '{name}'", is_error=True)

    # ── run loop ───────────────────────────────────────────────────────────────

    def serve_forever(self) -> None:
        # Initial discovery + manifest before we accept traffic.
        try:
            self._catalog.refresh()
        except Exception:
            pass
        self._rewrite_manifest()
        # From here on, a discovery change should notify the client.
        self._notify_enabled = True
        self._catalog.start_background()

        if sys.stdin.isatty():
            sys.stderr.write("runspec-mcp serve is an MCP stdio server — configure it in your MCP host, don't run it in a terminal.\n")
            return
        try:
            for raw in sys.stdin:
                line = raw.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    self._write({"jsonrpc": "2.0", "id": None, "error": {"code": _ERR_PARSE, "message": "Parse error"}})
                    continue
                response = self._handle(request)
                if response is not None:
                    self._write(response)
        except KeyboardInterrupt:
            pass
        finally:
            self._catalog.stop()


def build_pool(cfg: FleetConfig) -> Any:
    """Construct the SSH connection pool from ``[ssh]`` config.

    Defaults the SSH identity to the gateway's managed keypair first, so remote
    hosts are reached with a key the operator never had to set up in ``~/.ssh``."""
    from . import sshkeys
    from .executor import _make_ssh_client
    from .ssh_pool import SSHConnectionPool, pool_settings

    sshkeys.apply_managed_identity(cfg)
    return SSHConnectionPool(settings=pool_settings(cfg.ssh), make_client=_make_ssh_client)


def serve(cfg: FleetConfig) -> None:
    pool = build_pool(cfg)
    FleetServer(cfg, pool).serve_forever()
