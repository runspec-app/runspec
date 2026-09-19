"""
test_codeexec.py — the sandboxed code-execution path (run_python_sandboxed /
run_sql_sandboxed).

Two layers:
  * the **pure** ``codeexec`` helpers (SQL read/write classification, code-input
    normalisation) — no runtime, run everywhere;
  * the WASM sandbox (``sandbox.run_python`` / ``sandbox.run_sql``) and the
    ``_run_exec_tool`` dispatcher — these need the WASI CPython binary, so they're
    guarded by ``requires_sandbox`` and skip when it isn't present (set
    ``RUNSPEC_SANDBOX_WASM`` / install the ``[sandbox]`` extra to run them).

The security boundary itself (no network, no host filesystem escape) is asserted
directly against the sandbox — it is no longer "the confirm gate is the only
control".
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import types

import pytest

from runspec_console import codeexec, sandbox
from runspec_console.bridge import Bridge
from tests.session_stub import install_sessions
from runspec_console.bridge_const import ToolCancelled

_EXEC_READY = sandbox._wasmtime() is not None and sandbox.wasm_path() is not None
requires_sandbox = pytest.mark.skipif(
    not _EXEC_READY,
    reason="WASM sandbox binary not available (set RUNSPEC_SANDBOX_WASM or install "
    "the [sandbox] extra)",
)


# ── classify_sql (pure) ────────────────────────────────────────────────────────


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


# ── normalize_code (pure) ──────────────────────────────────────────────────────


def test_normalize_code_string():
    assert codeexec.normalize_code("x = 1") == "x = 1"


def test_normalize_code_list_joins_lines():
    # The array form lets the agent avoid escaping newlines in a JSON string.
    assert codeexec.normalize_code(["import os", "print(1)"]) == "import os\nprint(1)"


def test_normalize_code_empty_is_none():
    assert codeexec.normalize_code("") is None
    assert codeexec.normalize_code([]) is None
    assert codeexec.normalize_code(None) is None
    assert codeexec.normalize_code(42) is None


# ── sandbox.run_python (real WASM) ─────────────────────────────────────────────


@requires_sandbox
def test_run_python_happy_path(tmp_path):
    r = sandbox.run_python(tmp_path, code="print(2 + 2)", timeout_s=15)
    assert r["stdout"].strip() == "4"
    assert r["exit_code"] == 0
    assert r["timed_out"] is False


@requires_sandbox
def test_run_python_array_code_equals_string(tmp_path):
    r = sandbox.run_python(
        tmp_path, code=["a = 2", "b = 3", "print(a * b)"], timeout_s=15
    )
    assert r["stdout"].strip() == "6" and r["exit_code"] == 0


@requires_sandbox
def test_run_python_cwd_is_workspace_and_relative_read(tmp_path):
    (tmp_path / "data.json").write_text('{"v": 7}', encoding="utf-8")
    r = sandbox.run_python(
        tmp_path,
        code="import os, json; print(os.getcwd()); print(json.load(open('data.json'))['v'])",
        timeout_s=15,
    )
    lines = r["stdout"].strip().splitlines()
    assert lines[0] == "/ws"  # the workspace is the guest cwd
    assert lines[1] == "7"  # relative open resolves inside the workspace


@requires_sandbox
def test_run_python_no_network(tmp_path):
    r = sandbox.run_python(
        tmp_path,
        code="import socket; socket.create_connection(('1.1.1.1', 80), 2)",
        timeout_s=15,
    )
    assert r["exit_code"] != 0  # WASI exposes no sockets — the connect can't happen


@requires_sandbox
def test_run_python_no_host_fs_escape(tmp_path):
    r = sandbox.run_python(tmp_path, code="open('/etc/passwd')", timeout_s=15)
    assert r["exit_code"] != 0
    assert "FileNotFoundError" in r["stderr"] or "No such file" in r["stderr"]


@requires_sandbox
def test_run_python_timeout_kills_even_a_blocking_sleep(tmp_path):
    # A blocking host syscall (time.sleep) is unkillable in-process; the killable
    # subprocess is why the wall-clock timeout is a hard guarantee.
    t0 = time.monotonic()
    r = sandbox.run_python(tmp_path, code="import time; time.sleep(30)", timeout_s=2)
    assert r["timed_out"] is True
    assert time.monotonic() - t0 < 8  # killed near the deadline, not after 30s
    assert "timeout" in r["stderr"].lower()


@requires_sandbox
def test_run_python_cancel_during_sleep_raises(tmp_path):
    ev = threading.Event()
    threading.Thread(target=lambda: (time.sleep(0.5), ev.set()), daemon=True).start()
    with pytest.raises(ToolCancelled):
        sandbox.run_python(
            tmp_path,
            code="import time; time.sleep(30)",
            timeout_s=30,
            cancel=lambda: ev.is_set(),
        )


@requires_sandbox
def test_run_python_requires_exactly_one_source(tmp_path):
    with pytest.raises(ValueError):
        sandbox.run_python(tmp_path, timeout_s=5)  # neither
    with pytest.raises(ValueError):
        sandbox.run_python(tmp_path, code="x=1", file_rel="a.py", timeout_s=5)  # both


@requires_sandbox
def test_run_python_file_jail_escape(tmp_path):
    with pytest.raises(ValueError):
        sandbox.run_python(tmp_path, file_rel="../evil.py", timeout_s=5)


@requires_sandbox
def test_run_python_runs_a_workspace_file(tmp_path):
    (tmp_path / "job.py").write_text("print('from file')", encoding="utf-8")
    r = sandbox.run_python(tmp_path, file_rel="job.py", timeout_s=15)
    assert r["stdout"].strip() == "from file" and r["exit_code"] == 0


# ── sandbox.run_sql (real WASM) ────────────────────────────────────────────────


def _seed_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (id int, name text)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [(i, f"n{i}") for i in range(5)])
    conn.commit()
    conn.close()


@requires_sandbox
def test_run_sql_read(tmp_path):
    _seed_db(tmp_path / "data.db")
    r = sandbox.run_sql(
        tmp_path,
        "data.db",
        "SELECT id, name FROM t ORDER BY id",
        read_only=True,
        max_rows=100,
        timeout_s=15,
    )
    assert r["columns"] == ["id", "name"]
    assert r["rows"][0] == [0, "n0"]
    assert r["returned"] == 5 and r["truncated"] is False


@requires_sandbox
def test_run_sql_read_only_rejects_write(tmp_path):
    _seed_db(tmp_path / "data.db")
    # A write mis-classified as a read still can't mutate: mode=ro raises in-guest.
    with pytest.raises(RuntimeError):
        sandbox.run_sql(
            tmp_path,
            "data.db",
            "INSERT INTO t VALUES (9, 'x')",
            read_only=True,
            max_rows=100,
            timeout_s=15,
        )


@requires_sandbox
def test_run_sql_write_commits(tmp_path):
    _seed_db(tmp_path / "data.db")
    sandbox.run_sql(
        tmp_path,
        "data.db",
        "INSERT INTO t VALUES (9, 'nine')",
        read_only=False,
        max_rows=100,
        timeout_s=15,
    )
    r = sandbox.run_sql(
        tmp_path,
        "data.db",
        "SELECT name FROM t WHERE id = 9",
        read_only=True,
        max_rows=10,
        timeout_s=15,
    )
    assert r["rows"] == [["nine"]]


@requires_sandbox
def test_run_sql_row_cap(tmp_path):
    _seed_db(tmp_path / "data.db")
    r = sandbox.run_sql(
        tmp_path,
        "data.db",
        "SELECT id FROM t ORDER BY id",
        read_only=True,
        max_rows=2,
        timeout_s=15,
    )
    assert r["returned"] == 2 and r["truncated"] is True


# ── _run_exec_tool (dispatch: jail, spill, availability, disabled) ─────────────


class _ExecBridge:
    _run_exec_tool = Bridge._run_exec_tool
    _current_workspace_root = Bridge._current_workspace_root

    def __init__(self):
        self._lock = threading.Lock()
        install_sessions(self, "convexec01")


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


@requires_sandbox
def test_exec_tool_python_happy(monkeypatch, tmp_path):
    b = _exec_stub(monkeypatch, tmp_path)
    out = b._run_exec_tool("run_python_sandboxed", {"code": "print('hi')"})
    data = json.loads(out)
    assert data["stdout"].strip() == "hi" and data["exit_code"] == 0


@requires_sandbox
def test_exec_tool_python_array_code(monkeypatch, tmp_path):
    b = _exec_stub(monkeypatch, tmp_path)
    out = b._run_exec_tool("run_python_sandboxed", {"code": ["x = 21", "print(x * 2)"]})
    assert json.loads(out)["stdout"].strip() == "42"


@requires_sandbox
def test_exec_tool_large_python_output_spills(monkeypatch, tmp_path):
    b = _exec_stub(monkeypatch, tmp_path)
    out = b._run_exec_tool("run_python_sandboxed", {"code": "print('x' * 40000)"})
    assert "Output spilled to workspace" in out
    assert "tool-results/" in out
    files = list((tmp_path / b._chat_session_id / "tool-results").glob("*"))
    assert len(files) == 1 and "x" * 100 in files[0].read_text()


@requires_sandbox
def test_exec_tool_sql_jail_escape_is_error(monkeypatch, tmp_path):
    b = _exec_stub(monkeypatch, tmp_path)
    out = b._run_exec_tool(
        "run_sql_sandboxed", {"db": "../secret.db", "query": "SELECT 1"}
    )
    assert "escapes the workspace" in json.loads(out)["error"]


@requires_sandbox
def test_exec_tool_spill_failure_falls_back_inline(monkeypatch, tmp_path):
    b = _exec_stub(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise OSError("no space left on device")

    monkeypatch.setattr("runspec_console.workspaces.spill_output", boom)
    out = b._run_exec_tool("run_python_sandboxed", {"code": "print('y' * 40000)"})
    data = json.loads(out)  # valid JSON payload, not an exception
    assert data["exit_code"] == 0 and "y" * 100 in data["stdout"]


@requires_sandbox
def test_exec_tool_sql_missing_db_is_error(monkeypatch, tmp_path):
    b = _exec_stub(monkeypatch, tmp_path)
    out = b._run_exec_tool("run_sql_sandboxed", {"db": "nope.db", "query": "SELECT 1"})
    assert "no such workspace database" in json.loads(out)["error"]


@requires_sandbox
def test_exec_tool_python_missing_code_is_directive(monkeypatch, tmp_path):
    b = _exec_stub(monkeypatch, tmp_path)
    out = b._run_exec_tool("run_python_sandboxed", {"reason": "compute something"})
    data = json.loads(out)
    assert "no Python source received" in data["error"]
    assert "'code'" in data["error"]
    assert data["received_keys"] == ["reason"]


def test_exec_tool_no_workspace_is_error(monkeypatch, tmp_path):
    # Checked before the sandbox availability gate, so it needs no binary.
    b = _exec_stub(monkeypatch, tmp_path)
    b._current_workspace_root = lambda chat_id="": None  # no session workspace
    out = b._run_exec_tool("run_python_sandboxed", {"code": "print(1)"})
    assert "No active session workspace" in json.loads(out)["error"]


def test_exec_tool_unavailable_is_error(monkeypatch, tmp_path):
    # When the sandbox runtime/binary can't be made ready, the call fails closed
    # with an actionable message — never a host-execution fallback.
    b = _exec_stub(monkeypatch, tmp_path)
    monkeypatch.setattr("runspec_console.sandbox.available", lambda: False)
    monkeypatch.setattr(
        "runspec_console.sandbox.unavailable_reason", lambda: "runtime not installed"
    )
    out = b._run_exec_tool("run_python_sandboxed", {"code": "print(1)"})
    assert "code sandbox is unavailable" in json.loads(out)["error"]


# ── autonomy classification + tool listing ─────────────────────────────────────


def test_exec_autonomy_classification():
    s = types.SimpleNamespace()
    s._effective_tool_autonomy = types.MethodType(Bridge._effective_tool_autonomy, s)
    assert (
        s._effective_tool_autonomy("run_python_sandboxed", {"code": "x=1"}) == "confirm"
    )
    assert (
        s._effective_tool_autonomy("run_sql_sandboxed", {"query": "SELECT 1"})
        == "autonomous"
    )
    assert (
        s._effective_tool_autonomy("run_sql_sandboxed", {"query": "DELETE FROM t"})
        == "confirm"
    )


def test_exec_tools_dropped_when_code_exec_disabled(monkeypatch):
    monkeypatch.setattr(
        "runspec_console.config.read_workspace",
        lambda: {"code_exec": False, "spill_threshold": 32_768, "exec_timeout": 30},
    )
    s = types.SimpleNamespace()
    s._exec_tools = types.MethodType(Bridge._exec_tools, s)
    assert s._exec_tools() == []


def test_exec_tools_dropped_when_sandbox_unavailable(monkeypatch):
    monkeypatch.setattr(
        "runspec_console.config.read_workspace",
        lambda: {"code_exec": True, "spill_threshold": 32_768, "exec_timeout": 30},
    )
    monkeypatch.setattr("runspec_console.sandbox.available", lambda: False)
    s = types.SimpleNamespace()
    s._exec_tools = types.MethodType(Bridge._exec_tools, s)
    assert s._exec_tools() == []  # fail-closed


def test_exec_tools_listed_with_sandbox_names(monkeypatch):
    monkeypatch.setattr(
        "runspec_console.config.read_workspace",
        lambda: {"code_exec": True, "spill_threshold": 32_768, "exec_timeout": 30},
    )
    monkeypatch.setattr("runspec_console.sandbox.available", lambda: True)
    s = types.SimpleNamespace()
    s._exec_tools = types.MethodType(Bridge._exec_tools, s)
    names = {t["name"] for t in s._exec_tools()}
    assert names == {"run_python_sandboxed", "run_sql_sandboxed"}
