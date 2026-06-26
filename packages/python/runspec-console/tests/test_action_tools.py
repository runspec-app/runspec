"""Tests for the built-in 'action' agent tools (create_document, run_runbook,
create_schedule, emit_event) — schema presence, confirm-gating, and execution.

These mirror the lightweight-stub pattern in test_relay_filter.py: a tiny bridge
that borrows just the methods under test, so no pywebview/COM/LLM is needed."""

from __future__ import annotations

import threading

from runspec_console import memory
from runspec_console.bridge import Bridge
from runspec_console.bridge_const import _ACTION_TOOL_NAMES


class _ActionBridge:
    _custom_agent_tools = Bridge._custom_agent_tools
    _denylist_tool = Bridge._denylist_tool
    _outlook_tools = Bridge._outlook_tools
    _repo_tools = Bridge._repo_tools
    _memory_tools = Bridge._memory_tools
    _status_tools = Bridge._status_tools
    _action_tools = Bridge._action_tools
    _run_action_tool = Bridge._run_action_tool
    _action_create_document = Bridge._action_create_document
    _action_run_runbook = Bridge._action_run_runbook
    _action_create_schedule = Bridge._action_create_schedule
    _action_emit_event = Bridge._action_emit_event
    _effective_tool_autonomy = Bridge._effective_tool_autonomy

    def __init__(self, hosts=None, runnables=None):
        self._hosts = hosts or []
        self._runnables_cache = runnables or []
        self._filters = []
        self._credentials = []
        self._lock = threading.Lock()
        self._trigger_engine = None
        self.transfers: list = []
        self.created_schedules: list = []

    def _reload_credentials(self):
        pass

    def start_transfer(self, *a, **k):
        self.transfers.append((a, k))
        return {"id": "t1"}

    def _await_transfer(self, tid):
        return {"ok": True, "files": 1, "error": None}

    def create_schedule(self, data):
        self.created_schedules.append(data)


def test_action_tools_present_in_custom_agent_tools():
    b = _ActionBridge(hosts=[{"name": "prod", "ssh": "deploy@prod"}])
    names = {t["name"] for t in b._custom_agent_tools()}
    # create_document / create_schedule / emit_event are always offered.
    assert {"create_document", "create_schedule", "emit_event"} <= names


def test_create_document_dest_enum_includes_remote_hosts():
    b = _ActionBridge(hosts=[{"name": "prod", "ssh": "x"}, {"name": "local"}])
    doc = next(t for t in b._action_tools() if t["name"] == "create_document")
    assert doc["input_schema"]["properties"]["dest"]["enum"] == ["local", "prod"]


def test_run_runbook_tool_only_when_runbooks_exist(monkeypatch):
    monkeypatch.setattr(memory, "list_runbooks", lambda: [])
    b = _ActionBridge()
    assert "run_runbook" not in {t["name"] for t in b._action_tools()}
    monkeypatch.setattr(
        memory, "list_runbooks", lambda: [{"id": "restart", "title": "Restart svc"}]
    )
    rs = next(t for t in b._action_tools() if t["name"] == "run_runbook")
    assert rs["input_schema"]["properties"]["runbook_id"]["enum"] == ["restart"]


def test_all_action_tools_are_confirm_gated():
    b = _ActionBridge()
    for name in _ACTION_TOOL_NAMES:
        assert b._effective_tool_autonomy(name, {}) == "confirm"


def test_create_document_local_write(tmp_path):
    b = _ActionBridge()
    out = b._run_action_tool(
        "create_document",
        {
            "filename": "note.md",
            "content": "# Hi\n",
            "dest": "local",
            "dest_dir": str(tmp_path),
        },
    )
    assert out.startswith("✓")
    written = tmp_path / "note.md"
    assert written.read_text(encoding="utf-8") == "# Hi\n"


def test_create_document_strips_path_traversal(tmp_path):
    b = _ActionBridge()
    b._run_action_tool(
        "create_document",
        {
            "filename": "../evil.md",
            "content": "x",
            "dest": "local",
            "dest_dir": str(tmp_path),
        },
    )
    # basename() keeps it inside dest_dir.
    assert (tmp_path / "evil.md").exists()
    assert not (tmp_path.parent / "evil.md").exists()


def test_create_document_remote_uploads(tmp_path):
    b = _ActionBridge(hosts=[{"name": "prod", "ssh": "x"}])
    out = b._run_action_tool(
        "create_document",
        {
            "filename": "r.md",
            "content": "data",
            "dest": "prod",
            "dest_dir": "/tmp/docs",
        },
    )
    assert "uploaded" in out
    assert b.transfers, "expected an upload via start_transfer"
    (a, _k) = b.transfers[0]
    assert a[0] == "local" and a[2] == "prod" and a[3] == "/tmp/docs"


def test_run_runbook_renders_inline_without_spawning(monkeypatch):
    monkeypatch.setattr(
        memory,
        "load_runbook",
        lambda sid: {
            "id": "restart",
            "title": "Restart",
            "steps": "1. do {{svc}}",
            "args": {},
        },
    )
    b = _ActionBridge()
    out = b._run_action_tool(
        "run_runbook", {"runbook_id": "restart", "values": {"svc": "nginx"}}
    )
    assert "Restart" in out and "do nginx" in out
    # It returns text for the agent to follow — it does NOT start a chat turn.


def test_run_runbook_unknown(monkeypatch):
    monkeypatch.setattr(memory, "load_runbook", lambda sid: None)
    b = _ActionBridge()
    out = b._run_action_tool("run_runbook", {"runbook_id": "nope"})
    assert out.startswith("Error")


def test_create_schedule_persists(monkeypatch):
    b = _ActionBridge()
    out = b._run_action_tool(
        "create_schedule",
        {
            "id": "nightly",
            "action": "event",
            "event": "nightly_report",
            "when": {"cron": "0 2 * * *"},
        },
    )
    assert out.startswith("✓")
    assert b.created_schedules and b.created_schedules[0]["id"] == "nightly"


def test_create_schedule_prompt_persists(monkeypatch):
    b = _ActionBridge()
    out = b._run_action_tool(
        "create_schedule",
        {
            "id": "morning-check",
            "action": "prompt",
            "prompt": "Check disk usage and flag anything over 80%.",
            "when": {"cron": "0 8 * * *"},
        },
    )
    assert out.startswith("✓")
    saved = b.created_schedules[0]
    assert saved["action"] == "prompt" and saved["prompt"].startswith("Check disk")


def test_create_schedule_runbook_persists(monkeypatch):
    b = _ActionBridge()
    out = b._run_action_tool(
        "create_schedule",
        {
            "id": "weekly-report",
            "action": "runbook",
            "runbook": "build-report",
            "values": {"scope": "prod"},
            "when": {"every": "1d"},
        },
    )
    assert out.startswith("✓")
    saved = b.created_schedules[0]
    assert saved["action"] == "runbook" and saved["runbook"] == "build-report"
    assert saved["values"] == {"scope": "prod"}


def test_create_schedule_action_enum_includes_prompt_and_runbook(monkeypatch):
    monkeypatch.setattr(memory, "list_runbooks", lambda: [{"id": "build-report"}])
    b = _ActionBridge()
    cs = next(t for t in b._action_tools() if t["name"] == "create_schedule")
    enum = cs["input_schema"]["properties"]["action"]["enum"]
    assert {"event", "runnable", "prompt", "runbook"} == set(enum)
    # The runbook field offers the saved runbook ids as an enum.
    assert cs["input_schema"]["properties"]["runbook"]["enum"] == ["build-report"]


def test_create_schedule_runnable_and_host_are_selectors(monkeypatch):
    b = _ActionBridge(
        hosts=[{"name": "web01"}, {"name": "web02"}],
        runnables=[
            {"name": "backup", "host": "web01"},
            {"name": "backup", "host": "web02"},
            {"name": "restart", "host": "web01"},
        ],
    )
    cs = next(t for t in b._action_tools() if t["name"] == "create_schedule")
    props = cs["input_schema"]["properties"]
    # The runnable "tool" is an enum of distinct names.
    assert props["runnable"]["enum"] == ["backup", "restart"]
    # host is a selector; hosts is an array of the same selector (any-one).
    assert props["host"]["enum"] == ["web01", "web02"]
    assert props["hosts"]["items"]["enum"] == ["web01", "web02"]


def test_create_schedule_hosts_list_persists():
    b = _ActionBridge(hosts=[{"name": "web01"}, {"name": "web02"}])
    out = b._run_action_tool(
        "create_schedule",
        {
            "id": "any-backup",
            "action": "runnable",
            "runnable": "backup",
            "hosts": ["web01", "web02"],
            "when": {"every": "1h"},
        },
    )
    assert out.startswith("✓")
    assert b.created_schedules[0]["hosts"] == ["web01", "web02"]


def test_create_schedule_invalid_shape():
    b = _ActionBridge()
    out = b._run_action_tool(
        "create_schedule",
        {"id": "bad", "action": "event", "when": {}},  # no cron/every
    )
    assert out.startswith("Error")
    assert not b.created_schedules


def test_emit_event_feeds_engine():
    class _Engine:
        def __init__(self):
            self.fed = []

        def feed(self, event):
            self.fed.append(event)

    b = _ActionBridge()
    b._trigger_engine = _Engine()
    out = b._run_action_tool("emit_event", {"event": "go", "fields": {"x": "1"}})
    assert out.startswith("✓")
    assert b._trigger_engine.fed[0].type == "go"
    assert b._trigger_engine.fed[0].payload["x"] == "1"


def test_emit_event_without_engine():
    b = _ActionBridge()  # _trigger_engine is None
    out = b._run_action_tool("emit_event", {"event": "go"})
    assert out.startswith("✗")
