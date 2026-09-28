"""Tests for the runs index's v5 columns — ``events`` (log-record counts by level)
and ``exception`` (the uncaught exception's type + message), taken from the
run_summary so a reader can aggregate from the index instead of the log files.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from runspec import runsdb


@pytest.fixture
def logs_dir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    return d


@contextlib.contextmanager
def _raw(logs_dir: Path) -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(str(runsdb.db_path(logs_dir)))
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def _get(logs_dir: Path, run_id: str) -> dict:
    row = runsdb.get_run(logs_dir, run_id)
    assert row is not None
    return row


def _begin(logs_dir: Path, run_id: str = "r1") -> None:
    runsdb.begin_run(logs_dir, {"run_id": run_id, "runnable": "backup", "status": "running", "start_ts": runsdb._now_iso()})


def test_finish_stores_events_and_exception(logs_dir):
    _begin(logs_dir)
    exc = {"type": "ValueError", "message": "bad", "traceback": "Traceback …"}
    runsdb.finish_run(logs_dir, "r1", exit_code=1, duration_ms=5, status="failed", events={"INFO": 3, "ERROR": 1, "DEBUG": 0}, exception=exc)
    row = _get(logs_dir, "r1")
    assert json.loads(row["events"]) == {"ERROR": 1, "INFO": 3}  # zero counts dropped
    assert json.loads(row["exception"]) == {"type": "ValueError", "message": "bad"}  # no traceback


def test_a_run_that_logged_nothing_is_empty_not_unknown(logs_dir):
    _begin(logs_dir)
    runsdb.finish_run(logs_dir, "r1", exit_code=0, duration_ms=5, status="completed", events={"INFO": 0})
    row = _get(logs_dir, "r1")
    assert row["events"] == "{}" and row["exception"] is None


def test_exception_message_is_capped(logs_dir):
    _begin(logs_dir)
    runsdb.finish_run(logs_dir, "r1", exit_code=1, duration_ms=5, status="failed", events={}, exception={"type": "E", "message": "x" * 5000})
    assert len(json.loads(_get(logs_dir, "r1")["exception"])["message"]) == runsdb._EXCEPTION_MESSAGE_MAX


def test_a_later_finalize_without_them_keeps_them(logs_dir):
    # `runspec exec` finalizes its child's row after the child did (observed exit
    # code); it doesn't know the events and must not clear them.
    _begin(logs_dir)
    runsdb.finish_run(logs_dir, "r1", exit_code=0, duration_ms=5, status="completed", events={"INFO": 2}, exception={"type": "E", "message": "m"})
    runsdb.finish_run(logs_dir, "r1", exit_code=3, duration_ms=9, status="failed")
    row = _get(logs_dir, "r1")
    assert row["exit_code"] == 3 and json.loads(row["events"]) == {"INFO": 2} and row["exception"]
    # …and a begin upsert racing in after a finish doesn't either
    runsdb.begin_run(logs_dir, {"run_id": "r1", "runnable": "backup", "status": "running"})
    assert json.loads(_get(logs_dir, "r1")["events"]) == {"INFO": 2}


def test_fallback_insert_carries_them(logs_dir):
    runsdb.finish_run(logs_dir, "lost", exit_code=1, duration_ms=5, status="failed", runnable="backup", events={"ERROR": 1}, exception="boom")
    row = _get(logs_dir, "lost")
    assert json.loads(row["events"]) == {"ERROR": 1}
    assert json.loads(row["exception"]) == {"type": "", "message": "boom"}


def test_reindex_fills_them_from_run_summary(logs_dir):
    recs = [
        {"run_id": "a", "runnable": "backup", "exit_code": 1, "ts": runsdb._now_iso(), "events": {"WARNING": 4}, "exception": {"type": "OSError", "message": "disk"}},
        {"run_id": "b", "runnable": "backup", "exit_code": 0, "ts": runsdb._now_iso(), "events": {}},
        {"run_id": "c", "runnable": "backup", "exit_code": 0, "ts": runsdb._now_iso()},  # an old summary: unknown
    ]
    assert runsdb.reindex(logs_dir, recs) == 3
    assert json.loads(_get(logs_dir, "a")["events"]) == {"WARNING": 4}
    assert json.loads(_get(logs_dir, "a")["exception"])["type"] == "OSError"
    assert _get(logs_dir, "b")["events"] == "{}"
    assert _get(logs_dir, "c")["events"] is None


def test_v4_db_migrates_and_reports_v5(logs_dir):
    # A v4 index (runspec 0.63): no events/exception columns.
    _begin(logs_dir, "old")
    with _raw(logs_dir) as con:
        con.execute("ALTER TABLE runs DROP COLUMN events")
        con.execute("ALTER TABLE runs DROP COLUMN exception")
        con.execute("PRAGMA user_version=4")
    assert runsdb.stats(logs_dir)["schema_version"] == 4
    _begin(logs_dir, "new")  # a write migrates
    runsdb.finish_run(logs_dir, "new", exit_code=0, duration_ms=1, status="completed", events={"INFO": 1})
    assert _get(logs_dir, "old")["events"] is None  # unknown for rows written before
    assert json.loads(_get(logs_dir, "new")["events"]) == {"INFO": 1}
    assert runsdb.stats(logs_dir)["schema_version"] == 5


def test_changes_carry_them(logs_dir):
    _begin(logs_dir)
    res = runsdb.changes_since(logs_dir)
    cursor = (res["db_id"], res["seq"])
    runsdb.finish_run(logs_dir, "r1", exit_code=0, duration_ms=1, status="completed", events={"INFO": 1})
    res = runsdb.changes_since(logs_dir, db_id=cursor[0], seq=cursor[1])
    assert [json.loads(r["events"]) for r in res["rows"]] == [{"INFO": 1}]
