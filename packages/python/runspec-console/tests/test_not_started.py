"""
test_not_started.py — runs the console launched that never reached the runnable.

A runnable writes its own run record once ``parse()`` runs. When the console
couldn't get that far (host unreachable, runnable not installed, run-as refused,
the venv's Python couldn't start it) nothing recorded the run, so it was missing
from History and the console log. The console now keeps its own record in
``~/logs/runspec-console-not-started.log``, shown under the host it was meant for.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from runspec_console import executor as executor_mod
from runspec_console import not_started as ns
from runspec_console.bridge import Bridge

# What Jason saw: the venv's python ran the env prelude, whose os.execvp found no
# runnable in the venv it was pointed at.
PRELUDE_TRACEBACK = [
    "Traceback (most recent call last):",
    '  File "<string>", line 11, in <module>',
    '  File "/apps/python/3.10.13/lib/python3.10/os.py", line 574, in execvp',
    "    _execvpe(file, args)",
    '  File "/apps/python/3.10.13/lib/python3.10/os.py", line 616, in _execvpe',
    "    raise last_exc",
    '  File "/apps/python/3.10.13/lib/python3.10/os.py", line 607, in _execvpe',
    "    exec_func(fullname, *argrest)",
    "FileNotFoundError: [Errno 2] No such file or directory",
]


# ── launch_failure ───────────────────────────────────────────────────────────


def test_prelude_traceback_is_a_launch_failure():
    why = ns.launch_failure(PRELUDE_TRACEBACK, 1)
    assert why == (
        "the venv's Python couldn't start the runnable "
        "(FileNotFoundError: [Errno 2] No such file or directory)"
    )


def test_console_markers_are_launch_failures():
    assert ns.launch_failure(["✗  SSH connection failed: timed out"], -1) == (
        "SSH connection failed: timed out"
    )
    note = "✗  Couldn't run as svc on web-01: sudo didn't let ops run this."
    assert ns.launch_failure(["sudo: a password is required", note], 1) == note[3:]


def test_sh_prelude_not_found():
    why = ns.launch_failure(["sh: 1: exec: /opt/v/bin/x: not found"], 127)
    assert (
        why == "the runnable couldn't be started (sh: 1: exec: /opt/v/bin/x: not found)"
    )


def test_a_runnable_failing_on_its_own_is_not_matched():
    own = [
        "Traceback (most recent call last):",
        '  File "/opt/v/lib/python3.12/site-packages/tool/cli.py", line 9, in main',
        "    open('/nope')",
        "FileNotFoundError: [Errno 2] No such file or directory: '/nope'",
    ]
    assert ns.launch_failure(own, 1) == ""
    assert ns.launch_failure(PRELUDE_TRACEBACK, 0) == ""
    assert ns.launch_failure(["not found"], 127) == ""


# ── records + rows ───────────────────────────────────────────────────────────


def _write(log_dir: Path, *records):
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / ns.FILE).open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _rec(host, runnable="backup", ts="2026-11-13T10:00:00Z", run_id="r1", **kw):
    return ns.build_record(
        run_id=run_id, ts=ts, host=host, runnable=runnable, reason="nope", **kw
    )


def test_row_round_trip(tmp_path):
    _write(
        tmp_path,
        _rec(
            "web-01",
            group="ops-tools",
            command_path=["add"],
            args={"x": 1},
            operator="jason",
            run_as="svc",
            trigger="schedule",
            output=["a", "", "b"],
        ),
    )
    [row] = ns.read_rows(tmp_path)
    assert row["host"] == "web-01"
    assert row["status"] == "not_started"
    assert row["reason"] == "nope"
    assert row["exitCode"] == -1
    assert row["commandPath"] == ["add"]
    assert row["args"] == {"x": 1}
    assert row["runAs"] == "svc"
    assert row["trigger"] == "schedule"
    assert [x["message"] for x in row["logLines"]] == ["Didn't start: nope", "a", "b"]
    assert ns.find_row(tmp_path, "r1") == row
    assert ns.find_row(tmp_path, "other") is None


def test_read_rows_filters(tmp_path):
    _write(
        tmp_path,
        _rec("a", run_id="1", ts="2026-11-01T00:00:00Z"),
        _rec("b", run_id="2", ts="2026-11-12T00:00:00Z"),
        _rec("c", runnable="hidden", run_id="3", ts="2026-11-13T00:00:00Z"),
        {"ts": "x", "extra": {"event": "run_summary"}},  # not ours: ignored
    )
    ids = lambda rows: [r["runId"] for r in rows]  # noqa: E731
    assert ids(ns.read_rows(tmp_path)) == ["3", "2", "1"]
    assert ids(ns.read_rows(tmp_path, hosts={"a"})) == ["1"]
    assert ids(ns.read_rows(tmp_path, exclude_hosts={"a", "b"})) == ["3"]
    assert ids(ns.read_rows(tmp_path, since_date="2026-11-10")) == ["3", "2"]
    assert ids(ns.read_rows(tmp_path, exclude_runnables=frozenset({"hidden"}))) == [
        "2",
        "1",
    ]


# ── the bridge: record + History ─────────────────────────────────────────────


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    with (
        patch("runspec_console.hosts.load_hosts", return_value=[]),
        patch.object(Bridge, "_start_refresh_watcher"),
    ):
        b = Bridge()
    b._ssh_pool = None
    b._hosts = [
        {"name": "web-01", "ssh": "u@web-01", "runspec_paths": ["/v/bin/runspec"]},
        {"name": "db-01", "ssh": "u@db-01", "runspec_paths": ["/v/bin/runspec"]},
    ]
    b._connected_cache = {"web-01": True}
    return b


def test_record_writes_the_file_and_logs(bridge, tmp_path, caplog):
    with caplog.at_level("WARNING"):
        bridge._record_not_started(
            host="db-01",
            runnable="backup",
            reason="couldn't connect",
            trigger="schedule",
            output=["password is hunter2"],
            hide=["hunter2"],
        )
    assert "run didn't start: backup on db-01 (schedule) — couldn't connect" in (
        caplog.text
    )
    [row] = ns.read_rows(tmp_path / "logs")
    assert row["host"] == "db-01"
    assert row["logLines"][-1]["message"] == "password is ****"
    detail = bridge.get_run_detail("db-01", row["runId"])
    assert detail["logLines"][0]["message"] == "Didn't start: couldn't connect"


def test_history_shows_not_started_runs_under_their_host(bridge, tmp_path):
    bridge._record_not_started(host="web-01", runnable="backup", reason="x")
    bridge._record_not_started(host="db-01", runnable="backup", reason="y")
    bridge._record_not_started(host="local", runnable="tidy", reason="z")
    with patch.object(Bridge, "_slim_history_db", return_value=[]):
        local = bridge.get_history_slim("local", since_days=7)
        web = bridge.get_history_slim("web-01", since_days=7)
    # The local read never shows other hosts' runs as local, but carries the
    # ones for hosts the tab doesn't read (db-01 is disconnected).
    assert sorted((r["host"], r["runnable"]) for r in local) == [
        ("db-01", "backup"),
        ("local", "tidy"),
    ]
    assert [(r["host"], r["status"]) for r in web] == [("web-01", "not_started")]
    assert all(r["logLines"] == [] for r in local + web)  # slim


def test_history_all_has_every_not_started_run_once(bridge):
    bridge._record_not_started(host="web-01", runnable="backup", reason="x")
    bridge._record_not_started(host="db-01", runnable="backup", reason="y")
    with patch.object(Bridge, "_get_remote_history", return_value=[]):
        rows = bridge.get_history_all(since_days=7)
    assert sorted(r["host"] for r in rows) == ["db-01", "web-01"]


# ── invoke_runnable records the run ──────────────────────────────────────────


class _InvokeHarness:
    invoke_runnable = Bridge.invoke_runnable
    _ui_output_caps = Bridge._ui_output_caps
    _positionals_for = staticmethod(Bridge._positionals_for)
    _secret_arg_names = staticmethod(Bridge._secret_arg_names)
    _become_params = Bridge._become_params
    _live_host_records = Bridge._live_host_records

    def __init__(self, records=(), error=""):
        self._entry = {
            "name": "web-01",
            "ssh": None,
            "runspec_paths": ["/opt/venvs/ops-tools/bin/runspec"],
        }
        self._runnables_cache: list = []
        self._records = list(records)
        self._error = error
        self._lock = threading.Lock()
        self._in_flight: dict = {}
        self.recorded: list = []
        self.done = threading.Event()

    def _discover_on_demand(self, host):
        return list(self._records), self._error

    def _host_entry(self, _host):
        return self._entry

    def get_config(self):
        return {}

    def _credential_env(self, *_a):
        return {}, []

    def _apply_selected_credentials(self, *_a, **_k):
        return []

    def _declared_arg_names(self, *_a):
        return set()

    def _current_user(self):
        return "tester"

    def _play_sound(self, _name):
        pass

    def _persist_operator_run(self, **_kw):
        pass

    def _record_not_started(self, **kw):
        self.recorded.append(kw)

    def _dispatch(self, event, detail):
        if event == "runspec:run_end":
            self.done.set()


def _record(name="backup"):
    return {"name": name, "host": "web-01", "group": "ops-tools", "args": []}


def test_invoke_records_a_host_it_could_not_reach(monkeypatch):
    b = _InvokeHarness(error="couldn't connect to host 'web-01': timed out")
    b.invoke_runnable("web-01", "backup", {"x": 1}, trigger="schedule")
    assert b.done.wait(5)
    [rec] = b.recorded
    assert rec["host"] == "web-01"
    assert rec["reason"] == "couldn't connect to host 'web-01': timed out"
    assert rec["trigger"] == "schedule"
    assert rec["args"] == {"x": 1}


def test_invoke_records_a_prelude_failure_once(monkeypatch):
    b = _InvokeHarness([_record()])

    def fake_run_local(rp, runnable, args, cp, on_line, on_done, **kw):
        for line in PRELUDE_TRACEBACK:
            on_line(line, "stderr")
        on_done(1, 5)

    monkeypatch.setattr(executor_mod, "run_local", fake_run_local)
    b.invoke_runnable("web-01", "backup", {})
    assert b.done.wait(5)
    [rec] = b.recorded
    assert rec["exit_code"] == 1
    assert rec["reason"].startswith("the venv's Python couldn't start the runnable")
    assert rec["group"] == "ops-tools"


def test_invoke_does_not_record_a_run_that_started(monkeypatch):
    b = _InvokeHarness([_record()])

    def fake_run_local(rp, runnable, args, cp, on_line, on_done, **kw):
        on_line("Error: disk full", "stderr")
        on_done(1, 5)

    monkeypatch.setattr(executor_mod, "run_local", fake_run_local)
    b.invoke_runnable("web-01", "backup", {})
    assert b.done.wait(5)
    assert b.recorded == []
