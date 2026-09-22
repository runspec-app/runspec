"""
test_mcp_server_gate.py — the Bridge-side gate logic for the console MCP server
(_mcp_server_execute): manual refused, autonomous runs, confirm approved/edited/
timed-out/declined. Uses a light fake with the gate primitives stubbed — no SDK,
no socket, no real executor.
"""

from __future__ import annotations

import json
import os
import threading

from runspec_console import config as config_mod
from runspec_console.bridge_mcp_server import McpServerMixin
from runspec_console.mcp_server import ExecTool


class _CardPrimitives:
    """The card / session / dispatch primitives ``_mcp_gate_with_card`` and
    ``_mcp_server_card`` touch — a gated MCP call now surfaces a visible Console
    card so its confirm has a home even with ``[console] ai = false``."""

    def _init_cards(self):
        self._lock = threading.Lock()
        self._trigger_cards: dict = {}
        self.cards: list[tuple] = []  # (event_name, payload) dispatched

    def _dispatch(self, event, payload):
        self.cards.append((event, dict(payload)))

    def _sync_card_session(self, kind, card):
        pass

    def _evict_trigger_cards(self, keep_id):
        pass

    def card_columns(self):
        return [p.get("column") for e, p in self.cards if e == "runspec:trigger_card"]


def _tool(name="greet", autonomy="autonomous", command_path=None):
    return ExecTool(
        name=name,
        description="",
        input_schema={"type": "object", "properties": {}},
        host="local",
        group="venv",
        runnable=name,
        command_path=command_path or [],
        autonomy=autonomy,
    )


class FakeBridge(_CardPrimitives, McpServerMixin):
    """McpServerMixin + just the gate/exec primitives it calls."""

    def __init__(
        self, *, approve=True, edited=None, cancelled=False, answer=True, scoped=None
    ):
        self._init_cards()
        self._approve = approve
        self._edited = edited
        self._cancelled = cancelled
        self._answer = answer  # whether the confirm "event" is set (answered)
        self._scoped = scoped or []  # credentials in scope for (host, runnable)
        self.runs: list[tuple] = []
        self.registered: list[tuple] = []
        self.credential_ids_seen: list = []
        self.inv_ids_seen: list = []

    # credential scope (server-side auto-apply) ----------------------------
    def _reload_credentials(self):
        pass

    def _scoped_credentials(self, host, runnable):
        return list(self._scoped)

    # gate primitives ------------------------------------------------------
    def _confirm_timeout_s(self, headless=False):
        return 300.0

    def _register_confirm(self, chat_id, tool_name, tool_input, level):
        self.registered.append((chat_id, tool_name, dict(tool_input), level))
        event = threading.Event()
        if self._answer:
            event.set()  # operator answered before the wait
        holder = {
            "approved": self._approve,
            "cancelled": self._cancelled,
            "edited_input": self._edited,
        }
        return "req-abc123", event, holder

    def _finish_confirm(self, request_id, holder, timed_out):
        edited = (
            holder.get("edited_input")
            if (holder["approved"] and not timed_out)
            else None
        )
        approved = holder["approved"] and not timed_out
        return approved, edited

    # executor -------------------------------------------------------------
    def invoke_runnable(
        self,
        host,
        runnable,
        args,
        *,
        command_path=None,
        group=None,
        credential_ids=None,
        trigger="",
        on_progress=None,
        **kw,
    ):
        self.credential_ids_seen.append(credential_ids)
        self.inv_ids_seen.append(kw.get("inv_id"))
        self.runs.append(
            (host, runnable, dict(args), tuple(command_path or []), trigger)
        )
        # simulate a fire-and-stream run that finishes immediately
        if on_progress:
            on_progress("line", {"line": f"ran {runnable} {args}"})
            on_progress("done", {"exit_code": 0, "duration_ms": 5})
        return "inv-1"


def test_manual_is_refused_without_running():
    b = FakeBridge()
    text, is_error = b._mcp_server_execute(_tool("danger", autonomy="manual"), {})
    assert is_error is True
    assert "manual" in text
    assert b.runs == []  # never executed


def test_autonomous_runs_and_returns_output():
    b = FakeBridge()
    text, is_error = b._mcp_server_execute(
        _tool("greet", autonomy="autonomous"), {"who": "x"}
    )
    assert is_error is False
    assert "ran greet" in text
    assert b.registered == []  # autonomous → no confirm raised
    assert b.runs and b.runs[0][1] == "greet" and b.runs[0][4] == "mcp"
    assert b.card_columns() == []  # nothing to approve → no card


def test_confirm_approved_runs():
    b = FakeBridge(approve=True, answer=True)
    text, is_error = b._mcp_server_execute(_tool(autonomy="confirm"), {"a": 1})
    assert is_error is False
    assert b.registered and b.registered[0][3] == "confirm"
    assert b.runs  # ran after approval
    # A visible Console card is raised for the gate and driven to completion.
    assert b.card_columns() == ["awaiting_human", "running", "done"]
    # The confirm is registered under the card id (its home), not a fixed sentinel.
    chat_id = b.registered[0][0]
    card_id = b.cards[0][1]["card_id"]
    assert chat_id == card_id and chat_id != "__mcp_server__"


def test_confirm_gate_run_streams_into_its_card():
    # The approved run is invoked with inv_id == card_id so its output/run_end
    # route to the card's Console run block.
    b = FakeBridge(approve=True, answer=True)
    b._mcp_server_execute(_tool(autonomy="confirm"), {"a": 1})
    card_id = b.cards[0][1]["card_id"]
    assert b.inv_ids_seen and b.inv_ids_seen[0] == card_id


def test_confirm_edited_input_is_used():
    b = FakeBridge(approve=True, answer=True, edited={"a": 999})
    b._mcp_server_execute(_tool(autonomy="confirm"), {"a": 1})
    assert b.runs[0][2] == {"a": 999}  # edited args won


def test_confirm_declined_does_not_run():
    b = FakeBridge(approve=False, answer=True)
    text, is_error = b._mcp_server_execute(_tool(autonomy="confirm"), {})
    assert is_error is True
    assert "declined" in text.lower()
    assert b.runs == []
    # The card is raised then finalised to failed (no dangling "waiting" card).
    assert b.card_columns() == ["awaiting_human", "failed"]


def test_scoped_credentials_are_passed_to_invoke():
    # In-scope credentials apply server-side by scope — their ids reach
    # invoke_runnable (so both the env-var + arg-binding channels fire).
    b = FakeBridge(scoped=[{"id": "Jira"}, {"id": "Deploy"}, {"no_id": 1}])
    b._mcp_server_execute(_tool("greet", autonomy="autonomous"), {})
    assert b.credential_ids_seen == [["Jira", "Deploy"]]  # ids only, malformed dropped


def test_no_scoped_credentials_passes_none():
    b = FakeBridge(scoped=[])
    b._mcp_server_execute(_tool("greet", autonomy="autonomous"), {})
    assert b.credential_ids_seen == [None]  # empty scope → None (no creds)


def _collapsed():
    """A `runnable`-style tool: greeter with hello=autonomous, bye=confirm."""
    return ExecTool(
        name="greeter",
        description="",
        input_schema={"type": "object", "properties": {}},
        host="local",
        group="venv",
        runnable="greeter",
        command_path=[],
        autonomy="confirm",
        command_options=["hello", "bye"],
        command_autonomy={"hello": "autonomous", "bye": "confirm"},
        command_required=True,
    )


def test_collapsed_autonomous_command_runs_without_confirm():
    b = FakeBridge()
    text, is_error = b._mcp_server_execute(
        _collapsed(), {"command": "hello", "name": "x"}
    )
    assert is_error is False
    assert b.registered == []  # hello is autonomous → no confirm raised
    # the resolved subcommand path is passed through, `command` popped from args
    assert b.runs and b.runs[0][3] == ("hello",) and b.runs[0][2] == {"name": "x"}


def test_collapsed_confirm_command_parks_and_runs_on_approval():
    b = FakeBridge(approve=True, answer=True)
    b._mcp_server_execute(_collapsed(), {"command": "bye"})
    assert b.registered and b.registered[0][3] == "confirm"  # bye gates
    assert b.runs and b.runs[0][3] == ("bye",)


def test_collapsed_missing_command_errors_without_running():
    b = FakeBridge()
    text, is_error = b._mcp_server_execute(_collapsed(), {})
    assert is_error is True and "requires a command" in text
    assert b.registered == [] and b.runs == []


def _collapsed_multihost():
    """A `runnable`-style tool with no subcommands, discovered on two hosts."""
    return ExecTool(
        name="df",
        description="",
        input_schema={"type": "object", "properties": {}},
        host="web1",
        group="linux",
        runnable="df",
        command_path=[],
        autonomy="autonomous",
        command_autonomy={"": "autonomous"},
        host_options=["web1", "web2"],
        host_required=True,
    )


def test_collapsed_host_arg_targets_that_host():
    b = FakeBridge()
    text, is_error = b._mcp_server_execute(_collapsed_multihost(), {"host": "web2"})
    assert is_error is False
    assert b.runs and b.runs[0][0] == "web2"  # ran on the chosen host, not web1


def test_collapsed_missing_host_errors_without_running():
    b = FakeBridge()
    text, is_error = b._mcp_server_execute(_collapsed_multihost(), {})
    assert is_error is True and "multiple hosts" in text
    assert b.runs == []


def test_confirm_timeout_returns_resumable_and_does_not_run():
    # answer=False → the event is never set → the bounded wait elapses.
    b = FakeBridge(answer=False)
    # shrink the bound so the test doesn't actually wait 300s
    b._mcp_server_confirm_wait = lambda: 0.05  # type: ignore
    text, is_error = b._mcp_server_execute(_tool(autonomy="confirm"), {})
    assert is_error is False  # resumable, not an error
    assert "awaiting operator approval" in text.lower()
    assert b.runs == []  # nothing ran (no orphan execution)
    # The parked card is finalised (failed) so it doesn't dangle at "waiting".
    assert b.card_columns() == ["awaiting_human", "failed"]


# ── built-in agent tools over MCP (opt-in) ───────────────────────────────────────


class BuiltinBridge(_CardPrimitives, McpServerMixin):
    """McpServerMixin + the built-in dispatch primitives it reuses."""

    def __init__(self, *, autonomy="autonomous", approve=True, answer=True):
        self._init_cards()
        self._autonomy = autonomy
        self._approve = approve
        self._answer = answer
        self.ran: list[tuple] = []
        self.registered: list[tuple] = []

    def _custom_agent_tools(self):
        # a mix: two exposed console-specific tools + two that must be filtered out
        return [
            {"name": "console_status", "description": "", "input_schema": {}},
            {"name": "remember", "description": "", "input_schema": {}},
            {"name": "manage_hosts", "description": "", "input_schema": {}},
            {"name": "run_python_sandboxed", "description": "", "input_schema": {}},
        ]

    def _effective_tool_autonomy(self, name, ti):
        return self._autonomy

    def _confirm_timeout_s(self, headless=False):
        return 300.0

    def _register_confirm(self, chat_id, tool_name, tool_input, level):
        self.registered.append((chat_id, tool_name, level))
        event = threading.Event()
        if self._answer:
            event.set()
        return "req-b1", event, {"approved": self._approve, "cancelled": False}

    def _finish_confirm(self, request_id, holder, timed_out):
        return (holder["approved"] and not timed_out), None

    def _run_tool_sync(self, name, args, chat_id=""):
        self.ran.append((name, dict(args), chat_id))
        return f"ran {name}"


def _expose(monkeypatch, on=True):
    monkeypatch.setattr(
        config_mod,
        "read_mcp_server",
        lambda: {"expose_builtins": on, "confirm_wait": 50},
    )


def test_builtins_off_by_default(monkeypatch):
    _expose(monkeypatch, on=False)
    assert BuiltinBridge()._mcp_server_builtins() == []


def test_builtins_filtered_to_console_specific_set(monkeypatch):
    _expose(monkeypatch, on=True)
    names = {t["name"] for t in BuiltinBridge()._mcp_server_builtins()}
    assert names == {"console_status", "remember"}  # config + exec tools dropped


def test_builtin_autonomous_runs_under_mcp_chat_id(monkeypatch):
    _expose(monkeypatch, on=True)
    b = BuiltinBridge(autonomy="autonomous")
    text, is_error = b._mcp_server_execute_builtin("console_status", {"x": 1})
    assert is_error is False and "ran console_status" in text
    assert b.registered == []  # autonomous → no confirm
    assert b.ran and b.ran[0][0] == "console_status" and b.ran[0][2] == "__mcp_server__"


def test_builtin_confirm_parks_then_runs(monkeypatch):
    _expose(monkeypatch, on=True)
    b = BuiltinBridge(autonomy="confirm", approve=True, answer=True)
    text, is_error = b._mcp_server_execute_builtin("remember", {"text": "x"})
    assert is_error is False
    assert b.registered and b.registered[0][2] == "confirm"
    assert b.ran and b.ran[0][0] == "remember"
    # A visible card is raised for the built-in's gate and finalised to done.
    assert b.card_columns() == ["awaiting_human", "running", "done"]


def test_builtin_not_in_allowlist_is_refused(monkeypatch):
    _expose(monkeypatch, on=True)
    b = BuiltinBridge()
    text, is_error = b._mcp_server_execute_builtin("manage_hosts", {})
    assert is_error is True and "not exposed" in text
    assert b.ran == []


def test_builtin_confirm_timeout_is_resumable(monkeypatch):
    _expose(monkeypatch, on=True)
    b = BuiltinBridge(autonomy="confirm", answer=False)
    b._mcp_server_confirm_wait = lambda: 0.05  # type: ignore
    text, is_error = b._mcp_server_execute_builtin("remember", {})
    assert is_error is False and "awaiting operator approval" in text.lower()
    assert b.ran == []


# ── discovery file hardening ─────────────────────────────────────────────────────


class _FakeServer:
    def __init__(self, running=True, url="http://127.0.0.1:8765/mcp"):
        self._running = running
        self._url = url

    def status(self):
        return {"running": self._running, "url": self._url}


class _DiscoveryBridge(McpServerMixin):
    def __init__(self, server):
        self._mcp_server = server


def test_discovery_file_written_owner_only(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(config_mod, "read_mcp_server", lambda: {"token": "s3cret"})
    b = _DiscoveryBridge(_FakeServer(running=True))
    b._write_mcp_server_discovery()

    path = tmp_path / "runspec_mcp_server.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["token"] == "s3cret" and data["url"].endswith("/mcp")
    assert data["header"] == "Authorization: Bearer s3cret"
    if os.name != "nt":  # POSIX: the token file must not be group/world-readable
        assert (path.stat().st_mode & 0o777) == 0o600


def test_discovery_file_removed_when_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(config_mod, "read_mcp_server", lambda: {"token": None})
    path = tmp_path / "runspec_mcp_server.json"
    path.write_text("{}")  # stale file from a previous run
    b = _DiscoveryBridge(_FakeServer(running=False))
    b._write_mcp_server_discovery()
    assert not path.exists()


def test_discovery_file_tightens_preexisting_loose_perms(tmp_path, monkeypatch):
    if os.name == "nt":
        return  # POSIX perms only
    monkeypatch.setattr(config_mod, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(config_mod, "read_mcp_server", lambda: {"token": "t"})
    path = tmp_path / "runspec_mcp_server.json"
    path.write_text("{}")
    os.chmod(path, 0o644)  # world-readable leftover
    _DiscoveryBridge(_FakeServer(running=True))._write_mcp_server_discovery()
    assert (path.stat().st_mode & 0o777) == 0o600
