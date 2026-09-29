"""The logs inbox, console side (runspec >= 0.66 — docs/design/venv-logs-inbox.md).

* the stdlib snippet fragment reads a venv's inbox exactly like core's
  ``runspec.inbox.inbox_rows`` (the one parser) — parity, like the change-sequence
  snippet's;
* the read / sync / running snippets carry those rows (``pending``), and refuse a
  runs index in a world-writable or sticky dir;
* the log stream's frames carry a host-generated nonce, so a log file's content
  can't forge a frame; a per-run file yields only its own run;
* the History cache keeps finished inbox runs as a per-pull snapshot, the index
  winning a dedupe.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from runspec import inbox

from runspec_console import bridge_utils
from runspec_console.history_cache import HistoryCache

POSIX = os.name == "posix" and inbox.supported()


def _rec(
    run_id: str,
    event: str | None = None,
    ts: str = "2026-09-01T10:00:00+00:00",
    **extra,
) -> str:
    e = {"run_id": run_id, **extra}
    if event:
        e["event"] = event
    return json.dumps(
        {
            "ts": ts,
            "level": "INFO",
            "logger": "runspec.runsummary",
            "message": "m",
            "extra": e,
        }
    )


def _snippet(fragment_call: str, home: Path) -> object:
    code = (
        bridge_utils._INBOX_FRAGMENT
        + "\nimport sys\nsys.stdout.write(json.dumps("
        + fragment_call
        + "))\n"
    )
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout
    return json.loads(out)


@unittest.skipUnless(POSIX, "the inbox is POSIX-only")
class TestInboxParity(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.logs = self.tmp / "v1" / "logs"
        self.logs.mkdir(parents=True)
        assert inbox.ensure_inbox(self.logs) == "created"
        self.box = self.logs / "inbox"

    def _file(self, rid: str, *lines: str, runnable: str = "demo") -> Path:
        p = self.box / f"{runnable}.20260901T100000Z.{rid}.log"
        p.write_text("\n".join(lines) + "\n")
        os.chmod(p, 0o644)
        return p

    def test_the_snippet_reads_the_inbox_like_core(self) -> None:
        done, crashed, forged, exec_run = (str(uuid.uuid4()) for _ in range(4))
        self._file(
            done,
            _rec(
                done,
                "run_start",
                runnable="demo",
                start_ts="2026-09-01T09:00:00+00:00",
                invocation_args={"x": "1"},
            ),
            _rec(done, "run_summary", exit_code=2, duration_ms=5, events={"INFO": 1}),
        )
        # No finish record, lock free, a pid that's gone on this host → crashed.
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        self._file(
            crashed,
            _rec(
                crashed,
                "run_start",
                runnable="demo",
                pid=gone.pid,
                host=bridge_utils_host(),
                proc_start="1",
            ),
        )
        # A per-run file claiming another run's records yields only its own.
        self._file(
            forged,
            _rec(forged, "run_start", runnable="demo"),
            _rec(done, "run_summary", exit_code=0),
        )
        self._file(
            exec_run,
            _rec(exec_run, "run_start", runnable="demo", label="sched:x"),
            _rec(exec_run, "run_summary", exit_code=0),
            _rec(exec_run, "exec_result", status="failed", exit_code=137),
        )
        (self.box / "junk.txt").write_text("x")
        os.symlink(
            "/etc/hostname", self.box / f"demo.20260901T100000Z.{uuid.uuid4()}.log"
        )

        core = sorted(inbox.inbox_rows(self.logs), key=lambda r: r["run_id"])
        snip = sorted(
            _snippet(f"_rs_inbox({str(self.logs)!r})", self.tmp),
            key=lambda r: r["run_id"],
        )  # type: ignore[arg-type]
        self.assertEqual(json.loads(json.dumps(core, default=str)), snip)
        by = {r["run_id"]: r for r in snip}
        self.assertEqual(set(by), {done, crashed, forged, exec_run})
        self.assertEqual(by[done]["status"], "failed")
        self.assertEqual(by[crashed]["status"], "crashed")
        self.assertEqual(
            by[forged]["status"], "crashed"
        )  # its own run: unfinished, and long dead
        self.assertEqual(
            (by[exec_run]["status"], by[exec_run]["exit_code"], by[exec_run]["label"]),
            ("failed", 137, "sched:x"),
        )
        self.assertTrue(all(r["pending"] and r["source"] == "inbox" for r in snip))

    def test_a_live_run_reads_running_by_its_lock(self) -> None:
        import fcntl

        rid = str(uuid.uuid4())
        p = self._file(rid, _rec(rid, "run_start", runnable="demo"))
        fd = os.open(p, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            [row] = _snippet(f"_rs_inbox({str(self.logs)!r})", self.tmp)  # type: ignore[misc]
            self.assertEqual(row["status"], "running")
        finally:
            os.close(fd)

    def test_the_sync_snippet_carries_pending_and_the_running_one_live_runs(
        self,
    ) -> None:
        from runspec import runsdb

        runsdb.begin_run(
            self.logs,
            {
                "run_id": "indexed",
                "runnable": "demo",
                "status": "completed",
                "start_ts": "2026-09-01T09:00:00+00:00",
            },
        )
        rid = str(uuid.uuid4())
        self._file(
            rid,
            _rec(rid, "run_start", runnable="demo"),
            _rec(rid, "run_summary", exit_code=0),
        )
        d = str(self.logs)
        cmd = bridge_utils.db_sync_cmd("python", {d: {}}, limit=100, window_start=None)
        out = json.loads(_run(cmd, self.tmp))
        self.assertEqual(out[d]["mode"], "reset")
        self.assertEqual([r["run_id"] for r in out[d]["pending"]], [rid])
        cmd = bridge_utils.db_sync_cmd(
            "python", {d: {}}, limit=100, window_start=None, inbox=False
        )
        self.assertNotIn("pending", json.loads(_run(cmd, self.tmp))[d])
        read = json.loads(
            _run(bridge_utils.db_read_cmd("python", [d], None, 100), self.tmp)
        )
        self.assertEqual([r["run_id"] for r in read[d]["pending"]], [rid])
        running = json.loads(
            _run(bridge_utils.db_running_cmd("python", [d], 100), self.tmp)
        )
        self.assertEqual(running[d]["pending"], [])  # finished, not live

    def test_a_world_writable_logs_dir_is_never_read_as_an_index(self) -> None:
        from runspec import runsdb

        runsdb.begin_run(
            self.logs,
            {
                "run_id": "r",
                "runnable": "demo",
                "status": "completed",
                "start_ts": "2026-09-01T09:00:00+00:00",
            },
        )
        os.chmod(self.logs, 0o1777)
        d = str(self.logs)
        out = json.loads(
            _run(
                bridge_utils.db_sync_cmd(
                    "python", {d: {}}, limit=100, window_start=None
                ),
                self.tmp,
            )
        )
        self.assertEqual(out[d]["mode"], "error")
        read = json.loads(
            _run(bridge_utils.db_read_cmd("python", [d], None, 100), self.tmp)
        )
        self.assertFalse(read[d]["has_db"])


def bridge_utils_host() -> str:
    import socket

    return socket.gethostname()


def _run(cmd: str, home: Path) -> str:
    argv = shlex.split(cmd)
    argv[0] = sys.executable
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    return subprocess.run(
        argv, capture_output=True, text=True, env=env, check=True
    ).stdout


class TestFrames(unittest.TestCase):
    def test_a_file_cant_forge_a_frame(self) -> None:
        n = "0123abcd"
        stream = "\n".join(
            [
                f"\x00RUNSPEC_NONCE:{n}",
                f"\x00RUNSPEC_LOG:{n}:a.20260901T100000Z.x",
                '{"ts": "1"}',
                "\x00RUNSPEC_LOG:evil",  # inside a.'s content — no nonce
                f"\x00RUNSPEC_LOG:wrong:{n}",
                f"\x00RUNSPEC_LOG:{n}:b",
                '{"ts": "2"}',
            ]
        )
        frames = bridge_utils.iter_log_frames(stream)
        self.assertEqual([f[0] for f in frames], ["a.20260901T100000Z.x", "b"])
        self.assertIn("RUNSPEC_LOG:evil", frames[0][1])

    def test_an_old_stream_without_a_nonce_still_parses(self) -> None:
        frames = bridge_utils.iter_log_frames(
            "\x00RUNSPEC_LOG:a\n{}\n\x00RUNSPEC_LOG:b\n{}"
        )
        self.assertEqual([f[0] for f in frames], ["a", "b"])

    @unittest.skipUnless(os.name == "posix", "a POSIX shell")
    def test_the_script_announces_a_nonce_and_frames_with_it(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        (tmp / "demo.log").write_text('{"ts": "1"}\n')
        os.symlink("/etc/hostname", tmp / "evil.log")
        out = subprocess.run(
            ["sh", "-c", bridge_utils._remote_log_script([str(tmp)], None)],
            capture_output=True,
            text=True,
        ).stdout
        self.assertTrue(out.startswith("\x00RUNSPEC_NONCE:"))
        self.assertEqual(
            [f[0] for f in bridge_utils.iter_log_frames(out)], ["demo"]
        )  # the symlink is skipped

    def test_a_per_run_file_yields_only_its_own_run(self) -> None:
        mine, theirs = str(uuid.uuid4()), str(uuid.uuid4())
        text = "\n".join(
            [
                _rec(mine, "run_summary", runnable="demo", exit_code=0),
                _rec(theirs, "run_summary", runnable="forged", exit_code=0),
            ]
        )
        recs = bridge_utils._parse_log_text(f"demo.20260901T100000Z.{mine}", text, "h")
        self.assertEqual([r["runId"] for r in recs], [mine])


class TestPendingCache(unittest.TestCase):
    def test_the_index_wins_and_the_snapshot_is_replaced(self) -> None:
        cache = HistoryCache(Path(tempfile.mkdtemp()) / "c.db")
        try:
            rec = lambda rid, ts, **kw: {"runId": rid, "ts": ts, **kw}  # noqa: E731
            cache.apply(
                "h",
                "/v/logs",
                {"mode": "reset", "db_id": "x", "seq": 1},
                [rec("a", "2026-09-02", pending=False)],
                [],
            )
            self.assertTrue(
                cache.set_pending(
                    "h",
                    "/v/logs",
                    [
                        rec("a", "2026-09-02", pending=True),
                        rec("b", "2026-09-03", pending=True),
                    ],
                )
            )
            got = {
                r["runId"]: r["pending"]
                for r in cache.read("h", since_date=None, limit=10)
            }
            self.assertEqual(got, {"a": False, "b": True})  # the index's "a" wins
            self.assertFalse(
                cache.set_pending(
                    "h",
                    "/v/logs",
                    [
                        rec("a", "2026-09-02", pending=True),
                        rec("b", "2026-09-03", pending=True),
                    ],
                )
            )
            cache.set_pending("h", "/v/logs", [])  # swept
            self.assertEqual(
                [r["runId"] for r in cache.read("h", since_date=None, limit=10)], ["a"]
            )
            cache.set_pending("h", "/v/logs", [rec("c", "2026-09-04", pending=True)])
            self.assertEqual(
                [
                    r["runId"]
                    for r in cache.read("h", since_date="2026-09-03", limit=10)
                ],
                ["c"],
            )
            cache.drop_dirs("h", set())
            self.assertEqual(cache.read("h", since_date=None, limit=10), [])
        finally:
            cache.close()

    def test_records_carry_account_and_pending(self) -> None:
        rec = bridge_utils.db_row_to_record(
            {
                "run_id": "r",
                "runnable": "d",
                "status": "completed",
                "os_user": "alice",
                "pending": True,
                "log_file": "d.x.log",
            },
            "v",
            "h",
        )
        self.assertEqual(
            (rec["account"], rec["pending"], rec["logFile"]), ("alice", True, "d.x.log")
        )
        run = bridge_utils.db_row_to_running(
            {"run_id": "r", "runnable": "d", "os_user": "bob", "pending": True},
            "v",
            "h",
        )
        self.assertEqual((run["account"], run["pending"]), ("bob", True))
        self.assertEqual(
            bridge_utils.db_row_to_record(
                {"run_id": "r", "runnable": "d", "status": "completed"}, "v", "h"
            )["account"],
            "",
        )


class TestUnsafeDirIsPosixOnly(unittest.TestCase):
    def test_windows_dirs_are_never_refused(self) -> None:
        # Windows reports every directory 0o777, so the world-writable check must
        # not run there (it made every index read as unsafe — the sync snippet
        # answered "error" for every dir on the Windows CI runner).
        from unittest.mock import patch

        ns: dict = {}
        exec(bridge_utils._INBOX_FRAGMENT, ns)
        d = tempfile.mkdtemp()
        os.chmod(d, 0o777)
        with patch.object(ns["os"], "name", "nt"):
            self.assertFalse(ns["_rs_unsafe"](d))
        if os.name == "posix":
            self.assertTrue(
                ns["_rs_unsafe"](d)
            )  # a world-writable dir on POSIX is refused


class TestReadOnlyOpenFallsBackToImmutable(unittest.TestCase):
    """``mode=ro`` on a WAL index needs to create ``-shm``, which a non-owner can't
    in an owner-only (0755) logs dir — the snippets must still read the rows."""

    def _db(self) -> str:
        import sqlite3

        d = tempfile.mkdtemp()
        db = os.path.join(d, "runspec_runs.db")
        con = sqlite3.connect(db)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE runs (run_id TEXT)")
        con.execute("INSERT INTO runs VALUES ('r1')")
        con.commit()
        con.close()  # last connection: -wal/-shm are removed
        return db

    def _ns(self, refuse_plain_ro: bool) -> dict:
        import sqlite3

        ns: dict = {}
        exec(bridge_utils._INBOX_FRAGMENT, ns)
        real = sqlite3.connect
        seen: list[str] = []

        class _Refusing:
            def execute(self, *a, **k):  # what SQLite says when it can't make -shm
                raise sqlite3.OperationalError("attempt to write a readonly database")

            def close(self) -> None:
                pass

        def connect(target, *a, **k):
            seen.append(target)
            if refuse_plain_ro and "immutable" not in target:
                return _Refusing()
            return real(target, *a, **k)

        fake = type(
            "M",
            (),
            {
                "connect": staticmethod(connect),
                "OperationalError": sqlite3.OperationalError,
            },
        )
        ns["sqlite3"] = fake
        ns["_seen"] = seen
        return ns

    def test_plain_ro_is_used_when_it_works(self) -> None:
        db = self._db()
        ns = self._ns(refuse_plain_ro=False)
        con = ns["_rs_ro"](db)
        self.assertEqual(con.execute("SELECT run_id FROM runs").fetchall(), [("r1",)])
        self.assertEqual(len(ns["_seen"]), 1)

    def test_falls_back_to_immutable_when_shm_cant_be_created(self) -> None:
        db = self._db()
        ns = self._ns(refuse_plain_ro=True)
        con = ns["_rs_ro"](db)
        self.assertEqual(con.execute("SELECT run_id FROM runs").fetchall(), [("r1",)])
        self.assertIn("immutable=1", ns["_seen"][-1])

    def test_no_immutable_read_while_a_wal_holds_uncheckpointed_rows(self) -> None:
        import sqlite3

        db = self._db()
        Path(db + "-wal").write_bytes(b"")
        ns = self._ns(refuse_plain_ro=True)
        with self.assertRaises(sqlite3.OperationalError):
            ns["_rs_ro"](db)
