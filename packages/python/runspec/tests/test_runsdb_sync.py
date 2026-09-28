"""Tests for the runs index's v4 change sequence (runsdb schema v4) — the substrate
of the console's incremental history pull (docs/design/history-incremental-pull.md).

Every writer path advances ``seq``; deletes leave tombstones (cleared when the row
comes back); a DB migrated to v4 keeps stamping ``seq`` when an *older* runspec
writes to it; and the ``changes`` contract (delta / reset / legacy / none, capped
pages) delivers every change exactly once.
"""

from __future__ import annotations

import contextlib
import os
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


def _row(run_id="r1", **over):
    base = {
        "run_id": run_id,
        "runnable": "backup",
        "command": "",
        "status": "running",
        "start_ts": runsdb._now_iso(),
        "user": "alice",
        "agent": False,
        "trigger": "",
        "skill": "",
        "autonomy": "",
        "pid": os.getpid(),
        "host": runsdb._hostname(),
        "proc_start": runsdb.proc_start_token(),
        "log_file": f"backup.{run_id}.log",
        "args": {},
    }
    base.update(over)
    return base


@contextlib.contextmanager
def _raw(logs_dir: Path) -> Iterator[sqlite3.Connection]:
    """A raw connection that commits *and closes* on exit (sqlite3's own context
    manager only commits — an unclosed connection is a ResourceWarning on 3.13)."""
    con = sqlite3.connect(str(runsdb.db_path(logs_dir)))
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def _seq(logs_dir: Path, run_id: str) -> int | None:
    with _raw(logs_dir) as con:
        r = con.execute("SELECT seq FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return r["seq"] if r else None


def _meta_seq(logs_dir: Path) -> int:
    with _raw(logs_dir) as con:
        return int(con.execute("SELECT value FROM sync_meta WHERE key='seq'").fetchone()[0])


def _tombstones(logs_dir: Path) -> dict[str, int]:
    with _raw(logs_dir) as con:
        return {r["run_id"]: r["seq"] for r in con.execute("SELECT run_id, seq FROM runs_tombstones")}


def _pull_all(logs_dir: Path, db_id=None, seq=None, limit=2000, window_start=None):
    """Apply pages until ``more`` is false — what the console does. Returns the
    final cursor, the live rows by id, and every page (to count deliveries)."""
    held: dict[str, dict] = {}
    pages = []
    while True:
        res = runsdb.changes_since(logs_dir, db_id=db_id, seq=seq, limit=limit, window_start=window_start)
        pages.append(res)
        if res["mode"] == "reset":
            held.clear()
        for r in res["rows"]:
            held[r["run_id"]] = r
        for rid in res["deleted"]:
            held.pop(rid, None)
        db_id, seq = res["db_id"], res["seq"]
        if not res["more"]:
            return (db_id, seq), held, pages


# ── schema + every writer path ─────────────────────────────────────────────────


def test_fresh_db_is_v4(logs_dir):
    runsdb.begin_run(logs_dir, _row())
    with _raw(logs_dir) as con:
        assert runsdb._has_sync(con)
        assert con.execute("PRAGMA user_version").fetchone()[0] == 4
        meta = runsdb.sync_state(con)
    assert meta and meta["db_id"] and meta["seq"] == 1 and meta["floor"] == 0
    assert _seq(logs_dir, "r1") == 1


def test_every_writer_path_advances_seq(logs_dir):
    runsdb.begin_run(logs_dir, _row("a"))
    s = _seq(logs_dir, "a")

    def bumped(run_id):
        nonlocal s
        new = _seq(logs_dir, run_id)
        assert new is not None and new > s, f"{run_id}: seq {new} did not advance past {s}"
        s = new

    runsdb.finish_run(logs_dir, "a", exit_code=0, duration_ms=5, status="completed")
    bumped("a")
    runsdb.set_log_file(logs_dir, "a", "ignored.log")  # log_file already set → no change
    assert _seq(logs_dir, "a") == s
    runsdb.mark_log_pruned(logs_dir, ["backup.a.log"], pruned_by="ops")
    bumped("a")
    runsdb.repoint_logs(logs_dir, {"backup.a.log": "archive.log"})
    bumped("a")
    runsdb.reindex(logs_dir, [{"run_id": "a", "runnable": "backup", "exit_code": 0, "ts": runsdb._now_iso(), "duration_ms": 5}])
    bumped("a")
    # a dead-pid running row reaped to crashed
    runsdb.begin_run(logs_dir, _row("b", pid=999_999_999, proc_start="1"))
    s = _seq(logs_dir, "b")
    assert runsdb.reap_now(logs_dir) == 1
    bumped("b")
    # set_log_file on a row without one
    with _raw(logs_dir) as con:
        con.execute("UPDATE runs SET log_file=NULL WHERE run_id='b'")
    s = _seq(logs_dir, "b")
    runsdb.set_log_file(logs_dir, "b", "b.log")
    bumped("b")


def test_delete_leaves_tombstone_and_reinsert_clears_it(logs_dir):
    runsdb.begin_run(logs_dir, _row("old", status="completed", start_ts="2020-01-01T00:00:00+00:00"))
    assert runsdb.prune_now(logs_dir, retention_days=30, max_rows=0) == 1
    tomb = _tombstones(logs_dir)
    assert tomb["old"] == _meta_seq(logs_dir)
    # reindex restores the pruned row → its tombstone goes (never both in a pull)
    runsdb.reindex(logs_dir, [{"run_id": "old", "runnable": "backup", "exit_code": 0, "ts": "2020-01-01T00:00:01+00:00"}])
    assert "old" not in _tombstones(logs_dir)
    assert _seq(logs_dir, "old") == _meta_seq(logs_dir)


def test_adopt_child_row_tombstones_the_wrapper(logs_dir):
    since = runsdb._now_iso()
    runsdb.begin_run(logs_dir, _row("wrap", label="sched:x"))
    runsdb.begin_run(logs_dir, _row("child"))
    assert runsdb.adopt_child_row(logs_dir, "wrap", runnable="backup", since_ts=since, label="sched:x") == "child"
    assert "wrap" in _tombstones(logs_dir)


# ── v3 → v4 migration ──────────────────────────────────────────────────────────


def _make_v3(logs_dir: Path, ids) -> None:
    """A DB as runspec 0.62 left it: v3 DDL, no seq, user_version 3."""
    con = sqlite3.connect(str(runsdb.db_path(logs_dir)))
    con.executescript(runsdb._DDL)
    for i, rid in enumerate(ids):
        con.execute(
            "INSERT INTO runs (run_id, runnable, status, start_ts) VALUES (?, 'backup', 'completed', ?)",
            (rid, f"2026-09-2{i}T00:00:00+00:00"),
        )
    con.execute("PRAGMA user_version=3")
    con.commit()
    con.close()


def test_migration_backfills_in_rowid_order(logs_dir):
    _make_v3(logs_dir, ["x", "y", "z"])
    runsdb.begin_run(logs_dir, _row("new"))
    assert [_seq(logs_dir, r) for r in ("x", "y", "z", "new")] == [1, 2, 3, 4]
    assert _meta_seq(logs_dir) == 4


def test_finish_run_fallback_insert_survives_the_migrating_connection(logs_dir):
    # The first connection after upgrade runs the migration (writes to sync_meta and
    # backfills). finish_run must still see its own UPDATE matched nothing and
    # insert the fallback row — it once keyed on con.total_changes.
    _make_v3(logs_dir, ["x"])
    runsdb.finish_run(logs_dir, "lost-start", exit_code=0, duration_ms=1, status="completed", runnable="backup")
    assert _seq(logs_dir, "lost-start") is not None


def test_old_writer_on_a_v4_db_still_gets_seq(logs_dir):
    """An older runspec (0.60-0.62) writing a v4 DB: it re-runs its own DDL, resets
    user_version, and writes with explicit column lists — the triggers still stamp
    seq and record tombstones, and the DB is still detected as v4 by structure."""
    runsdb.begin_run(logs_dir, _row("new"))
    con = sqlite3.connect(str(runsdb.db_path(logs_dir)))
    con.executescript(runsdb._DDL)
    con.execute("PRAGMA user_version=2")
    con.execute("INSERT INTO runs (run_id, runnable, status, start_ts) VALUES ('old1', 'backup', 'running', ?)", (runsdb._now_iso(),))
    con.execute("UPDATE runs SET status='completed' WHERE run_id='old1'")
    con.execute("DELETE FROM runs WHERE run_id='new'")
    con.commit()
    con.close()
    assert _seq(logs_dir, "old1") == _meta_seq(logs_dir) - 1
    assert _tombstones(logs_dir)["new"] == _meta_seq(logs_dir)
    with _raw(logs_dir) as c:
        assert runsdb._has_sync(c)
    st = runsdb.stats(logs_dir)
    assert st["schema_version"] == 4 and st["sync"]["seq"] == _meta_seq(logs_dir)


# ── the pull contract ──────────────────────────────────────────────────────────


def test_none_and_legacy_modes(logs_dir, tmp_path):
    assert runsdb.changes_since(tmp_path / "nowhere")["mode"] == "none"
    _make_v3(logs_dir, ["x"])
    assert runsdb.changes_since(logs_dir)["mode"] == "legacy"


def test_snapshot_then_delta_delivers_each_change_once(logs_dir):
    for i in range(3):
        runsdb.begin_run(logs_dir, _row(f"r{i}"))
    cursor, held, pages = _pull_all(logs_dir)
    assert pages[0]["mode"] == "reset" and set(held) == {"r0", "r1", "r2"}
    # a write committed after the snapshot comes in the next delta, and only once
    runsdb.finish_run(logs_dir, "r1", exit_code=2, duration_ms=9, status="failed")
    runsdb.begin_run(logs_dir, _row("r3"))
    cursor2, held2, pages2 = _pull_all(logs_dir, *cursor)
    assert pages2[0]["mode"] == "delta"
    assert [r["run_id"] for r in pages2[0]["rows"]] == ["r1", "r3"]
    assert held2["r1"]["status"] == "failed"
    # nothing new → an empty delta that keeps the cursor
    _, held3, pages3 = _pull_all(logs_dir, *cursor2)
    assert pages3[0]["mode"] == "delta" and not held3 and pages3[0]["seq"] == cursor2[1]


def test_delta_carries_deletes(logs_dir):
    runsdb.begin_run(logs_dir, _row("keep"))
    runsdb.begin_run(logs_dir, _row("gone", start_ts="2020-01-01T00:00:00+00:00", status="completed"))
    cursor, _, _ = _pull_all(logs_dir)
    runsdb.prune_now(logs_dir, retention_days=30, max_rows=0)
    res = runsdb.changes_since(logs_dir, db_id=cursor[0], seq=cursor[1])
    assert res["mode"] == "delta" and res["deleted"] == ["gone"] and res["rows"] == []


def test_capped_pages_converge_in_seq_order(logs_dir):
    for i in range(7):
        runsdb.begin_run(logs_dir, _row(f"r{i}", status="completed", start_ts=f"2026-09-2{i}T00:00:00+00:00"))
    cursor, held, pages = _pull_all(logs_dir, limit=3)
    assert [p["more"] for p in pages] == [True, True, False]
    assert pages[0]["mode"] == "reset" and all(p["mode"] == "delta" for p in pages[1:])
    delivered = [r["run_id"] for p in pages for r in p["rows"]]
    assert delivered == [f"r{i}" for i in range(7)]  # each exactly once, in seq order
    assert cursor[1] == _meta_seq(logs_dir)
    # a page mixing rows and tombstones, capped
    runsdb.prune_now(logs_dir, retention_days=0, max_rows=5)  # r0 and r1 go
    runsdb.begin_run(logs_dir, _row("r7"))
    _, _, pages = _pull_all(logs_dir, *cursor, limit=2)
    assert [(p["deleted"], [r["run_id"] for r in p["rows"]]) for p in pages] == [(["r0", "r1"], []), ([], ["r7"])]


def test_reset_on_new_db_id_floor_and_future_cursor(logs_dir):
    runsdb.begin_run(logs_dir, _row("a"))
    db_id, seq = _pull_all(logs_dir)[0]
    assert runsdb.changes_since(logs_dir, db_id="other", seq=seq)["mode"] == "reset"
    assert runsdb.changes_since(logs_dir, db_id=db_id, seq=seq + 10)["mode"] == "reset"
    # tombstones trimmed past the cursor → floor rises above it → reset
    runsdb.begin_run(logs_dir, _row("b", status="completed", start_ts="2020-01-01T00:00:00+00:00"))
    runsdb.prune_now(logs_dir, retention_days=30, max_rows=0)
    with _raw(logs_dir) as con:
        runsdb._trim_tombstones(con, days=0)
        con.execute("UPDATE runs_tombstones SET deleted_at='2000-01-01T00:00:00Z'")
        runsdb._trim_tombstones(con, days=1)
        con.commit()
        assert runsdb.sync_state(con)["floor"] > seq
    assert not _tombstones(logs_dir)
    res = runsdb.changes_since(logs_dir, db_id=db_id, seq=seq)
    assert res["mode"] == "reset" and [r["run_id"] for r in res["rows"]] == ["a"]


def test_tombstone_cap_raises_floor(logs_dir):
    for i in range(4):
        runsdb.begin_run(logs_dir, _row(f"r{i}", status="completed", start_ts="2020-01-01T00:00:00+00:00"))
    runsdb.prune_now(logs_dir, retention_days=30, max_rows=0)
    before = _tombstones(logs_dir)
    with _raw(logs_dir) as con:
        runsdb._trim_tombstones(con, cap=2)
        con.commit()
        floor = runsdb.sync_state(con)["floor"]
    kept = _tombstones(logs_dir)
    assert len(kept) == 2 and floor == sorted(before.values())[1] and min(kept.values()) > floor


def test_window_start_filters_rows(logs_dir):
    runsdb.begin_run(logs_dir, _row("old", start_ts="2020-01-01T00:00:00+00:00"))
    runsdb.begin_run(logs_dir, _row("new"))
    res = runsdb.changes_since(logs_dir, window_start="2026-01-01T00:00:00+00:00")
    assert [r["run_id"] for r in res["rows"]] == ["new"] and res["seq"] == _meta_seq(logs_dir)
