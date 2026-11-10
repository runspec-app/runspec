"""Rotation of the keeper-captured output files (outputlog.py)."""

from __future__ import annotations

import datetime
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from runspec_scheduler import outputlog
from runspec_scheduler.outputlog import STDOUT_LOG, SUPERVISORD_LOG, OutputLogs

DAY = datetime.date(2026, 11, 10)


def _write(path: Path, text: str, day: datetime.date) -> None:
    path.write_text(text)
    ts = datetime.datetime.combine(day, datetime.time(12)).timestamp()
    os.utime(path, (ts, ts))


class _Tty:
    def __init__(self, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def test_echo_only_at_a_terminal_or_under_systemd():
    assert outputlog.echo_to_stderr({}, _Tty(True)) is True
    assert outputlog.echo_to_stderr({"JOURNAL_STREAM": "8:123"}, _Tty(False)) is True
    assert outputlog.echo_to_stderr({}, _Tty(False)) is False  # cron watchdog / supervisord


def test_file_older_than_today_is_filed_under_its_day(tmp_path, monkeypatch):
    log = tmp_path / STDOUT_LOG
    _write(log, "Traceback …\n", DAY - datetime.timedelta(days=2))
    reopened = []
    monkeypatch.setattr(OutputLogs, "_reopen_fds", staticmethod(reopened.append))
    rotated = OutputLogs(tmp_path, "file").check(DAY)
    assert [p.name for p in rotated] == [f"{STDOUT_LOG}.2026-11-08"]
    assert not log.exists() and reopened == [log]


def test_todays_file_and_empty_files_stay(tmp_path):
    _write(tmp_path / STDOUT_LOG, "x\n", DAY)
    (tmp_path / SUPERVISORD_LOG).write_text("")
    assert OutputLogs(tmp_path, "supervisor").check(DAY) == []


def test_lines_written_after_midnight_go_with_the_day_that_ended(tmp_path, monkeypatch):
    monkeypatch.setattr(OutputLogs, "_reopen_fds", staticmethod(lambda p: None))
    logs = OutputLogs(tmp_path, "file")
    logs.check(DAY)  # the daemon looked during the 10th
    _write(tmp_path / STDOUT_LOG, "x\n", DAY + datetime.timedelta(days=1))  # touched at 00:00:30
    assert [p.name for p in logs.check(DAY + datetime.timedelta(days=1))] == [f"{STDOUT_LOG}.2026-11-10"]


def test_no_mode_rotates_nothing(tmp_path):
    _write(tmp_path / STDOUT_LOG, "x\n", DAY - datetime.timedelta(days=1))
    assert OutputLogs(tmp_path, None).check(DAY) == []
    assert (tmp_path / STDOUT_LOG).exists()


def test_supervisor_mode_signals_the_parent_once(tmp_path, monkeypatch):
    for name in (STDOUT_LOG, SUPERVISORD_LOG):
        _write(tmp_path / name, "x\n", DAY - datetime.timedelta(days=1))
    sent = []
    monkeypatch.setattr(outputlog.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(outputlog.os, "getppid", lambda: 4242)
    rotated = OutputLogs(tmp_path, "supervisor").check(DAY)
    assert sorted(p.name for p in rotated) == [f"{STDOUT_LOG}.2026-11-09", f"{SUPERVISORD_LOG}.2026-11-09"]
    assert sent == [(4242, signal.SIGUSR2)]


def test_a_taken_name_gets_a_counter(tmp_path, monkeypatch):
    monkeypatch.setattr(OutputLogs, "_reopen_fds", staticmethod(lambda p: None))
    (tmp_path / f"{STDOUT_LOG}.2026-11-09").write_text("earlier\n")
    _write(tmp_path / STDOUT_LOG, "x\n", DAY - datetime.timedelta(days=1))
    assert [p.name for p in OutputLogs(tmp_path, "file").check(DAY)] == [f"{STDOUT_LOG}.2026-11-09.1"]


def test_keeps_seven_days(tmp_path):
    log = tmp_path / STDOUT_LOG
    for n in range(10):
        (tmp_path / f"{STDOUT_LOG}.{(DAY - datetime.timedelta(days=n)).isoformat()}").write_text("x")
    (tmp_path / f"{STDOUT_LOG}.2026-11-01.1").write_text("x")  # same day as one being dropped
    (tmp_path / f"{STDOUT_LOG}.1").write_text("x")  # supervisord's own size backup: not ours
    outputlog.prune(log, 7)
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == sorted([f"{STDOUT_LOG}.1"] + [f"{STDOUT_LOG}.{(DAY - datetime.timedelta(days=n)).isoformat()}" for n in range(7)])


def test_detect_supervisor_only_for_the_venvs_own(tmp_path, monkeypatch):
    (tmp_path / "supervisor").mkdir()
    (tmp_path / "supervisor" / "supervisord.pid").write_text("4242\n")
    monkeypatch.setattr(outputlog.os, "getppid", lambda: 4242)
    assert outputlog.detect_mode(tmp_path / "logs", tmp_path, {"SUPERVISOR_ENABLED": "1"}) == "supervisor"
    monkeypatch.setattr(outputlog.os, "getppid", lambda: 1)  # root's supervisord
    assert outputlog.detect_mode(tmp_path / "logs", tmp_path, {"SUPERVISOR_ENABLED": "1"}) is None


@pytest.mark.skipif(sys.platform == "win32", reason="fd redirection is POSIX here")
def test_detached_daemon_writes_to_the_new_file_after_rotation(tmp_path):
    """A real child whose fds 1/2 are the log file, as ``run --detach`` starts it."""
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / STDOUT_LOG
    script = textwrap.dedent(
        f"""
        import datetime, os, sys
        from pathlib import Path
        from runspec_scheduler.outputlog import OutputLogs, detect_mode
        logs = Path({str(logs)!r})
        print("before", flush=True)
        os.utime(logs / {STDOUT_LOG!r}, (0, 0))  # pretend it was written long ago
        mode = detect_mode(logs, Path({str(tmp_path)!r}), {{}})
        OutputLogs(logs, mode).check(datetime.date(2026, 11, 10))
        print("after", flush=True)
        print("err", file=sys.stderr, flush=True)
        """
    )
    with open(log, "ab") as out:
        subprocess.run([sys.executable, "-c", script], stdout=out, stderr=subprocess.STDOUT, check=True)
    old = [p for p in logs.iterdir() if p.name != STDOUT_LOG]
    assert len(old) == 1 and old[0].read_text() == "before\n"
    assert log.read_text() == "after\nerr\n"
