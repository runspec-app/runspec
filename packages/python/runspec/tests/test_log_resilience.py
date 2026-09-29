"""Shared-venv logging resilience (core 0.65.0 — docs/design/venv-logs-inbox.md §2).

The fixes that stand on their own before the inbox: a race-free write probe, a
fallback that never fails the run, the rotating single file only for the account
that owns the logs dir, root handing what it creates to the venv owner, the runs
index refusing a world-writable/sticky dir, hidepid-aware liveness, and
``compact`` never deleting a file it couldn't read.
"""

from __future__ import annotations

import io
import json
import logging
import os
import stat
import sys
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

import runspec.logging_setup as ls
from runspec import logs, runsdb
from runspec.logging_setup import _ConsoleFormatter, _JsonFormatter, _RunSummaryCounter, _SensitiveFilter, configure_logging

posix_only = pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership semantics")
root_only = pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root")


@pytest.fixture
def venv(monkeypatch, tmp_path):
    root = tmp_path / "venv"
    root.mkdir()
    monkeypatch.setattr(sys, "prefix", str(root))
    return root


@pytest.fixture
def reset_logging(monkeypatch):
    monkeypatch.setattr(ls.atexit, "register", lambda fn, *a, **k: fn)
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(getattr(h, "formatter", None), (_JsonFormatter, _ConsoleFormatter)):
            root.removeHandler(h)
            h.close()
        elif isinstance(h, _RunSummaryCounter):
            root.removeHandler(h)
    for f in list(root.filters):
        if isinstance(f, _SensitiveFilter):
            root.removeFilter(f)
    ls._configured = False
    ls._summary_state = None
    ls._debug = False


# ── the resolver ─────────────────────────────────────────────────────────────


def test_concurrent_resolves_never_fall_back(venv):
    results: list[Path] = []
    barrier = threading.Barrier(16)

    def go() -> None:
        barrier.wait()
        results.append(ls._resolve_log_dir())

    threads = [threading.Thread(target=go) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(results) == {venv / "logs"}
    assert not [p for p in (venv / "logs").iterdir() if p.name.startswith(".wtest")]  # probes cleaned up


def test_a_leftover_probe_file_is_harmless(venv):
    # The old fixed `.wtest` name: one left behind (e.g. by another account) used
    # to make touch() fail and push every run to ~/logs.
    (venv / "logs").mkdir()
    (venv / "logs" / ".wtest").mkdir()  # can't be touched/unlinked as a file
    assert ls._resolve_log_dir() == venv / "logs"


def test_unwritable_everywhere_uses_a_private_temp_dir(venv, monkeypatch, capsys):
    monkeypatch.setattr(ls, "_writable_dir", lambda d, **kw: False)
    d = ls._resolve_log_dir()
    assert d.is_dir() and d.name.startswith("runspec-logs-")
    assert "neither" in capsys.readouterr().err
    d.rmdir()


def test_run_succeeds_when_home_is_unwritable(venv, monkeypatch, reset_logging):
    monkeypatch.setattr(ls, "_writable_dir", lambda d, **kw: False)
    configure_logging({"rotate": "midnight", "keep": 7, "store": "per-run"}, runnable_name="demo", history_cfg={"db": False})
    logging.getLogger("demo").info("hello")  # no exception: the run carries on


# ── single store only for the dir's owner ────────────────────────────────────


def _file_handlers() -> list[logging.FileHandler]:
    return [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler) and isinstance(h.formatter, _JsonFormatter)]


@posix_only
def test_single_store_for_the_dir_owner(venv, reset_logging):
    configure_logging({"rotate": "midnight", "keep": 7}, runnable_name="demo", history_cfg={"db": False})
    [fh] = _file_handlers()
    assert Path(fh.baseFilename).name == "demo.log"


@posix_only
def test_another_accounts_shared_dir_logs_per_run(venv, monkeypatch, reset_logging):
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)  # we don't own logs/
    configure_logging({"rotate": "midnight", "keep": 7}, runnable_name="demo", history_cfg={"db": False})
    [fh] = _file_handlers()
    assert Path(fh.baseFilename).name.startswith("demo.") and Path(fh.baseFilename).name != "demo.log"


def test_unopenable_single_file_falls_back_to_per_run(venv, monkeypatch, reset_logging):
    def boom(*a, **k):
        raise PermissionError("another account's demo.log")

    monkeypatch.setattr(ls, "_make_file_handler", boom)
    configure_logging({"rotate": "midnight", "keep": 7}, runnable_name="demo", history_cfg={"db": False})
    [fh] = _file_handlers()
    assert Path(fh.baseFilename).name != "demo.log"


# ── the runs index refuses an unsafe dir ─────────────────────────────────────


@posix_only
@pytest.mark.parametrize("mode", [0o777, 0o1777, 0o1755])
def test_index_refuses_world_writable_or_sticky_dirs(tmp_path, mode):
    d = tmp_path / "logs"
    d.mkdir()
    os.chmod(d, mode)
    runsdb.begin_run(d, {"run_id": "r1", "runnable": "demo", "status": "running", "start_ts": runsdb._now_iso()})
    assert not runsdb.db_path(d).exists()
    assert runsdb.list_runs(d) == []
    assert "world-writable or sticky" in runsdb.write_error(d)


# ── liveness under hidepid ───────────────────────────────────────────────────


def test_a_process_hidden_by_hidepid_is_still_alive(monkeypatch):
    monkeypatch.setattr(runsdb, "_read_proc_start", lambda pid: "")  # /proc hides it
    monkeypatch.setattr(runsdb, "_pid_exists", lambda pid: True)  # kill(pid, 0) → EPERM
    assert runsdb._pid_alive(4242, "123456") is True
    monkeypatch.setattr(runsdb, "_pid_exists", lambda pid: False)  # really gone
    assert runsdb._pid_alive(4242, "123456") is False


def test_a_reused_pid_is_still_dead(monkeypatch):
    monkeypatch.setattr(runsdb, "_read_proc_start", lambda pid: "999")  # another process now
    assert runsdb._pid_alive(4242, "123456") is False


# ── root hands what it creates to the venv owner ─────────────────────────────


@root_only
def test_root_created_logs_dir_and_index_belong_to_the_owner(venv):
    nobody = 65534
    os.chown(venv, nobody, nobody)
    d = ls._resolve_log_dir()
    assert d == venv / "logs" and d.stat().st_uid == nobody
    os.chmod(d, 0o755)
    runsdb.begin_run(d, {"run_id": "r1", "runnable": "demo", "status": "running", "start_ts": runsdb._now_iso()})
    assert runsdb.db_path(d).stat().st_uid == nobody


# ── compact never deletes what it couldn't read ──────────────────────────────


def _per_run(d: Path, run_id: str, age_days: float) -> Path:
    p = d / f"demo.20260101T000000Z.{uuid.uuid4()}.log"
    p.write_text(json.dumps({"ts": "2026-01-01T00:00:00+00:00", "level": "INFO", "message": run_id, "extra": {"run_id": run_id}}) + "\n")
    old = time.time() - age_days * 86400
    os.utime(p, (old, old))
    return p


def test_compact_leaves_an_unreadable_source_in_place(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    d.mkdir()
    good, bad = _per_run(d, "good", 30), _per_run(d, "bad", 30)
    real = logs._read_lines_checked
    monkeypatch.setattr(logs, "_read_lines_checked", lambda p: None if p == bad else real(p))
    out = io.StringIO()
    n = logs.compact("demo", dirs=[d], older_than=timedelta(days=7), out=out)
    assert n == 1
    assert not good.exists() and bad.exists()
    assert "couldn't be read" in out.getvalue()
    [archive] = [p for p in d.iterdir() if ".archive." in p.name]
    assert "good" in archive.read_text() and "bad" not in archive.read_text()


def test_read_lines_checked_tells_unreadable_from_empty(tmp_path):
    empty = tmp_path / "e.log"
    empty.write_text("")
    assert logs._read_lines_checked(empty) == []
    assert logs._read_lines_checked(tmp_path / "missing.log") is None
    assert stat.S_ISREG(empty.stat().st_mode)
