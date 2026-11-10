"""outputlog.py — keep the daemon's output files to one per day, seven days.

The daemon writes its own log (``logs/scheduler.log``, a ``TimedRotatingFileHandler``
at midnight, 7 kept). Two more files catch what a keeper captures:

* ``logs/scheduler.stdout.log`` — the daemon's stdout/stderr. The cron watchdog
  (``run --detach``) points the daemon's fds 1 and 2 at it; under the venv's own
  supervisord, supervisord writes it from a pipe.
* ``logs/supervisord.log`` — supervisord's own log (venv supervisord only).

After 0.10.2 the daemon no longer echoes its log lines to stderr under a keeper
(see :func:`echo_to_stderr`), so these hold only startup errors and crash
tracebacks. The daemon still rotates them at midnight the way ``scheduler.log``
rotates: the file becomes ``<name>.YYYY-MM-DD`` (the day its content is from)
and the newest :data:`KEEP_DAYS` dated copies are kept.

How the writer is told about the new file depends on who holds it:

* ``file`` mode (cron watchdog): the daemon holds it as fds 1 and 2, so it
  renames the file, opens a new one and ``dup2``s it over both.
* ``supervisor`` mode (the venv's supervisord is the daemon's parent): the
  daemon renames the files and sends supervisord ``SIGUSR2``, which makes it
  reopen its main log and every child log.

Anywhere else (a terminal, systemd's journal, root's supervisord) nothing is
rotated here — the journal has its own retention, and root's supervisord can't
be signalled by the venv owner.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import os
import re
import signal
import sys
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger("runspec_scheduler.outputlog")

STDOUT_LOG = "scheduler.stdout.log"
SUPERVISORD_LOG = "supervisord.log"
KEEP_DAYS = 7

_DATED = re.compile(r"\.(\d{4}-\d{2}-\d{2})(?:\.(\d+))?$")


def echo_to_stderr(env: Mapping[str, str] | None = None, stream: object = None) -> bool:
    """Whether the daemon should also print its log lines to stderr.

    Yes at a terminal (someone is watching ``run``) and under systemd, whose
    journal is where those lines are read. No under the cron watchdog or
    supervisord: their capture file would hold a second copy of
    ``scheduler.log``, without its rotation."""
    env_: Mapping[str, str] = os.environ if env is None else env
    if env_.get("JOURNAL_STREAM"):
        return True
    stream = sys.__stderr__ if stream is None else stream
    try:
        return bool(stream is not None and stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        return False


def _venv_supervisord_pid(venv: Path) -> int | None:
    try:
        return int((venv / "supervisor" / "supervisord.pid").read_text().strip())
    except (OSError, ValueError):
        return None


def detect_mode(logs_dir: Path, venv: Path, env: Mapping[str, str] | None = None) -> str | None:
    """``file``, ``supervisor`` or ``None`` (nothing to rotate here)."""
    env_: Mapping[str, str] = os.environ if env is None else env
    if env_.get("SUPERVISOR_ENABLED"):
        pid = _venv_supervisord_pid(venv)
        return "supervisor" if pid is not None and pid == os.getppid() else None
    try:
        st = os.fstat(1)
        target = (logs_dir / STDOUT_LOG).stat()
    except OSError:
        return None
    return "file" if (st.st_dev, st.st_ino) == (target.st_dev, target.st_ino) else None


def _dest(path: Path, day: datetime.date) -> Path:
    dest = path.with_name(f"{path.name}.{day.isoformat()}")
    n = 1
    while dest.exists():
        dest = path.with_name(f"{path.name}.{day.isoformat()}.{n}")
        n += 1
    return dest


def prune(path: Path, keep: int = KEEP_DAYS) -> list[Path]:
    """Delete dated copies of ``path`` beyond the newest ``keep`` days."""
    days: dict[str, list[Path]] = {}
    for p in path.parent.glob(f"{path.name}.*"):
        m = _DATED.search(p.name[len(path.name) :])
        if m:
            days.setdefault(m.group(1), []).append(p)
    removed = []
    for day in sorted(days, reverse=True)[keep:]:
        for p in days[day]:
            with contextlib.suppress(OSError):
                p.unlink()
                removed.append(p)
    return removed


class OutputLogs:
    """Rotates the keeper-captured output files; :meth:`check` is cheap (a stat
    per file), so the daemon calls it about once a minute."""

    def __init__(self, logs_dir: Path, mode: str | None, *, keep: int = KEEP_DAYS) -> None:
        self.logs_dir = logs_dir
        self.mode = mode
        self.keep = keep
        self._last_day: datetime.date | None = None

    @classmethod
    def for_venv(cls, venv: Path | None = None) -> OutputLogs:
        venv = Path(sys.prefix) if venv is None else venv
        logs = venv / "logs"
        return cls(logs, detect_mode(logs, venv))

    def files(self) -> list[Path]:
        if self.mode == "file":
            return [self.logs_dir / STDOUT_LOG]
        if self.mode == "supervisor":
            return [self.logs_dir / STDOUT_LOG, self.logs_dir / SUPERVISORD_LOG]
        return []

    def _day_to_close(self, path: Path, today: datetime.date) -> datetime.date | None:
        """The day ``path``'s content belongs to, when that day is over."""
        try:
            st = path.stat()
        except OSError:
            return None
        if st.st_size == 0:
            return None
        written = datetime.date.fromtimestamp(st.st_mtime)
        if written < today:
            return written
        # written today, but the day turned since we last looked: the file holds
        # yesterday's lines plus a few from after midnight — file it under yesterday
        if self._last_day is not None and self._last_day < today:
            return self._last_day
        return None

    def check(self, today: datetime.date | None = None) -> list[Path]:
        """Rotate any file whose day is over. Returns the new dated copies."""
        today = datetime.date.today() if today is None else today
        rotated: list[Path] = []
        for path in self.files():
            day = self._day_to_close(path, today)
            if day is None:
                continue
            dest = _dest(path, day)
            try:
                path.rename(dest)
            except OSError as exc:
                logger.warning("could not rotate %s: %s", path, exc)
                continue
            rotated.append(dest)
            if self.mode == "file":
                self._reopen_fds(path)
        if rotated and self.mode == "supervisor":
            with contextlib.suppress(OSError):
                os.kill(os.getppid(), signal.SIGUSR2)  # supervisord reopens its logs
        for path in self.files():
            prune(path, self.keep)
        self._last_day = today
        return rotated

    @staticmethod
    def _reopen_fds(path: Path) -> None:
        for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
            with contextlib.suppress(Exception):
                stream.flush()  # type: ignore[union-attr]
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.dup2(fd, 1)
            os.dup2(fd, 2)
        finally:
            os.close(fd)
