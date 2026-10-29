"""Tests for the built-in 'action' agent tools (create_document, run_runbook,
create_schedule, emit_event) — schema presence, confirm-gating, and execution.

These mirror the lightweight-stub pattern in test_relay_filter.py: a tiny bridge
that borrows just the methods under test, so no pywebview/COM/LLM is needed."""

from __future__ import annotations

import json
import threading

from runspec_console import memory
from runspec_console.bridge import Bridge
from tests.session_stub import install_sessions
from runspec_console.bridge_const import _ACTION_TOOL_NAMES


class _ActionBridge:
    _custom_agent_tools = Bridge._custom_agent_tools
    _denylist_tool = Bridge._denylist_tool
    _outlook_tools = Bridge._outlook_tools
    _repo_tools = Bridge._repo_tools
    _memory_tools = Bridge._memory_tools
    _knowledge_tools = Bridge._knowledge_tools
    _status_tools = Bridge._status_tools
    _history_tools = Bridge._history_tools
    _files_tools = Bridge._files_tools
    _web_tools = Bridge._web_tools
    _diagram_tools = Bridge._diagram_tools
    _improve_tools = Bridge._improve_tools
    _marketplace_agent_tools = Bridge._marketplace_agent_tools
    _workspace_tools = Bridge._workspace_tools
    _exec_tools = Bridge._exec_tools
    _action_tools = Bridge._action_tools
    _config_tools = Bridge._config_tools
    _mcp_tools = Bridge._mcp_tools
    _run_action_tool = Bridge._run_action_tool
    _action_create_document = Bridge._action_create_document
    _action_run_runbook = Bridge._action_run_runbook
    _action_create_schedule = Bridge._action_create_schedule
    _action_emit_event = Bridge._action_emit_event
    _action_open_url = Bridge._action_open_url
    _action_open_file = Bridge._action_open_file
    _action_reveal_in_folder = Bridge._action_reveal_in_folder
    _action_play_sound = Bridge._action_play_sound
    _play_sound_id = Bridge._play_sound_id
    _resolve_jailed_path = Bridge._resolve_jailed_path
    _current_workspace_root = Bridge._current_workspace_root
    _run_workspace_tool = Bridge._run_workspace_tool
    _effective_tool_autonomy = Bridge._effective_tool_autonomy
    _tag_run_origin_runbook = Bridge._tag_run_origin_runbook
    _set_run_origin = Bridge._set_run_origin

    def __init__(self, hosts=None, runnables=None):
        self._hosts = hosts or []
        self._runnables_cache = runnables or []
        self._filters = []
        self._credentials = []
        self._lock = threading.Lock()
        self._trigger_engine = None
        install_sessions(self, "convact01")
        self.transfers: list = []
        self.created_schedules: list = []
        self._run_origin: dict = {}

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
    # create_document / create_schedule / emit_event / open_url / open_file /
    # reveal_in_folder are always offered.
    assert {
        "create_document",
        "create_schedule",
        "emit_event",
        "open_url",
        "open_file",
        "reveal_in_folder",
    } <= names


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


def test_all_side_effecting_action_tools_are_confirm_gated():
    b = _ActionBridge()
    # run_runbook returns text (autonomous); play_sound is a benign local audio cue
    # (autonomous) — both stay in _ACTION_TOOL_NAMES only so action_allow can scope
    # them. Everything that actually acts on the world confirm-gates.
    for name in _ACTION_TOOL_NAMES - {"run_runbook", "play_sound"}:
        assert b._effective_tool_autonomy(name, {}) == "confirm"


def test_run_runbook_is_autonomous_but_still_action_allow_scopable():
    """run_runbook returns *text* — a runbook's rendered steps — and every side
    effect happens later, when the agent calls the runnables those steps name, each
    at its own autonomy level. Gating the read protected nothing downstream and cost
    a prompt on a common path.

    It stays in _ACTION_TOOL_NAMES so a trigger's `action_allow` can still scope it:
    that set does double duty as the allowlist vocabulary, which is why the autonomy
    exception is a special case rather than dropping the membership."""
    b = _ActionBridge()
    assert b._effective_tool_autonomy("run_runbook", {}) == "autonomous"
    assert "run_runbook" in _ACTION_TOOL_NAMES

    from runspec_console.bridge import Bridge

    scoped = Bridge._filter_headless_tools(
        [{"name": "run_runbook"}, {"name": "create_document"}], ["create_document"]
    )
    assert [t["name"] for t in scoped] == ["create_document"]


def test_play_sound_schema_offers_builtin_cues():
    from runspec_console import sounds

    b = _ActionBridge()
    tool = next(t for t in b._action_tools() if t["name"] == "play_sound")
    enum = tool["input_schema"]["properties"]["sound"]["enum"]
    assert tool["input_schema"]["required"] == ["sound"]
    # every bundled cue is a valid choice even with no registered sounds.
    for name in sounds.KNOWN_SOUNDS:
        assert name in enum


def test_play_sound_is_autonomous_but_action_allow_scopable():
    b = _ActionBridge()
    assert b._effective_tool_autonomy("play_sound", {}) == "autonomous"
    assert "play_sound" in _ACTION_TOOL_NAMES
    scoped = Bridge._filter_headless_tools(
        [{"name": "play_sound"}, {"name": "open_url"}], ["play_sound"]
    )
    assert [t["name"] for t in scoped] == ["play_sound"]


def test_play_sound_dispatch_unknown_id():
    b = _ActionBridge()
    out = b._run_action_tool("play_sound", {"sound": "does-not-exist"})
    assert out.startswith("Error: no sound named")


def test_play_sound_dispatch_known_id_reports_outcome():
    # Off Windows, sounds.available() is False, so the tool reports it requested
    # the cue but nothing played — never an error, and it never raises.
    b = _ActionBridge()
    out = b._run_action_tool("play_sound", {"sound": "confirm"})
    assert out.startswith("✓") and "confirm" in out


def test_open_url_schema():
    b = _ActionBridge()
    tool = next(t for t in b._action_tools() if t["name"] == "open_url")
    assert tool["input_schema"]["required"] == ["url"]


def test_open_url_opens_web_url(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "webbrowser.open", lambda url, new=0: seen.update(url=url, new=new) or True
    )
    b = _ActionBridge()
    out = b._run_action_tool(
        "open_url", {"url": "https://teams.microsoft.com/l/meetup-join/xyz"}
    )
    assert seen["url"] == "https://teams.microsoft.com/l/meetup-join/xyz"
    assert out.startswith("✓ Opened")


def test_open_url_rejects_non_web_scheme(monkeypatch):
    called = []
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: called.append(a) or True)
    b = _ActionBridge()
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "not a url", ""):
        out = b._run_action_tool("open_url", {"url": bad})
        assert out.startswith("Error:")
    assert called == []  # never handed a non-web URL to the browser


def test_open_url_reports_no_browser(monkeypatch):
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: False)
    b = _ActionBridge()
    out = b._run_action_tool("open_url", {"url": "https://example.com"})
    assert out.startswith("✗ No browser")


# ── open_file / reveal_in_folder / workspace_info ─────────────────────────────


def _ws_stub(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "runspec_console.config.workspace_dir", lambda sid: tmp_path / "ws" / sid
    )
    monkeypatch.setattr("runspec_console.config.app_dir", lambda: tmp_path / "app")
    return _ActionBridge()


def test_openable_ext():
    from runspec_console.bridge_agent_tools import _openable_ext

    assert _openable_ext("report.pdf") and _openable_ext("IMG.PNG")
    # executables and script-capable formats are refused
    assert not _openable_ext("payload.exe")
    assert not _openable_ext("page.html")
    assert not _openable_ext("noext")


def test_workspace_info_reports_paths(monkeypatch, tmp_path):
    b = _ws_stub(monkeypatch, tmp_path)
    out = json.loads(b._run_workspace_tool("workspace_info", {}))
    assert out["session_id"] == "convact01"
    assert out["workspace_root"].endswith("convact01")
    assert out["documents_dir"].endswith("documents")
    from runspec_console import config as config_mod

    assert out["config_dir"] == str(config_mod.app_dir())


def test_workspace_ls_includes_root(monkeypatch, tmp_path):
    b = _ws_stub(monkeypatch, tmp_path)
    out = json.loads(b._run_workspace_tool("workspace_ls", {}))
    assert out["root"].endswith("convact01")
    assert out["entries"] == []


def test_open_file_opens_allowlisted(monkeypatch, tmp_path):
    b = _ws_stub(monkeypatch, tmp_path)
    notes = tmp_path / "ws" / "convact01" / "notes"
    notes.mkdir(parents=True)
    (notes / "report.pdf").write_bytes(b"%PDF-1.4")
    opened = []
    monkeypatch.setattr(
        "runspec_console.bridge_agent_tools._launch_open", opened.append
    )
    out = b._run_action_tool("open_file", {"path": "notes/report.pdf"}, "")
    assert out.startswith("✓ Opened")
    assert opened and opened[0].endswith("report.pdf")


def test_open_file_rejects_executable(monkeypatch, tmp_path):
    b = _ws_stub(monkeypatch, tmp_path)
    ws = tmp_path / "ws" / "convact01"
    ws.mkdir(parents=True)
    (ws / "payload.exe").write_bytes(b"MZ")
    called = []
    monkeypatch.setattr(
        "runspec_console.bridge_agent_tools._launch_open", called.append
    )
    out = b._run_action_tool("open_file", {"path": "payload.exe"}, "")
    assert out.startswith("Error:") and "not allowed" in out
    assert called == []  # never handed a non-allowlisted file to the OS


def test_open_file_rejects_jail_escape(monkeypatch, tmp_path):
    b = _ws_stub(monkeypatch, tmp_path)
    (tmp_path / "ws" / "convact01").mkdir(parents=True)
    out = b._run_action_tool("open_file", {"path": "../../secret.pdf"}, "")
    assert out.startswith("Error:") and "escape" in out.lower()


def test_open_file_from_documents_location(monkeypatch, tmp_path):
    b = _ws_stub(monkeypatch, tmp_path)
    docs = tmp_path / "app" / "documents"
    docs.mkdir(parents=True)
    (docs / "note.md").write_text("# hi")
    opened = []
    monkeypatch.setattr(
        "runspec_console.bridge_agent_tools._launch_open", opened.append
    )
    out = b._run_action_tool(
        "open_file", {"path": "note.md", "location": "documents"}, ""
    )
    assert out.startswith("✓ Opened") and opened


def test_reveal_in_folder_allows_any_type(monkeypatch, tmp_path):
    b = _ws_stub(monkeypatch, tmp_path)
    ws = tmp_path / "ws" / "convact01"
    ws.mkdir(parents=True)
    (ws / "payload.exe").write_bytes(b"MZ")  # open_file would refuse this
    revealed = []
    monkeypatch.setattr(
        "runspec_console.bridge_agent_tools._launch_reveal", revealed.append
    )
    out = b._run_action_tool("reveal_in_folder", {"path": "payload.exe"}, "")
    assert out.startswith("✓ Revealed")
    assert revealed and revealed[0].endswith("payload.exe")


def test_reveal_in_folder_rejects_missing(monkeypatch, tmp_path):
    b = _ws_stub(monkeypatch, tmp_path)
    (tmp_path / "ws" / "convact01").mkdir(parents=True)
    out = b._run_action_tool("reveal_in_folder", {"path": "nope.txt"}, "")
    assert out.startswith("Error:") and "does not exist" in out


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
        "run_runbook", {"runbook_id": "restart", "values": {"svc": "nginx"}}, "chat1"
    )
    assert "Restart" in out and "do nginx" in out
    # It returns text for the agent to follow — it does NOT start a chat turn.
    # It does tag provenance, so the runnables the steps drive are attributed to
    # this runbook in History/Analytics the way the Forms path's are.
    assert b._run_origin["chat1"]["runbook"] == "restart"


def test_run_runbook_provenance_never_clears_the_trigger_flag(monkeypatch):
    """_run_config_tool refuses when the run origin names a trigger — that is one of
    the two layers stopping automation rewriting its own config. Tagging the runbook
    must add a field, never replace the record, or a rule trigger could reach the
    config tools by first following a runbook."""
    monkeypatch.setattr(
        memory,
        "load_runbook",
        lambda sid: {"id": "rb", "title": "T", "steps": "1. go", "args": {}},
    )
    b = _ActionBridge()
    b._set_run_origin("card1", "email-trigger", agent=True)
    b._run_action_tool("run_runbook", {"runbook_id": "rb"}, "card1")
    assert b._run_origin["card1"] == {
        "trigger": "email-trigger",
        "agent": True,
        "runbook": "rb",
    }


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
    assert {
        "event",
        "runnable",
        "prompt",
        "runbook",
        "saved_run",
        "saved_transfer",
        "eval",
    } == set(enum)
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


# ── run_saved_run action tool ─────────────────────────────────────────────────


class _SavedRunActionBridge(_ActionBridge):
    _action_run_saved_run = Bridge._action_run_saved_run
    _saved_run_entry = Bridge._saved_run_entry
    _saved_run_calls = Bridge._saved_run_calls
    _pick_connected_host = Bridge._pick_connected_host
    _format_group_output = staticmethod(Bridge._format_group_output)

    def __init__(self, hosts=None, runnables=None):
        super().__init__(hosts, runnables)
        self._connected_cache = {}
        self.parallel_calls: list = []

    def _host_entry(self, h):
        return {"name": h, "ssh": "user@" + h}

    def _group_members(self, g):
        return []

    def _host_has_runnable(self, h, r):
        return True

    def _run_calls_parallel(self, calls, chat_id):
        self.parallel_calls.append((calls, chat_id))
        return ["OK" for _ in calls]


def _seed_action_saved_runs(tmp_path, monkeypatch, rows):
    from runspec_console import savedruns

    monkeypatch.setattr(
        "runspec_console.config.saved_runs_path",
        lambda: tmp_path / "runspec_saved_runs.toml",
    )
    savedruns.save_saved_runs(rows)


def test_run_saved_run_tool_only_when_saved_runs_exist(tmp_path, monkeypatch):
    _seed_action_saved_runs(tmp_path, monkeypatch, [])
    b = _SavedRunActionBridge()
    assert "run_saved_run" not in {t["name"] for t in b._action_tools()}
    _seed_action_saved_runs(
        tmp_path,
        monkeypatch,
        [
            {
                "id": "flush",
                "title": "Flush DNS",
                "runnable": "flush-dns",
                "host": "web01",
                "params": {"reason": {"required": True}},
            }
        ],
    )
    tool = next(t for t in b._action_tools() if t["name"] == "run_saved_run")
    assert tool["input_schema"]["properties"]["id"]["enum"] == ["flush"]
    # The menu names the runnable, target, and params.
    assert "flush-dns" in tool["description"]
    assert "web01" in tool["description"]
    assert "reason" in tool["description"]
    # …and in _ACTION_TOOL_NAMES so the gate treats it as an action tool.
    assert "run_saved_run" in _ACTION_TOOL_NAMES


def test_action_run_saved_run_executes_expanded_calls(tmp_path, monkeypatch):
    _seed_action_saved_runs(
        tmp_path,
        monkeypatch,
        [
            {
                "id": "flush",
                "runnable": "flush-dns",
                "selected_hosts": ["w1", "w2"],
                "args": {"x": "1"},
            }
        ],
    )
    b = _SavedRunActionBridge()
    out = b._run_action_tool("run_saved_run", {"id": "flush"}, "chat1")
    assert "Ran saved run 'flush'" in out
    (calls, chat_id) = b.parallel_calls[0]
    assert [c[0] for c in calls] == ["w1__flush-dns", "w2__flush-dns"]
    assert chat_id == "chat1"


def test_action_run_saved_run_invalid_values(tmp_path, monkeypatch):
    _seed_action_saved_runs(
        tmp_path,
        monkeypatch,
        [
            {
                "id": "flush",
                "runnable": "flush-dns",
                "host": "w1",
                "params": {"reason": {"required": True}},
            }
        ],
    )
    b = _SavedRunActionBridge()
    out = b._run_action_tool("run_saved_run", {"id": "flush"}, "chat1")
    assert out.startswith("Error")
    assert b.parallel_calls == []


def test_create_schedule_passes_saved_run_through(tmp_path, monkeypatch):
    _seed_action_saved_runs(
        tmp_path,
        monkeypatch,
        [{"id": "flush", "runnable": "flush-dns", "host": "w1"}],
    )
    b = _SavedRunActionBridge()
    out = b._run_action_tool(
        "create_schedule",
        {
            "id": "nightly-flush",
            "action": "saved_run",
            "saved_run": "flush",
            "values": {"reason": "nightly"},
            "when": {"cron": "0 2 * * *"},
        },
    )
    assert out.startswith("✓")
    saved = b.created_schedules[0]
    assert saved["action"] == "saved_run" and saved["saved_run"] == "flush"
    assert saved["values"] == {"reason": "nightly"}
    # …and the schema offers saved-run ids as an enum.
    cs = next(t for t in b._action_tools() if t["name"] == "create_schedule")
    assert cs["input_schema"]["properties"]["saved_run"]["enum"] == ["flush"]


def test_create_schedule_passes_command_through():
    b = _ActionBridge(
        hosts=[{"name": "prod-1"}], runnables=[{"name": "backup", "host": "prod-1"}]
    )
    out = b._run_action_tool(
        "create_schedule",
        {
            "id": "nightly-restore",
            "action": "runnable",
            "host": "prod-1",
            "runnable": "backup",
            "command": ["db", "restore"],
            "args": {"target": "main"},
            "when": {"cron": "0 2 * * *"},
        },
    )
    assert out.startswith("✓")
    saved = b.created_schedules[0]
    assert saved["command"] == ["db", "restore"]
