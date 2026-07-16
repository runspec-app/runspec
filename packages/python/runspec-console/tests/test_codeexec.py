"""
test_codeexec.py — the workspace code-execution primitives (run_python / run_sql).

These drive the pure ``codeexec`` helpers directly, plus the ``_run_exec_tool``
dispatcher on a bare Bridge stub (mirroring test_agent_output_cap.py) to cover
path-jailing, the read-only SQL enforcement, timeouts, and large-output spill.
The security control for run_python is the confirm gate (tested via the autonomy
classifier), not the jail — the jail only bounds the file argument + cwd.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import types

import pytest

from runspec_console import codeexec
from runspec_console.bridge import Bridge


# ── classify_sql ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM t",
        "  select 1",
        "EXPLAIN QUERY PLAN SELECT 1",
        "PRAGMA table_info(t)",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "VALUES (1), (2)",
    ],
)
def test_classify_sql_reads(query):
    assert codeexec.classify_sql(query) == "read"


@pytest.mark.parametrize(
    "query",
    [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET x = 1",
        "DELETE FROM t",
        "CREATE TABLE t (x int)",
        "DROP TABLE t",
        "PRAGMA journal_mode = WAL",
        "WITH x AS (SELECT id FROM t) DELETE FROM t WHERE id IN (SELECT id FROM x)",
        "",
    ],
)
def test_classify_sql_writes(query):
    assert codeexec.classify_sql(query) == "write"


# ── run_python (pure) ──────────────────────────────────────────────────────────


def test_run_python_happy_path(tmp_path):
    r = codeexec.run_python(tmp_path, code="print(2 + 2)", timeout_s=10)
    assert r["stdout"] == "4\n"
    assert r["exit_code"] == 0
    assert r["timed_out"] is False


def test_run_python_cwd_is_workspace(tmp_path):
    r = codeexec.run_python(
        tmp_path, code="import os; print(os.getcwd())", timeout_s=10
    )
    assert r["stdout"].strip() == str(tmp_path.resolve())


def test_run_python_timeout_kills_process(tmp_path):
    r = codeexec.run_python(tmp_path, code="import time; time.sleep(30)", timeout_s=1)
    assert r["timed_out"] is True
    assert "timeout" in r["stderr"].lower()


def test_run_python_requires_exactly_one_source(tmp_path):
    with pytest.raises(ValueError):
        codeexec.run_python(tmp_path, timeout_s=5)  # neither
    with pytest.raises(ValueError):
        codeexec.run_python(tmp_path, code="x=1", file_rel="a.py", timeout_s=5)  # both


def test_run_python_file_jail_escape(tmp_path):
    with pytest.raises(ValueError):
        codeexec.run_python(tmp_path, file_rel="../evil.py", timeout_s=5)


def test_run_python_runs_a_workspace_file(tmp_path):
    (tmp_path / "job.py").write_text("print('from file')", encoding="utf-8")
    r = codeexec.run_python(tmp_path, file_rel="job.py", timeout_s=10)
    assert r["stdout"].strip() == "from file"
    assert r["exit_code"] == 0


# ── run_sql (pure) ─────────────────────────────────────────────────────────────


def _seed_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (id int, name text)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [(i, f"n{i}") for i in range(5)])
    conn.commit()
    conn.close()


def test_run_sql_read(tmp_path):
    db = tmp_path / "data.db"
    _seed_db(db)
    r = codeexec.run_sql(
        db, "SELECT id, name FROM t ORDER BY id", read_only=True, max_rows=100
    )
    assert r["columns"] == ["id", "name"]
    assert r["rows"][0] == [0, "n0"]
    assert r["returned"] == 5 and r["truncated"] is False


def test_run_sql_read_only_connection_rejects_write(tmp_path):
    db = tmp_path / "data.db"
    _seed_db(db)
    # A write classified as a read (or a fooled classifier) still can't mutate:
    # the read-only connection raises at the sqlite layer.
    with pytest.raises(sqlite3.OperationalError):
        codeexec.run_sql(
            db, "INSERT INTO t VALUES (9, 'x')", read_only=True, max_rows=100
        )


def test_run_sql_write_commits(tmp_path):
    db = tmp_path / "data.db"
    _seed_db(db)
    codeexec.run_sql(
        db, "INSERT INTO t VALUES (9, 'nine')", read_only=False, max_rows=100
    )
    r = codeexec.run_sql(
        db, "SELECT name FROM t WHERE id = 9", read_only=True, max_rows=10
    )
    assert r["rows"] == [["nine"]]


def test_run_sql_row_cap(tmp_path):
    db = tmp_path / "data.db"
    _seed_db(db)
    r = codeexec.run_sql(db, "SELECT id FROM t ORDER BY id", read_only=True, max_rows=2)
    assert r["returned"] == 2 and r["truncated"] is True


# ── _run_exec_tool (dispatch: jail, spill, disabled) ───────────────────────────


class _ExecBridge:
    _run_exec_tool = Bridge._run_exec_tool
    _current_workspace_root = Bridge._current_workspace_root

    def __init__(self):
        self._lock = threading.Lock()
        self._conversation_id = "convexec01"
        self._workspace_by_chat = {}


def _exec_stub(monkeypatch, tmp_path, *, code_exec=True):
    monkeypatch.setattr(
        "runspec_console.config.workspace_dir", lambda sid: tmp_path / sid
    )
    monkeypatch.setattr(
        "runspec_console.config.read_workspace",
        lambda: {
            "spill_threshold": 32_768,
            "headless_retention_days": 7,
            "code_exec": code_exec,
            "exec_timeout": 30,
        },
    )
    return _ExecBridge()


def test_exec_tool_python_happy(monkeypatch, tmp_path):
    b = _exec_stub(monkeypatch, tmp_path)
    out = b._run_exec_tool("run_python", {"code": "print('hi')"})
    data = json.loads(out)
    assert data["stdout"].strip() == "hi" and data["exit_code"] == 0


def test_exec_tool_large_python_output_spills(monkeypatch, tmp_path):
    b = _exec_stub(monkeypatch, tmp_path)
    out = b._run_exec_tool("run_python", {"code": "print('x' * 40000)"})
    assert "Output spilled to workspace" in out
    assert "tool-results/" in out
    # The full result landed on disk under the session workspace.
    files = list((tmp_path / b._conversation_id / "tool-results").glob("*"))
    assert len(files) == 1 and "x" * 100 in files[0].read_text()


def test_exec_tool_sql_jail_escape_is_error(monkeypatch, tmp_path):
    b = _exec_stub(monkeypatch, tmp_path)
    out = b._run_exec_tool("run_sql", {"db": "../secret.db", "query": "SELECT 1"})
    assert "escapes the workspace" in json.loads(out)["error"]


def test_exec_tool_spill_failure_falls_back_inline(monkeypatch, tmp_path):
    # A disk-full/unwritable spill during a large exec result must NOT fail the
    # tool call — it falls back to returning the payload inline (audit #5). Before
    # the fix the spill ran outside the try/except and the OSError escaped.
    b = _exec_stub(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise OSError("no space left on device")

    monkeypatch.setattr("runspec_console.workspaces.spill_output", boom)
    out = b._run_exec_tool("run_python", {"code": "print('y' * 40000)"})
    data = json.loads(out)  # valid JSON payload, not an exception
    assert data["exit_code"] == 0 and "y" * 100 in data["stdout"]


def test_exec_tool_sql_missing_db_is_error(monkeypatch, tmp_path):
    b = _exec_stub(monkeypatch, tmp_path)
    out = b._run_exec_tool("run_sql", {"db": "nope.db", "query": "SELECT 1"})
    assert "no such workspace database" in json.loads(out)["error"]


def test_exec_tool_no_workspace_is_error(monkeypatch, tmp_path):
    b = _exec_stub(monkeypatch, tmp_path)
    b._conversation_id = ""  # no active session
    out = b._run_exec_tool("run_python", {"code": "print(1)"})
    assert "No active session workspace" in json.loads(out)["error"]


def test_exec_tool_python_missing_code_is_directive(monkeypatch, tmp_path):
    # A model that emits run_python with no code gets a directive error naming the
    # keys it did send, so it recovers in one step instead of retrying empty.
    b = _exec_stub(monkeypatch, tmp_path)
    out = b._run_exec_tool("run_python", {"reason": "compute something"})
    data = json.loads(out)
    assert "no Python source received" in data["error"]
    assert "'code'" in data["error"]
    assert data["received_keys"] == ["reason"]


def test_exec_tool_python_both_code_and_file_rejected(monkeypatch, tmp_path):
    b = _exec_stub(monkeypatch, tmp_path)
    out = b._run_exec_tool("run_python", {"code": "print(1)", "file": "j.py"})
    assert "no Python source received" in json.loads(out)["error"]


# ── autonomy classification ────────────────────────────────────────────────────


def test_exec_autonomy_classification():
    s = types.SimpleNamespace()
    s._effective_tool_autonomy = types.MethodType(Bridge._effective_tool_autonomy, s)
    assert s._effective_tool_autonomy("run_python", {"code": "x=1"}) == "confirm"
    assert s._effective_tool_autonomy("run_sql", {"query": "SELECT 1"}) == "autonomous"
    assert (
        s._effective_tool_autonomy("run_sql", {"query": "DELETE FROM t"}) == "confirm"
    )


def test_exec_tools_dropped_when_disabled(monkeypatch):
    monkeypatch.setattr(
        "runspec_console.config.read_workspace",
        lambda: {"code_exec": False, "spill_threshold": 32_768, "exec_timeout": 30},
    )
    s = types.SimpleNamespace()
    s._exec_tools = types.MethodType(Bridge._exec_tools, s)
    assert s._exec_tools() == []
