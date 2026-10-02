"""The shared-venv logs inbox (core 0.66.0 — docs/design/venv-logs-inbox.md).

Two tiers:

* **Unit** (any POSIX account): the parser, layout checks, provisioning by the
  owner, the per-file run-id rule, the binding table, index ∪ inbox reads.
* **Multi-user** (root only — skipped otherwise; CI's ``python-multiuser`` job
  runs them): real accounts writing the same venv through subprocesses demoted
  with ``setuid`` — an operator's run lands in the inbox, the owner's sweep
  indexes it, and the attacks in the design's threat model fail.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from pathlib import Path

import pytest

from runspec import inbox, logs, runsdb

pytestmark = [
    pytest.mark.inbox,
    pytest.mark.skipif(not inbox.supported(), reason="the inbox is POSIX-only"),
]
root_only = pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="multi-user tests need root")

PKG_ROOT = Path(__file__).resolve().parent.parent  # packages/python/runspec


def _rid() -> str:
    return str(uuid.uuid4())


def _rec(run_id: str, event: str | None = None, ts: str = "2026-09-01T10:00:00+00:00", **extra) -> str:
    e = {"run_id": run_id, **extra}
    if event:
        e["event"] = event
    return json.dumps({"ts": ts, "level": "INFO", "logger": "runspec.runsummary", "message": "m", "extra": e})


# ── the parser ───────────────────────────────────────────────────────────────


class TestParser:
    def test_file_run_id_strips_a_clash_suffix(self):
        rid = _rid()
        assert inbox.file_run_id(f"demo.20260901T100000Z.{rid}.log") == rid
        assert inbox.file_run_id(f"demo.20260901T100000Z.{rid}-0a1b2c3d.log") == rid
        assert inbox.file_run_id("demo.log") is None
        assert inbox.file_run_id("demo.archive.20260901.log") is None

    def test_start_and_summary_make_a_finished_row(self):
        rid = _rid()
        row = inbox.row_from_records(
            rid,
            [
                json.loads(_rec(rid, "run_start", runnable="demo", command_path=["go"], pid=7, host="h", user="alice", start_ts="2026-09-01T09:59:59+00:00", invocation_args={"x": "1"})),
                json.loads(_rec(rid, message="hi")),
                json.loads(_rec(rid, "run_summary", runnable="demo", exit_code=0, duration_ms=1000, events={"INFO": 1})),
            ],
        )
        assert row is not None
        assert row["finished"] and row["status"] == "completed" and row["exit_code"] == 0
        assert row["runnable"] == "demo" and row["command"] == "go" and row["user"] == "alice"
        assert row["start_ts"] == "2026-09-01T09:59:59+00:00" and row["args"] == {"x": "1"}

    def test_no_finish_record_is_running(self):
        rid = _rid()
        row = inbox.row_from_records(rid, [json.loads(_rec(rid, "run_start", runnable="demo"))])
        assert row is not None and not row["finished"] and row["status"] == "running"

    def test_run_end_is_a_clean_finish_without_a_summary(self):
        rid = _rid()
        row = inbox.row_from_records(rid, [json.loads(_rec(rid, "run_start", runnable="demo")), json.loads(_rec(rid, "run_end", exit_code=0, duration_ms=5))])
        assert row is not None and row["finished"] and row["status"] == "completed"

    def test_exec_result_is_the_observed_outcome(self):
        rid = _rid()
        row = inbox.row_from_records(
            rid,
            [
                json.loads(_rec(rid, "run_start", runnable="demo", label="sched:x")),
                json.loads(_rec(rid, "run_start", command_path=["sub"], invocation_args={"a": "b"}, label="ignored")),  # the child's, fill-only
                json.loads(_rec(rid, "run_summary", exit_code=0, duration_ms=3)),
                json.loads(_rec(rid, "exec_result", status="failed", exit_code=137, duration_ms=9)),
            ],
        )
        assert row is not None
        assert row["status"] == "failed" and row["exit_code"] == 137  # exec wins
        assert row["label"] == "sched:x" and row["command"] == "sub" and row["args"] == {"a": "b"}
        assert row["exec"]["exit_code"] == 137

    def test_history_db_false_marker(self):
        rid = _rid()
        row = inbox.row_from_records(rid, [json.loads(_rec(rid, "run_start", history_db=False)), json.loads(_rec(rid, "run_summary", exit_code=0))])
        assert row is not None and row["history_db"] is False

    def test_a_per_run_file_yields_only_its_own_run(self):
        mine, theirs = _rid(), _rid()
        rows = inbox.rows_from_lines(
            f"demo.20260901T100000Z.{mine}.log",
            [_rec(mine, "run_start", runnable="demo"), _rec(theirs, "run_summary", runnable="forged", exit_code=0)],
        )
        assert [r["run_id"] for r in rows] == [mine]

    def test_an_archive_yields_every_run(self):
        a, b = _rid(), _rid()
        rows = inbox.rows_from_lines("demo.archive.20260901.log", [_rec(a, "run_summary", exit_code=0), _rec(b, "run_summary", exit_code=1)])
        assert {r["run_id"]: r["status"] for r in rows} == {a: "completed", b: "failed"}


# ── layout + provisioning (as the owner) ─────────────────────────────────────


@pytest.fixture
def venv(tmp_path, monkeypatch):
    root = tmp_path / "venv"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(root))
    return root


class TestProvisioning:
    def test_owner_creates_a_sticky_world_writable_inbox(self, venv):
        logs_dir = venv / "logs"
        assert inbox.state(logs_dir) == "missing"
        assert inbox.ensure_inbox(logs_dir) == "created"
        st = os.lstat(logs_dir / "inbox")
        assert stat.S_IMODE(st.st_mode) == 0o1777 and st.st_uid == os.geteuid()
        assert inbox.state(logs_dir) == "ok"
        assert inbox.ensure_inbox(logs_dir) == "ok"  # idempotent

    def test_a_wrong_mode_is_repaired(self, venv):
        logs_dir = venv / "logs"
        (logs_dir / "inbox").mkdir(mode=0o755)
        assert inbox.state(logs_dir) == "bad-mode"
        assert inbox.ensure_inbox(logs_dir) == "repaired"
        assert stat.S_IMODE(os.lstat(logs_dir / "inbox").st_mode) == 0o1777

    def test_a_symlink_at_the_path_is_never_touched(self, venv, tmp_path):
        target = tmp_path / "elsewhere"
        target.mkdir(mode=0o700)
        (venv / "logs" / "inbox").symlink_to(target)
        assert inbox.ensure_inbox(venv / "logs") == "symlink"
        assert stat.S_IMODE(target.stat().st_mode) == 0o700

    def test_a_file_at_the_path_is_never_touched(self, venv):
        (venv / "logs" / "inbox").write_text("x")
        assert inbox.ensure_inbox(venv / "logs") == "not-dir"

    def test_a_foreign_owned_dir_is_reported(self, venv):
        (venv / "logs" / "inbox").mkdir()
        os.chmod(venv / "logs" / "inbox", 0o1777)
        assert inbox.state(venv / "logs", owner=os.geteuid() + 1) == "foreign-owner"

    def test_the_no_inbox_marker_opts_out(self, venv):
        inbox.disable_inbox(venv / "logs")
        assert inbox.ensure_inbox(venv / "logs") == "disabled"
        assert not (venv / "logs" / "inbox").exists()
        assert inbox.ensure_inbox(venv / "logs", force=True) == "created"  # logs share --inbox
        assert not (venv / "logs" / ".no-inbox").exists()

    def test_someone_else_never_provisions(self, venv, monkeypatch):
        monkeypatch.setattr(os, "geteuid", lambda: 4242 if os.getuid() != 4242 else 4243)
        assert inbox.ensure_inbox(venv / "logs") == "missing"
        assert not (venv / "logs" / "inbox").exists()

    def test_owners_run_auto_creates_it_quietly(self, venv, capsys, monkeypatch):
        import logging

        import runspec.logging_setup as ls

        monkeypatch.setattr(ls.atexit, "register", lambda fn, *a, **k: fn)
        try:
            ls.configure_logging({"rotate": "midnight", "keep": 7, "store": "per-run"}, runnable_name="demo", history_cfg={"db": False})
            logging.getLogger("demo").info("hello")
        finally:
            _reset_logging(ls)
        assert inbox.state(venv / "logs") == "ok"
        captured = capsys.readouterr()
        assert "inbox" not in captured.out and "inbox" not in captured.err  # audit log only
        [f] = [p for p in (venv / "logs").glob("demo.*.log")]
        events = [json.loads(ln)["extra"].get("event") for ln in f.read_text().splitlines()]
        assert "inbox_created" in events


def _reset_logging(ls) -> None:  # type: ignore[no-untyped-def]
    import logging

    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(getattr(h, "formatter", None), (ls._JsonFormatter, ls._ConsoleFormatter)):
            root.removeHandler(h)
            h.close()
        elif isinstance(h, ls._RunSummaryCounter):
            root.removeHandler(h)
    for f in list(root.filters):
        if isinstance(f, ls._SensitiveFilter):
            root.removeFilter(f)
    if isinstance(sys.stdout, ls._StdoutTee):
        sys.stdout = sys.stdout._original
    ls._configured = False
    ls._summary_state = None
    ls._debug = False


# ── readers (the owner's view of its own inbox) ──────────────────────────────


class TestReaders:
    def test_index_wins_over_the_inbox_and_inbox_rows_are_pending(self, venv, monkeypatch):
        logs_dir = venv / "logs"
        inbox.ensure_inbox(logs_dir)
        done, pending = _rid(), _rid()
        runsdb.begin_run(logs_dir, {"run_id": done, "runnable": "demo", "status": "completed", "start_ts": "2026-09-01T10:00:00+00:00"})
        for rid in (done, pending):
            (logs_dir / "inbox" / f"demo.20260901T100000Z.{rid}.log").write_text(
                "\n".join([_rec(rid, "run_start", runnable="demo", start_ts="2026-09-01T10:00:00+00:00"), _rec(rid, "run_summary", exit_code=3)]) + "\n"
            )
            os.chmod(logs_dir / "inbox" / f"demo.20260901T100000Z.{rid}.log", 0o644)
        rows = runsdb.list_runs_with_inbox(logs_dir, limit=10)
        by = {r["run_id"]: r for r in rows}
        assert by[done]["status"] == "completed" and not by[done].get("pending")
        assert by[pending]["status"] == "failed" and by[pending]["pending"] and by[pending]["source"] == "inbox"

    def test_resolve_log_file_finds_a_pending_file(self, venv):
        logs_dir = venv / "logs"
        inbox.ensure_inbox(logs_dir)
        name = f"demo.20260901T100000Z.{_rid()}.log"
        (logs_dir / "inbox" / name).write_text("{}\n")
        assert logs.resolve_log_file(logs_dir, name) == logs_dir / "inbox" / name
        (logs_dir / "inbox" / name).rename(logs_dir / name)
        assert logs.resolve_log_file(logs_dir, name) == logs_dir / name
        assert logs.resolve_log_file(logs_dir, "../etc/passwd") is None

    def test_resolve_log_file_refuses_a_symlink_in_the_inbox(self, venv, tmp_path):
        logs_dir = venv / "logs"
        inbox.ensure_inbox(logs_dir)
        secret = tmp_path / "secret"
        secret.write_text("x")
        name = f"demo.20260901T100000Z.{_rid()}.log"
        (logs_dir / "inbox" / name).symlink_to(secret)
        assert logs.resolve_log_file(logs_dir, name) is None

    def test_prune_and_compact_never_touch_the_inbox(self, venv):
        logs_dir = venv / "logs"
        inbox.ensure_inbox(logs_dir)
        f = logs_dir / "inbox" / f"demo.20260101T100000Z.{_rid()}.log"
        f.write_text(_rec("x", "run_summary") + "\n")
        old = time.time() - 90 * 86400
        os.utime(f, (old, old))
        logs.prune(None, dirs=[logs_dir], older_than=logs.parse_duration("1d"), out=io.StringIO())
        logs.compact(None, dirs=[logs_dir], older_than=logs.parse_duration("1d"), out=io.StringIO())
        assert f.exists()

    def test_the_index_never_lives_in_the_inbox(self, venv):
        logs_dir = venv / "logs"
        inbox.ensure_inbox(logs_dir)
        runsdb.begin_run(logs_dir / "inbox", {"run_id": "r", "runnable": "d", "status": "running", "start_ts": runsdb._now_iso()})
        assert not (logs_dir / "inbox" / runsdb.DB_NAME).exists()


class TestBinding:
    def _con(self, logs_dir: Path):
        runsdb.begin_run(logs_dir, {"run_id": "seed", "runnable": "d", "status": "completed", "start_ts": runsdb._now_iso()})
        con = runsdb._connect(runsdb.db_path(logs_dir))
        assert con is not None
        return con

    def test_the_table(self, tmp_path):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        con = self._con(logs_dir)
        try:
            row = {"run_id": "r1", "runnable": "d", "status": "completed", "exit_code": 0, "start_ts": runsdb._now_iso(), "finished": True}
            assert runsdb.inbox_binding(con, "r1", os_user="alice", uid=1001) == "insert"
            runsdb.apply_inbox_row(con, row, os_user="alice", uid=1001)
            assert runsdb.inbox_binding(con, "r1", os_user="alice", uid=1001) == "update"
            assert runsdb.inbox_binding(con, "r1", os_user="bob", uid=1002) == "collision"  # never another account's row
            # A direct row (the owner's) run as root: root's file only fills gaps.
            runsdb._upsert(con, runsdb._normalise_row({"run_id": "r2", "runnable": "d", "status": "completed", "exit_code": 0, "start_ts": "t", "run_as": "0", "source": "direct"}))
            assert runsdb.inbox_binding(con, "r2", os_user="root", uid=0) == "fill"
            runsdb.apply_inbox_row(con, {**row, "run_id": "r2", "status": "failed", "exit_code": 9, "command": "sub"}, os_user="root", uid=0)
            got = dict(con.execute("SELECT status, exit_code, command FROM runs WHERE run_id='r2'").fetchone())
            assert got == {"status": "completed", "exit_code": 0, "command": "sub"}
            # A direct child of an inbox wrapper: the wrapper's exec_result is the outcome.
            runsdb._upsert(con, runsdb._normalise_row({"run_id": "r3", "runnable": "d", "status": "completed", "exit_code": 0, "start_ts": "t", "exec_uid": 1001, "source": "direct"}))
            exec_row = {**row, "run_id": "r3", "exec": {"status": "failed", "exit_code": 124, "duration_ms": 5, "end_ts": "e"}, "label": "sched:x"}
            assert runsdb.inbox_binding(con, "r3", os_user="alice", uid=1001, row=exec_row) == "outcome"
            runsdb.apply_inbox_row(con, exec_row, os_user="alice", uid=1001)
            got = dict(con.execute("SELECT status, exit_code, label FROM runs WHERE run_id='r3'").fetchone())
            assert got == {"status": "failed", "exit_code": 124, "label": "sched:x"}
            assert runsdb.inbox_binding(con, "r3", os_user="bob", uid=1002, row=exec_row) == "collision"
        finally:
            con.close()

    def test_v6_columns_and_direct_attribution(self, venv, monkeypatch):
        import logging

        import runspec.logging_setup as ls

        monkeypatch.setattr(ls.atexit, "register", lambda fn, *a, **k: fn)
        try:
            ls.configure_logging({"rotate": "midnight", "keep": 7, "store": "per-run"}, runnable_name="demo", history_cfg={})
            logging.getLogger("demo").info("hi")
            ls._emit_run_summary()
        finally:
            _reset_logging(ls)
        [row] = runsdb.list_runs(venv / "logs", live=False)
        assert row["source"] == "direct" and row["os_user"] == inbox.effective_user()
        assert runsdb.stats(venv / "logs")["schema_version"] == 6


# ── multi-user (root only) ───────────────────────────────────────────────────

OWNER, ALICE, BOB = 4201, 4202, 4203


def _as(uid: int):  # type: ignore[no-untyped-def]
    def demote() -> None:
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)

    return demote


def _python_for(uid: int) -> str | None:
    for exe in (sys.executable, shutil.which("python3") or ""):
        try:
            ok = subprocess.run([exe, "-c", "import runspec.inbox"], preexec_fn=_as(uid), env=_env(), capture_output=True).returncode == 0
        except OSError:
            ok = False
        if exe and ok:
            return exe
    return None


def _env(**extra: str) -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(PKG_ROOT), "HOME": "/nonexistent", "USER": "u", **extra}


@pytest.fixture
def shared(monkeypatch):
    """A venv owned by OWNER in a world-traversable temp dir; operators can't
    write its logs/."""
    base = Path(tempfile.mkdtemp(prefix="rs-inbox-"))
    os.chmod(base, 0o755)
    root = base / "venv"
    (root / "logs").mkdir(parents=True)
    for p in (root, root / "logs"):
        os.chown(p, OWNER, OWNER)
        os.chmod(p, 0o755)
    exe = _python_for(ALICE)
    if exe is None:
        shutil.rmtree(base, ignore_errors=True)
        why = "no interpreter an unprivileged account can run with runspec importable"
        if os.environ.get("RUNSPEC_MULTIUSER_REQUIRED"):
            pytest.fail(why)  # the CI multi-user job must never pass by skipping
        pytest.skip(why)
    monkeypatch.setattr(sys, "prefix", str(root))
    yield root, exe
    shutil.rmtree(base, ignore_errors=True)


def _run_as(uid: int, exe: str, venv: Path, body: str, **env: str) -> subprocess.CompletedProcess[str]:
    code = f"import sys; sys.prefix = {str(venv)!r}\n" + textwrap.dedent(body)
    return subprocess.run([exe, "-c", code], preexec_fn=_as(uid) if uid else None, env=_env(**env), capture_output=True, text=True, timeout=60)


_A_RUN = """
import logging
from runspec.logging_setup import configure_logging
configure_logging({"rotate": "midnight", "keep": 7}, runnable_name="demo", history_cfg={})
logging.getLogger("demo").info("hello from %s", __import__("os").geteuid())
"""

_SWEEP = """
import json
from pathlib import Path
from runspec import inbox
print(json.dumps(inbox.sweep_inbox(Path(sys.prefix) / "logs", {})))
"""


def _sweep(exe: str, venv: Path) -> dict:
    res = _run_as(OWNER, exe, venv, _SWEEP)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


@root_only
class TestMultiUser:
    def test_the_owner_provisions_then_an_operators_run_lands_in_the_inbox(self, shared):
        venv, exe = shared
        assert _run_as(OWNER, exe, venv, _A_RUN).returncode == 0
        box = venv / "logs" / "inbox"
        st = os.lstat(box)
        assert stat.S_IMODE(st.st_mode) == 0o1777 and st.st_uid == OWNER
        res = _run_as(ALICE, exe, venv, _A_RUN)
        assert res.returncode == 0, res.stderr
        assert "inbox" not in res.stdout + res.stderr
        [f] = list(box.iterdir())
        fst = os.lstat(f)
        assert fst.st_uid == ALICE and stat.S_IMODE(fst.st_mode) == 0o644
        events = [json.loads(ln)["extra"].get("event") for ln in f.read_text().splitlines()]
        assert events[0] == "run_start" and events[-1] == "run_summary"
        assert not (Path("/nonexistent") / "logs").exists()

    def test_root_creates_the_inbox_for_the_owner_and_uses_it(self, shared):
        venv, exe = shared
        res = _run_as(0, exe, venv, _A_RUN)
        assert res.returncode == 0, res.stderr
        box = venv / "logs" / "inbox"
        assert os.lstat(box).st_uid == OWNER and stat.S_IMODE(os.lstat(box).st_mode) == 0o1777
        [f] = list(box.iterdir())
        assert os.lstat(f).st_uid == 0
        assert not runsdb.db_path(venv / "logs").exists()  # root never wrote the index

    def test_the_sweep_indexes_attributes_and_moves(self, shared):
        venv, exe = shared
        _run_as(OWNER, exe, venv, _A_RUN)
        for uid in (ALICE, BOB, 0):
            assert _run_as(uid, exe, venv, _A_RUN).returncode == 0
        res = _sweep(exe, venv)
        assert res["swept"] == 3 and not res["error"], res
        assert list((venv / "logs" / "inbox").iterdir()) == []
        rows = runsdb.list_runs(venv / "logs", live=False)
        inboxed = sorted((r["os_user"], r["source"]) for r in rows if r["source"] == "inbox")
        assert inboxed == sorted([(inbox.user_name(ALICE), "inbox"), (inbox.user_name(BOB), "inbox"), (inbox.user_name(0), "inbox")])
        assert all((venv / "logs" / r["log_file"]).exists() for r in rows)
        assert _sweep(exe, venv)["swept"] == 0  # idempotent

    def test_an_operator_reads_the_swept_index_in_an_owner_only_logs_dir(self, shared):
        # logs/ is 0755 and the owner's last connection removed -wal/-shm, so a
        # plain mode=ro open can't create -shm — the reader must still see the rows
        # (the immutable fallback), not an empty history.
        venv, exe = shared
        _run_as(OWNER, exe, venv, _A_RUN)
        assert _run_as(ALICE, exe, venv, _A_RUN).returncode == 0
        assert _sweep(exe, venv)["swept"] == 1
        logs = venv / "logs"
        assert not Path(f"{runsdb.db_path(logs)}-shm").exists()
        read = """
            import json
            from pathlib import Path
            from runspec import runsdb
            d = Path(sys.prefix) / "logs"
            st = runsdb.stats(d)
            print(json.dumps([len(runsdb.list_runs(d, live=False)), st.get("rows"), st.get("error")]))
        """
        res = _run_as(BOB, exe, venv, read)
        assert res.returncode == 0, res.stderr
        assert json.loads(res.stdout.strip().splitlines()[-1]) == [2, 2, None]

    def test_only_the_owner_sweeps(self, shared):
        venv, exe = shared
        _run_as(OWNER, exe, venv, _A_RUN)
        _run_as(ALICE, exe, venv, _A_RUN)
        res = _run_as(BOB, exe, venv, _SWEEP)
        assert "only the venv owner" in json.loads(res.stdout.strip().splitlines()[-1])["error"]

    def test_a_live_run_is_visible_but_never_indexed(self, shared):
        venv, exe = shared
        _run_as(OWNER, exe, venv, _A_RUN)
        body = _A_RUN + "\nimport time, sys\nprint('started', flush=True)\ntime.sleep(30)\n"
        code = f"import sys; sys.prefix = {str(venv)!r}\n" + textwrap.dedent(body)
        proc = subprocess.Popen([exe, "-c", code], preexec_fn=_as(ALICE), env=_env(), stdout=subprocess.PIPE, text=True)
        try:
            assert proc.stdout is not None
            deadline = time.time() + 20
            while "started" not in proc.stdout.readline() and time.time() < deadline:
                pass
            rows = inbox.inbox_rows(venv / "logs")
            assert [r["status"] for r in rows] == ["running"]
            res = _sweep(exe, venv)
            assert res["running"] == 1 and res["swept"] == 0
            assert runsdb.list_runs(venv / "logs", live=False, running_only=True) == []
        finally:
            proc.kill()
            proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
        # Killed (no run_summary), lock free, pid gone → indexed as crashed.
        res = _sweep(exe, venv)
        assert res["crashed"] == 1, res
        [row] = [r for r in runsdb.list_runs(venv / "logs", live=False) if r["source"] == "inbox"]
        assert row["status"] == "crashed"

    def test_legacy_summary_off_still_writes_the_record(self, shared):
        venv, exe = shared
        _run_as(OWNER, exe, venv, _A_RUN)
        body = _A_RUN.replace('{"rotate": "midnight", "keep": 7}', '{"rotate": "midnight", "keep": 7, "summary": False}')
        assert _run_as(ALICE, exe, venv, body).returncode == 0
        res = _sweep(exe, venv)
        assert res["swept"] == 1 and res["crashed"] == 0

    def test_attacks_fail(self, shared):
        venv, exe = shared
        _run_as(OWNER, exe, venv, _A_RUN)
        _run_as(ALICE, exe, venv, _A_RUN)
        _sweep(exe, venv)
        [alice_row] = [r for r in runsdb.list_runs(venv / "logs", live=False) if r["source"] == "inbox"]
        box = venv / "logs" / "inbox"
        secret = venv.parent / "secret"
        secret.write_text("TOP SECRET\n")
        os.chmod(secret, 0o600)  # root-only
        forged = _rid()
        attack = f"""
import os, json
box = {str(box)!r}
# 1. a symlink to a file bob can't read, named like a run
os.symlink({str(secret)!r}, box + "/x.20260901T100000Z.{_rid()}.log")
# 2. a FIFO named like a run (would hang a blocking open)
os.mkfifo(box + "/x.20260901T100000Z.{_rid()}.log")
# 3. a forged run id: alice's run, claimed by bob's file
with open(box + "/demo.20260901T100000Z.{alice_row["run_id"]}.log", "w") as f:
    f.write(json.dumps({{"ts": "2026-09-01T10:00:00+00:00", "extra": {{"run_id": "{alice_row["run_id"]}", "event": "run_summary", "exit_code": 99}}}}) + "\\n")
os.chmod(box + "/demo.20260901T100000Z.{alice_row["run_id"]}.log", 0o644)
# 4. a future-dated run (to evict real history through the row cap)
with open(box + "/demo.20990101T000000Z.{forged}.log", "w") as f:
    f.write(json.dumps({{"ts": "2099-01-01T00:00:00+00:00", "extra": {{"run_id": "{forged}", "event": "run_start", "runnable": "demo", "start_ts": "2099-01-01T00:00:00+00:00"}}}}) + "\\n")
    f.write(json.dumps({{"ts": "2099-01-01T00:00:01+00:00", "extra": {{"run_id": "{forged}", "event": "run_summary", "exit_code": 0}}}}) + "\\n")
os.chmod(box + "/demo.20990101T000000Z.{forged}.log", 0o644)
"""
        assert _run_as(BOB, exe, venv, attack).returncode == 0
        # 5. a hard link to the owner-unreadable secret (root makes it — protected_hardlinks would stop bob)
        os.link(secret, box / f"y.20260901T100000Z.{_rid()}.log")
        start = time.time()
        res = _sweep(exe, venv)
        assert time.time() - start < 20  # the FIFO didn't hang it
        assert res["collisions"] == 1  # bob can't rewrite alice's row
        again = runsdb.get_run(venv / "logs", alice_row["run_id"])
        assert again is not None and again["exit_code"] == alice_row["exit_code"] and again["os_user"] == inbox.user_name(ALICE)
        future = runsdb.get_run(venv / "logs", forged)
        assert future is not None and future["start_ts"] < "2099" and future["os_user"] == inbox.user_name(BOB)  # clamped, attributed
        for p in (venv / "logs").iterdir():
            if p.is_file() and not p.is_symlink() and p.suffix == ".log":
                assert "TOP SECRET" not in p.read_text(errors="replace")
        assert not any(p.is_symlink() for p in (venv / "logs").iterdir())

    def test_logs_share_inbox_as_root_drops_to_the_owner(self, shared):
        venv, exe = shared
        res = _run_as(0, exe, venv, "from runspec import cli\ncli.cmd_logs(['share', '--inbox'])\n")
        assert res.returncode == 0, res.stdout + res.stderr
        assert "logs inbox created" in res.stdout
        assert os.lstat(venv / "logs" / "inbox").st_uid == OWNER

    def test_an_operator_cant_set_up_the_inbox(self, shared):
        venv, exe = shared
        res = _run_as(ALICE, exe, venv, "from runspec import cli\ncli.cmd_logs(['share', '--inbox'])\n")
        assert res.returncode == 1 and "only the venv owner" in res.stdout


_EXEC = """
import sys
from runspec import runexec
spec = {{"runnable": "demo", "command": [sys.executable, {child!r}], "config_path": "", "run_as": "", "become_method": "sudo", "become_flags": None, "config": {{}}}}
runexec.find_runnable = lambda name: spec if name == "demo" else None
sys.exit(runexec.exec_runnable("demo", [], label="sched:x", skip_if_running={skip!r}))
"""

_CHILD = """
import logging, sys
sys.prefix = {venv!r}
from runspec.logging_setup import adopt_exec_run_id, configure_logging
rid = adopt_exec_run_id()
configure_logging({{"rotate": "midnight", "keep": 7}}, runnable_name="demo", history_cfg={{}}, run_id=rid)
logging.getLogger("demo").info("child says hi")
print("child stdout")
sys.exit(3)
"""


def _exec_as(uid: int, exe: str, venv: Path, *, skip: bool = False) -> subprocess.CompletedProcess[str]:
    child = venv.parent / "child.py"
    child.write_text(_CHILD.format(venv=str(venv)))
    os.chmod(child, 0o644)
    return _run_as(uid, exe, venv, _EXEC.format(child=str(child), skip=skip))


@root_only
class TestExecInInbox:
    def test_one_file_one_row_with_the_observed_outcome(self, shared):
        venv, exe = shared
        _run_as(OWNER, exe, venv, _A_RUN)
        res = _exec_as(ALICE, exe, venv)
        assert res.returncode == 3, res.stderr
        [f] = list((venv / "logs" / "inbox").iterdir())  # the child joined the wrapper's file
        events = [json.loads(ln)["extra"].get("event") for ln in f.read_text().splitlines()]
        assert events[0] == "run_start" and "run_summary" in events and events[-1] == "exec_result"
        assert "exec_output" in events
        assert _sweep(exe, venv)["swept"] == 1
        [row] = [r for r in runsdb.list_runs(venv / "logs", live=False) if r["source"] == "inbox"]
        assert row["label"] == "sched:x" and row["exit_code"] == 3 and row["status"] == "failed"
        assert row["os_user"] == inbox.user_name(ALICE)

    def test_a_forged_running_file_cant_hold_a_schedule_off(self, shared):
        venv, exe = shared
        _run_as(OWNER, exe, venv, _A_RUN)
        rid = _rid()
        forge = f"""
import json, os, fcntl, time
p = {str(venv / "logs" / "inbox")!r} + "/demo.20260901T100000Z.{rid}.log"
fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
os.write(fd, (json.dumps({{"ts": "2026-09-01T10:00:00+00:00", "extra": {{"run_id": "{rid}", "event": "run_start", "runnable": "demo", "label": "sched:x"}}}}) + "\\n").encode())
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
time.sleep(30)
"""
        code = f"import sys; sys.prefix = {str(venv)!r}\n" + textwrap.dedent(forge)
        proc = subprocess.Popen([exe, "-c", code], preexec_fn=_as(BOB), env=_env(), stdout=subprocess.PIPE, text=True)
        try:
            assert proc.stdout is not None and "held" in proc.stdout.readline()
            assert [r["status"] for r in inbox.inbox_rows(venv / "logs")] == ["running"]
            res = _exec_as(ALICE, exe, venv, skip=True)
            assert res.returncode == 3, res.stderr  # ran — bob's "running" file isn't trusted
        finally:
            proc.kill()
            proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()


class TestCliDisplay:
    def test_history_line_shows_the_account_and_the_invoker(self, capsys):
        from runspec import cli

        base = {"start_ts": "2026-09-29T20:35:49", "status": "completed", "runnable": "demo", "duration_ms": 5, "exit_code": 0, "host": "web-01"}
        cli._print_history_table(
            [
                {**base, "user": "root", "os_user": "alice"},  # sudo -u alice
                {**base, "user": "bob", "os_user": "bob"},
                {**base, "user": "carol"},  # pre-v6 row: no os_user
                {**base, "user": "unknown", "os_user": "root"},
            ]
        )
        out = capsys.readouterr().out.splitlines()
        assert out[0].endswith("[alice@web-01 via root]")
        assert out[1].endswith("[bob@web-01]")
        assert out[2].endswith("[carol@web-01]")
        assert out[3].endswith("[root@web-01]")

    def test_stats_calls_read_only_expected_in_an_inbox_venv(self, capsys):
        from runspec import cli

        snap = {"path": "/v/logs/runspec_runs.db", "exists": True, "rows": 1, "by_status": {"completed": 1}, "write_error": "permission denied"}
        cli._print_history_stats([{**snap, "inbox": {"state": "ok", "files": 0}}])
        out = capsys.readouterr().out
        assert "read-only for you" in out and "✗" not in out
        cli._print_history_stats([{**snap, "inbox": {"state": "missing"}}])
        out = capsys.readouterr().out
        assert "✗  not writable by you" in out and "no logs inbox" in out

    def test_stats_reports_the_inbox_before_any_index_exists(self, capsys):
        from runspec import cli

        cli._print_history_stats([{"path": "/v/logs/runspec_runs.db", "exists": False, "inbox": {"state": "ok", "files": 2, "finished": 2, "bytes": 2048}}])
        out = capsys.readouterr().out
        assert "(no index yet)" in out and "inbox: 2 pending file(s), 2 finished awaiting sweep" in out
        cli._print_history_stats([{"path": "/v/logs/runspec_runs.db", "exists": False, "inbox": {"state": "no-logs"}}])
        assert "runspec logs share --inbox" in capsys.readouterr().out
