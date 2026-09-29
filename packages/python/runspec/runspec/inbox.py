"""The shared-venv logs inbox (docs/design/venv-logs-inbox.md).

A venv's ``logs/`` has **one writer for its contents**: the account that owns
the venv root ("the owner"). Everyone else — operators, root, cron under other
accounts — drops its per-run log file into ``logs/inbox/`` (mode ``01777``,
owned by the owner: the classic sticky drop-box) and never touches SQLite. The
owner *sweeps* finished inbox files into the runs index and moves them into
``logs/``; readers show index ∪ inbox.

This module holds everything that touches the inbox:

* layout checks + auto-provisioning (:func:`state`, :func:`ensure_inbox`);
* the writer's file (:func:`open_run`, :class:`InboxRun`, :class:`HeldFileHandler`);
* the one parser from log records to a runs-index row (:func:`row_from_records`,
  :func:`parse_run_file`) — shared by the sweep, inbox reads and ``reindex``;
* readers (:func:`inbox_rows`) and the owner-only sweep (:func:`sweep_inbox`).

Everything is POSIX-only (``supported()``); on Windows the inbox doesn't exist
and the previous behaviour applies. Nothing here ever raises into a run.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import socket
import stat
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, Any

INBOX = "inbox"
NO_INBOX_MARKER = ".no-inbox"
SWEEP_LOCK = ".sweep.lock"
# A per-run file bigger than this is never read by the sweep or a reader (a
# user could otherwise make the owner read gigabytes). Generous: a debug run of a
# chatty runnable stays well under it.
MAX_FILE_BYTES = 64 * 1024 * 1024
# How many files from one account a single sweep takes (flood control); the rest
# wait for the next sweep.
PER_USER_CAP = 500
# Junk (symlinks, FIFOs, stray names, empty dirs, a record-less file) is removed
# once it's this old — so a writer that's mid-create is never raced.
GRACE_SECONDS = 3600
# Timestamps in a swept file may not be later than the file's kernel-set ctime
# (plus this slack) — a user can't future-date runs to evict real history.
CLOCK_SLACK_S = 5
SWEEP_THROTTLE_S = 60
SWEEP_BUDGET_S = 2.0

_FINISH_EVENTS = ("run_summary", "run_end", "exec_result")
_PER_RUN_NAME = re.compile(r"\.\d{8}T\d{6}Z\.([0-9a-fA-F-]{8,})\.log$")
_UUID_SUFFIXED = re.compile(r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:-[0-9a-fA-F]{8})?$")

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_NOCTTY = getattr(os, "O_NOCTTY", 0)


def supported() -> bool:
    """The inbox needs POSIX ownership, sticky dirs, ``O_NOFOLLOW``, dir-fd
    relative opens and ``flock``. False on Windows."""
    if os.name != "posix" or not hasattr(os, "geteuid") or not _O_NOFOLLOW or not _O_DIRECTORY:
        return False
    try:
        import fcntl  # noqa: F401
    except ImportError:
        return False
    return os.open in os.supports_dir_fd


# ── identities ──────────────────────────────────────────────────────────────────


def venv_owner(root: Path | None = None) -> int | None:
    """The owner of the venv root (``sys.prefix``) — the one account that writes
    ``logs/`` and the runs index. ``None`` when it can't be read."""
    try:
        return os.stat(root if root is not None else sys.prefix).st_uid
    except OSError:
        return None


def owner_of_logs(log_dir: Path) -> int | None:
    """The owner for a logs dir: the owner of the directory that contains it (the
    venv root for ``{venv}/logs``)."""
    return venv_owner(Path(log_dir).parent)


def user_name(uid: int | None) -> str:
    """Account name for a uid (``str(uid)`` when unknown, ``""`` for ``None``)."""
    if uid is None:
        return ""
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError, OverflowError):
        return str(uid)


def uid_of(name: Any) -> int | None:
    """uid for an account name (or a numeric string); ``None`` when unknown."""
    if name is None or name == "":
        return None
    if isinstance(name, int):
        return name
    s = str(name).strip()
    if s.isdigit():
        return int(s)
    try:
        import pwd

        return pwd.getpwnam(s).pw_uid
    except (KeyError, ImportError):
        return None


def effective_user() -> str:
    return user_name(os.geteuid()) if hasattr(os, "geteuid") else ""


# ── layout ──────────────────────────────────────────────────────────────────────


def inbox_dir(log_dir: Path) -> Path:
    return Path(log_dir) / INBOX


def _dir_problem(st: os.stat_result, owner: int | None, logs_dev: int | None) -> str:
    """Why an inbox dir with stat ``st`` must not be used — ``""`` when sane."""
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    if not stat.S_ISDIR(st.st_mode):
        return "not-dir"
    if owner is not None and st.st_uid != owner:
        return "foreign-owner"
    if logs_dev is not None and st.st_dev != logs_dev:
        return "other-fs"
    if stat.S_IMODE(st.st_mode) != 0o1777:
        return "bad-mode"
    return ""


def state(log_dir: Path, owner: int | None = None) -> str:
    """The inbox's state, one ``lstat`` (never follows a symlink):

    ``ok`` · ``missing`` · ``disabled`` (``.no-inbox`` marker) · ``bad-mode`` ·
    ``symlink`` · ``not-dir`` · ``foreign-owner`` · ``other-fs`` · ``no-logs`` ·
    ``unsupported``."""
    if not supported():
        return "unsupported"
    log_dir = Path(log_dir)
    try:
        logs_st = os.stat(log_dir)
    except OSError:
        return "no-logs"
    if owner is None:
        owner = owner_of_logs(log_dir)
    if os.path.lexists(log_dir / NO_INBOX_MARKER):
        return "disabled"
    try:
        st = os.lstat(inbox_dir(log_dir))
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "missing"
    return _dir_problem(st, owner, logs_st.st_dev) or "ok"


def ensure_inbox(log_dir: Path, owner: int | None = None, *, force: bool = False) -> str:
    """Create or repair ``logs/inbox`` — the automatic "quick check and set".

    Acts only when running as the owner or root (anyone else can't write
    ``logs/`` and gets the current :func:`state` back). Returns ``created`` /
    ``repaired`` / ``ok``, or the state that stopped it (``disabled``,
    ``symlink``, ``not-dir``, ``foreign-owner``, …).

    A missing inbox is made ``0700`` first, handed to the owner (when root), and
    only then opened up to ``01777`` — so a root-owned world-writable directory
    never exists, even briefly. Everything after ``mkdir`` goes through a
    ``O_NOFOLLOW`` directory handle. A path that is a symlink, not a directory,
    or owned by another account is never touched. ``force`` removes a
    ``.no-inbox`` opt-out marker first (``runspec logs share --inbox``)."""
    if not supported():
        return "unsupported"
    log_dir = Path(log_dir)
    if owner is None:
        owner = owner_of_logs(log_dir)
    euid = os.geteuid()
    if owner is None or euid not in (owner, 0):
        return state(log_dir, owner)
    try:
        logs_st = os.stat(log_dir)
    except OSError:
        return "no-logs"
    marker = log_dir / NO_INBOX_MARKER
    if os.path.lexists(marker):
        if not force:
            return "disabled"
        with contextlib.suppress(OSError):
            os.unlink(marker)
    path = inbox_dir(log_dir)
    created = False
    try:
        os.mkdir(path, 0o700)
        created = True
    except FileExistsError:
        pass
    except OSError:
        return "missing"
    try:
        st = os.lstat(path)
    except OSError:
        return "missing"
    problem = _dir_problem(st, None, logs_st.st_dev)
    if problem in ("symlink", "not-dir", "other-fs"):
        return problem
    if not created and st.st_uid != owner:
        return "foreign-owner"
    if not created and stat.S_IMODE(st.st_mode) == 0o1777:
        return "ok"
    try:
        fd = os.open(path, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
    except OSError:
        return "missing"
    try:
        fst = os.fstat(fd)
        if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            return "symlink"  # swapped between lstat and open
        if created and euid == 0 and owner != 0:
            os.fchown(fd, owner, os.stat(log_dir.parent).st_gid)
        os.fchmod(fd, 0o1777)
    except OSError:
        return "missing" if created else "bad-mode"
    finally:
        os.close(fd)
    return "created" if created else "repaired"


def disable_inbox(log_dir: Path) -> None:
    """Write the ``logs/.no-inbox`` opt-out marker (an existing inbox is left in
    place — it just stops being used; files already in it still get swept)."""
    fd = os.open(Path(log_dir) / NO_INBOX_MARKER, os.O_WRONLY | os.O_CREAT | _O_NOFOLLOW | _O_CLOEXEC, 0o644)
    os.close(fd)


def _open_dir(log_dir: Path, owner: int | None) -> int | None:
    """An ``O_NOFOLLOW`` handle on a sane inbox, verified *after* opening (the
    handle, not the path, is what everything else uses). ``None`` otherwise."""
    if state(log_dir, owner) != "ok":
        return None
    try:
        logs_dev = os.stat(log_dir).st_dev
        fd = os.open(inbox_dir(log_dir), os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
    except OSError:
        return None
    try:
        if _dir_problem(os.fstat(fd), owner if owner is not None else owner_of_logs(log_dir), logs_dev):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd


# ── the writer ──────────────────────────────────────────────────────────────────


class HeldFileHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """A log handler over an already-open file that is **never reopened by
    path** — ``logging.FileHandler`` reopens by name after a close, and in a
    world-writable directory that name could be replaced. ``close`` only
    flushes; the file (and its lock) lives as long as the process."""

    def __init__(self, stream: IO[str], path: Path) -> None:
        super().__init__(stream)
        self.baseFilename = str(path)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.flush()
        logging.Handler.close(self)


@dataclass
class InboxRun:
    """This process's file in the inbox. ``locked`` is False when it joined a
    file another process (the ``runspec exec`` wrapper) holds the lock on."""

    path: Path
    name: str
    stream: IO[str]
    locked: bool = True
    joined: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def write_record(self, record: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            self.stream.write(json.dumps(record, default=str) + "\n")
            self.stream.flush()

    def handler(self) -> HeldFileHandler:
        return HeldFileHandler(self.stream, self.path)


def _safe_runnable(runnable: str) -> str:
    return (runnable or "run").replace("/", "_").replace("\x00", "") or "run"


def open_run(
    log_dir: Path,
    runnable: str,
    run_id: str,
    *,
    owner: int | None = None,
    join: str | None = None,
    when: datetime | None = None,
) -> InboxRun | None:
    """Create this run's file in the inbox (a non-owner writer), or ``None`` when
    there's no usable inbox (the caller then falls back as before).

    The file is created with ``O_EXCL|O_NOFOLLOW`` relative to the verified inbox
    handle, ``fchmod``'d ``0644`` (readable under any umask) and held under an
    exclusive ``flock`` for the life of the process — the lock is the liveness
    signal. On a name clash it retries with a ``-<8 hex>`` suffix.

    ``join`` is the path a ``runspec exec`` wrapper handed down in
    ``RUNSPEC_RUN_LOG``: when it names *our own* regular, single-link file in this
    inbox for this run id, we append to it (no lock — the wrapper holds it).
    Otherwise ``joined`` is False on the result (a new file) — see the caller."""
    if not supported():
        return None
    import fcntl

    if owner is None:
        owner = owner_of_logs(Path(log_dir))
    dfd = _open_dir(Path(log_dir), owner)
    if dfd is None:
        return None
    inbox = inbox_dir(Path(log_dir))
    try:
        if join:
            name = os.path.basename(join)
            if os.path.dirname(join) == str(inbox) and file_run_id(name) == run_id:
                try:
                    fd = os.open(name, os.O_WRONLY | os.O_APPEND | _O_NOFOLLOW | _O_CLOEXEC | _O_NONBLOCK, dir_fd=dfd)
                except OSError:
                    fd = -1
                if fd >= 0:
                    st = os.fstat(fd)
                    if stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == os.geteuid():
                        stream = os.fdopen(fd, "a", encoding="utf-8")
                        return InboxRun(path=inbox / name, name=name, stream=stream, locked=False, joined=True)
                    os.close(fd)
            # Not a file we can share — the caller mints a fresh run id.
            return None
        ts = (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
        base = f"{_safe_runnable(runnable)}.{ts}.{run_id}"
        for attempt in range(8):
            name = f"{base}.log" if attempt == 0 else f"{base}-{secrets.token_hex(4)}.log"
            try:
                fd = os.open(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, 0o644, dir_fd=dfd)
            except FileExistsError:
                continue
            except OSError:
                return None
            with contextlib.suppress(OSError):
                os.fchmod(fd, 0o644)
            locked = True
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                locked = False
            stream = os.fdopen(fd, "a", encoding="utf-8")
            return InboxRun(path=inbox / name, name=name, stream=stream, locked=locked)
        return None
    finally:
        os.close(dfd)


def uses_inbox(log_dir: Path, owner: int | None = None) -> bool:
    """Would a run by this process write to the inbox (a non-owner with a sane
    inbox)?"""
    if not supported():
        return False
    if owner is None:
        owner = owner_of_logs(Path(log_dir))
    return owner is not None and os.geteuid() != owner and state(Path(log_dir), owner) == "ok"


# ── the parser (one for the sweep, inbox reads and reindex) ─────────────────────


def file_run_id(name: str) -> str | None:
    """The run id a per-run file name carries (``{runnable}.{ts}.{run_id}.log``),
    with a clash suffix (``-<8 hex>`` on a uuid) stripped. ``None`` for any
    other name. A per-run file contributes records only for this run id."""
    m = _PER_RUN_NAME.search(name)
    if not m:
        return None
    seg = m.group(1)
    u = _UUID_SUFFIXED.match(seg)
    return u.group(1) if u else seg


def _parse_ts(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _extra(rec: dict[str, Any]) -> dict[str, Any]:
    extra = rec.get("extra")
    return extra if isinstance(extra, dict) else {}


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def row_from_records(run_id: str, records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A runs-index row (the dict :func:`runsdb._normalise_row` takes) from one
    run's JSON log records, in file order. ``None`` when there's nothing about
    the run to index.

    ``run_start`` records merge fill-only (a ``runspec exec`` wrapper writes one,
    then its child adds command + args); ``run_summary`` / ``run_end`` give the
    outcome; an ``exec_result`` is the **observed** outcome and wins. Extra keys:
    ``finished`` (any finish record), ``history_db`` (False when the run opted out
    of indexing) and ``exec`` (the exec_result outcome, for the binding table)."""
    start: dict[str, Any] = {}
    summary: dict[str, Any] | None = None
    summary_ts: Any = None
    end: dict[str, Any] | None = None
    end_ts: Any = None
    exec_res: dict[str, Any] | None = None
    exec_ts: Any = None
    first_ts: Any = None
    history_db = True
    seen = False
    for rec in records:
        extra = _extra(rec)
        if extra.get("run_id") != run_id:
            continue
        seen = True
        if first_ts is None:
            first_ts = rec.get("ts")
        ev = extra.get("event")
        if ev == "run_start":
            if extra.get("history_db") is False:
                history_db = False
                continue
            for k, v in extra.items():
                if _empty(start.get(k)) and not _empty(v):
                    start[k] = v
        elif ev == "run_summary":
            summary, summary_ts = extra, rec.get("ts")
        elif ev == "run_end":
            end, end_ts = extra, rec.get("ts")
        elif ev == "exec_result":
            exec_res, exec_ts = extra, rec.get("ts")
    if not seen:
        return None

    src = summary or {}
    row: dict[str, Any] = {"run_id": run_id, "history_db": history_db}
    row["runnable"] = start.get("runnable") or src.get("runnable") or ""
    cmd = start.get("command_path") or src.get("command_path") or []
    row["command"] = " ".join(cmd) if isinstance(cmd, list) else str(cmd or "")
    for k in ("user", "user_target", "agent", "trigger", "skill", "autonomy"):
        v = start.get(k)
        row[k] = v if not _empty(v) else src.get(k)
    for k in ("pid", "host", "proc_start", "run_as", "label", "exec_uid", "venv"):
        row[k] = start.get(k)
    args = start.get("invocation_args") or src.get("invocation_args") or {}
    row["args"] = args if isinstance(args, dict) else {}

    if summary is not None:
        exc = summary.get("exception")
        code = summary.get("exit_code", 1 if exc else 0)
        row.update(
            status="failed" if (exc or code) else "completed",
            exit_code=code,
            end_ts=summary_ts,
            duration_ms=summary.get("duration_ms"),
            events=summary.get("events"),
            exception=exc,
        )
    elif end is not None:
        code = end.get("exit_code", 0)
        row.update(status="failed" if code else "completed", exit_code=code, end_ts=end_ts, duration_ms=end.get("duration_ms"))
    else:
        row["status"] = "running"
    if exec_res is not None:
        outcome = {
            "status": exec_res.get("status") or ("completed" if not exec_res.get("exit_code") else "failed"),
            "exit_code": exec_res.get("exit_code"),
            "duration_ms": exec_res.get("duration_ms"),
            "end_ts": exec_ts,
        }
        row["exec"] = outcome
        row.update(outcome)
        for k in ("label", "trigger"):
            if _empty(row.get(k)) and not _empty(exec_res.get(k)):
                row[k] = exec_res.get(k)

    start_ts = start.get("start_ts")
    if not start_ts and summary is not None:
        start_ts = _start_from_summary(summary_ts, summary.get("duration_ms"))
    row["start_ts"] = start_ts or first_ts
    row["finished"] = summary is not None or end is not None or exec_res is not None
    return row


def _start_from_summary(end_ts: Any, duration_ms: Any) -> Any:
    dt = _parse_ts(end_ts)
    if dt is not None and isinstance(duration_ms, int):
        return (dt - timedelta(milliseconds=duration_ms)).isoformat()
    return end_ts


def _records(lines: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def rows_from_lines(name: str, lines: list[str]) -> list[dict[str, Any]]:
    """Every run's row in one log file. A per-run file yields only its own run
    (the name's run id — a record claiming another run is ignored); a single-mode
    file or an archive yields every run id in it."""
    recs = _records(lines)
    own = file_run_id(name)
    if own is not None:
        row = row_from_records(own, recs)
        return [row] if row else []
    ids: list[str] = []
    for rec in recs:
        extra = _extra(rec)
        rid = extra.get("run_id")
        if isinstance(rid, str) and rid and rid not in ids:
            ids.append(rid)
    rows = []
    for rid in ids:
        row = row_from_records(rid, recs)
        if row:
            rows.append(row)
    return rows


def parse_run_file(path: Path, lines: list[str] | None = None) -> dict[str, Any] | None:
    """The row for one per-run file (``None`` for any other file or when it holds
    nothing about its run). ``log_file`` is the file's basename."""
    name = Path(path).name
    rid = file_run_id(name)
    if rid is None:
        return None
    if lines is None:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        lines = [ln for ln in text.splitlines() if ln.strip()]
    row = row_from_records(rid, _records(lines))
    if row is not None:
        row["log_file"] = name
    return row


# ── reading the inbox ───────────────────────────────────────────────────────────


@dataclass
class _Entry:
    name: str
    st: os.stat_result
    lines: list[str]
    held: bool  # the writer still holds its lock → the run is live


def _read_entry(dfd: int, name: str, *, owner: int | None) -> tuple[_Entry | None, str]:
    """Open and read one inbox entry safely: regular file, one link, readable,
    not the owner's, under the size cap; ``O_NONBLOCK|O_NOFOLLOW`` so a FIFO or a
    symlink can't hang or redirect the reader; ``lstat`` and ``fstat`` must
    agree. Returns ``(entry, "")`` or ``(None, reason)``."""
    import fcntl

    try:
        lst = os.lstat(name, dir_fd=dfd)
    except OSError:
        return None, "gone"
    if not stat.S_ISREG(lst.st_mode):
        return None, "not-regular"
    if lst.st_nlink != 1:
        return None, "hardlink"
    if not lst.st_mode & stat.S_IROTH:
        return None, "unreadable"
    if owner is not None and lst.st_uid == owner:
        return None, "owner-file"
    if lst.st_size > MAX_FILE_BYTES:
        return None, "too-big"
    try:
        fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_NOCTTY | _O_CLOEXEC, dir_fd=dfd)
    except OSError:
        return None, "unreadable"
    try:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (lst.st_dev, lst.st_ino) or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            return None, "swapped"
        held = False
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            held = True
        except OSError:
            held = False
        data = b""
        while len(data) <= MAX_FILE_BYTES:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            data += chunk
        text = data[: MAX_FILE_BYTES + 1].decode("utf-8", errors="replace")
        return _Entry(name, st, [ln for ln in text.splitlines() if ln.strip()], held), ""
    finally:
        os.close(fd)


def _hostname() -> str:
    with contextlib.suppress(Exception):
        return socket.gethostname()
    return ""


def _confirmed_dead(row: dict[str, Any], entry: _Entry, max_run_hours: int, now: float) -> bool:
    """Lock free *and* the death confirmed: same host → the writer's process is
    gone (``/proc`` start token or ``kill(pid, 0)``); another host (shared
    filesystem) → older than ``max_run_hours``."""
    from . import runsdb

    if row.get("host") == _hostname() and row.get("pid"):
        return not runsdb._pid_alive(row.get("pid"), row.get("proc_start"))
    started = _parse_ts(row.get("start_ts"))
    age = now - (started.timestamp() if started else entry.st.st_ctime)
    return age > max(1, max_run_hours) * 3600


def _clamp(row: dict[str, Any], ctime: float) -> None:
    """No timestamp later than the file's kernel-set ctime (+ slack)."""
    limit = ctime + CLOCK_SLACK_S
    for k in ("start_ts", "end_ts"):
        dt = _parse_ts(row.get(k))
        if dt is not None and dt.timestamp() > limit:
            row[k] = datetime.fromtimestamp(ctime, tz=timezone.utc).isoformat()


def _entry_row(entry: _Entry, *, max_run_hours: int, now: float) -> dict[str, Any] | None:
    rid = file_run_id(entry.name)
    if rid is None:
        return None
    row = row_from_records(rid, _records(entry.lines))
    if row is None:
        return None
    row["log_file"] = entry.name
    row["source"] = "inbox"
    row["os_user"] = user_name(entry.st.st_uid)
    _clamp(row, entry.st.st_ctime)
    if not row["finished"]:
        if entry.held:
            row["status"] = "running"
        elif _confirmed_dead(row, entry, max_run_hours, now):
            row["status"] = "crashed"
    return row


def inbox_rows(log_dir: Path, *, max_run_hours: int = 24) -> list[dict[str, Any]]:
    """Pending runs in the inbox, as row dicts (``source="inbox"``, ``os_user``
    from the file's owner, ``pending=True``). Live runs read ``running``, dead
    unfinished ones ``crashed``, finished-but-not-yet-swept ones their outcome.
    Runs that opted out of indexing are left out. Read-only; ``[]`` on any
    failure or when there's no sane inbox."""
    if not supported():
        return []
    log_dir = Path(log_dir)
    owner = owner_of_logs(log_dir)
    dfd = _open_dir(log_dir, owner)
    if dfd is None:
        return []
    now = time.time()
    out: list[dict[str, Any]] = []
    try:
        for name in sorted(os.listdir(dfd)):
            if file_run_id(name) is None:
                continue
            # Readers show every sane file (the owner's own too); only the sweep
            # refuses an owner-owned file.
            entry, _ = _read_entry(dfd, name, owner=None)
            if entry is None:
                continue
            row = _entry_row(entry, max_run_hours=max_run_hours, now=now)
            if row is None or not row.get("history_db", True):
                continue
            row["pending"] = True
            out.append(row)
    except OSError:
        pass
    finally:
        os.close(dfd)
    return out


def status(log_dir: Path) -> dict[str, Any]:
    """Inbox health for ``runspec history stats``: its state, pending files
    (live / finished / other), bytes, and junk the owner can't clear (non-empty
    subdirectories need root)."""
    log_dir = Path(log_dir)
    out: dict[str, Any] = {"state": state(log_dir), "path": str(inbox_dir(log_dir)), "files": 0, "running": 0, "finished": 0, "bytes": 0, "other": 0, "subdirs": 0}
    if out["state"] != "ok":
        return out
    owner = owner_of_logs(log_dir)
    dfd = _open_dir(log_dir, owner)
    if dfd is None:
        return out
    try:
        for name in os.listdir(dfd):
            try:
                lst = os.lstat(name, dir_fd=dfd)
            except OSError:
                continue
            if stat.S_ISDIR(lst.st_mode):
                out["subdirs"] += 1
                continue
            if file_run_id(name) is None or not stat.S_ISREG(lst.st_mode):
                out["other"] += 1
                continue
            out["files"] += 1
            out["bytes"] += lst.st_size
            entry, _ = _read_entry(dfd, name, owner=None)
            if entry is None:
                out["other"] += 1
            elif entry.held:
                out["running"] += 1
            elif any(f'"{ev}"' in ln for ln in entry.lines[-50:] for ev in _FINISH_EVENTS):
                out["finished"] += 1
    except OSError:
        pass
    finally:
        os.close(dfd)
    return out


# ── the sweep (owner only) ──────────────────────────────────────────────────────


def _new_result(log_dir: Path) -> dict[str, Any]:
    return {
        "dir": str(log_dir),
        "swept": 0,
        "crashed": 0,
        "unindexed": 0,
        "running": 0,
        "collisions": 0,
        "skipped": 0,
        "removed": 0,
        "deferred": 0,
        "error": "",
    }


def _remove_junk(dfd: int, name: str, lst: os.stat_result, now: float) -> bool:
    """Remove a non-run entry (symlink, FIFO, socket, stray name, empty dir) once
    it's past the grace period. The owner of a sticky dir may remove anything in
    it; a non-empty directory needs root, so it's left (and reported)."""
    if now - lst.st_ctime < GRACE_SECONDS:
        return False
    try:
        if stat.S_ISDIR(lst.st_mode):
            os.rmdir(name, dir_fd=dfd)
        else:
            os.unlink(name, dir_fd=dfd)
        return True
    except OSError:
        return False


def sweep_inbox(
    log_dir: Path,
    cfg: dict[str, Any] | None = None,
    *,
    budget_s: float | None = None,
    throttle_s: float = 0,
    per_user_cap: int = PER_USER_CAP,
) -> dict[str, Any]:
    """Index finished (or confirmed-dead) inbox runs and move their files into
    ``logs/``. **Owner only** — anyone else gets ``error``; one sweep at a time
    (``logs/.sweep.lock``; a busy lock reports ``busy``).

    Per file: validate (see :func:`_read_entry`); a live lock → left alone, not
    indexed; unfinished + lock free + death confirmed → indexed ``crashed``; a
    run that opted out of indexing → moved, not indexed. The binding table
    (:func:`runsdb.inbox_binding`) decides whether the row may be written; a
    collision leaves the file where it is. The file is renamed into ``logs/``
    (never over an existing file, re-checked by inode afterwards) **before** the
    row is written. Timestamps are clamped to the file's ctime; at most
    ``per_user_cap`` files per account per sweep; ``budget_s`` time-boxes it.
    Never raises."""
    import fcntl

    from . import runsdb

    log_dir = Path(log_dir)
    result = _new_result(log_dir)
    if not supported():
        result["error"] = "the inbox is POSIX-only"
        return result
    owner = owner_of_logs(log_dir)
    st_now = state(log_dir, owner)
    # A disabled inbox is still drained (files written before the opt-out).
    if st_now in ("missing", "no-logs") or (st_now == "disabled" and not os.path.isdir(inbox_dir(log_dir))):
        result["state"] = st_now
        return result
    if owner is None or os.geteuid() != owner:
        result["error"] = f"only the venv owner ({user_name(owner)}) can sweep the inbox"
        return result
    cfg = cfg or {}
    max_run_hours = runsdb._int(cfg.get("max_run_hours"), 24)
    deadline = time.monotonic() + budget_s if budget_s else None

    try:
        lfd = os.open(log_dir, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
    except OSError as exc:
        result["error"] = f"can't open {log_dir}: {exc}"
        return result
    lock_fd = -1
    dfd = -1
    con = None
    try:
        try:
            lock_fd = os.open(SWEEP_LOCK, os.O_RDWR | os.O_CREAT | _O_NOFOLLOW | _O_CLOEXEC, 0o644, dir_fd=lfd)
        except OSError as exc:
            result["error"] = f"can't create the sweep lock: {exc}"
            return result
        if throttle_s:
            with contextlib.suppress(OSError):
                if time.time() - os.fstat(lock_fd).st_mtime < throttle_s:
                    result["throttled"] = True
                    return result
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            result["busy"] = True
            return result
        with contextlib.suppress(OSError):
            os.utime(lock_fd)

        try:
            dfd = os.open(INBOX, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=lfd)
            dst = os.fstat(dfd)
        except OSError:
            result["state"] = "missing"
            return result
        problem = _dir_problem(dst, owner, os.fstat(lfd).st_dev)
        if problem and problem != "bad-mode":
            result["error"] = f"refusing to sweep {inbox_dir(log_dir)}: {problem}"
            return result

        con = runsdb._connect(runsdb.db_path(log_dir))
        now = time.time()
        taken: dict[int, int] = {}
        for name in sorted(os.listdir(dfd)):
            if deadline is not None and time.monotonic() > deadline:
                result["deferred"] += 1
                continue
            try:
                lst = os.lstat(name, dir_fd=dfd)
            except OSError:
                continue
            if file_run_id(name) is None or not stat.S_ISREG(lst.st_mode):
                if _remove_junk(dfd, name, lst, now):
                    result["removed"] += 1
                else:
                    result["skipped"] += 1
                continue
            entry, why = _read_entry(dfd, name, owner=owner)
            if entry is None:
                if why in ("hardlink", "unreadable", "too-big") or why == "not-regular":
                    # Never readable to us; the writer can fix it. A file past the
                    # grace period with a hard link or no read bit is left too —
                    # the owner can't read it and mustn't index what it can't read.
                    pass
                result["skipped"] += 1
                continue
            if entry.held:
                result["running"] += 1
                continue
            uid = entry.st.st_uid
            if taken.get(uid, 0) >= per_user_cap:
                result["deferred"] += 1
                continue
            row = _entry_row(entry, max_run_hours=max_run_hours, now=now)
            if row is None:
                # A run file with nothing about its run: junk once it's old.
                if now - entry.st.st_ctime >= GRACE_SECONDS and _move(dfd, lfd, name, entry.st):
                    result["unindexed"] += 1
                else:
                    result["skipped"] += 1
                continue
            index = row.get("history_db", True)
            if not row["finished"] and row["status"] != "crashed":
                result["running"] += 1  # lock free but not confirmed dead (yet)
                continue
            os_user = row["os_user"]
            decision = "none"
            if index and con is not None:
                decision = runsdb.inbox_binding(con, row["run_id"], os_user=os_user, uid=uid, row=row)
                if decision == "collision":
                    result["collisions"] += 1
                    continue
            if not _move(dfd, lfd, name, entry.st):
                result["skipped"] += 1
                continue
            taken[uid] = taken.get(uid, 0) + 1
            if not index or con is None:
                result["unindexed"] += 1
                continue
            runsdb.apply_inbox_row(con, row, os_user=os_user, uid=uid, decision=decision)
            with contextlib.suppress(Exception):
                con.commit()
            if row["status"] == "crashed":
                result["crashed"] += 1
            else:
                result["swept"] += 1
    except Exception as exc:  # noqa: BLE001 — a sweep must never raise into a run
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if con is not None:
            with contextlib.suppress(Exception):
                con.commit()
                con.close()
        for fd in (dfd, lock_fd, lfd):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
    return result


def _move(dfd: int, lfd: int, name: str, st: os.stat_result) -> bool:
    """Rename ``inbox/name`` → ``logs/name`` — never over an existing file — and
    re-check the moved entry is the file that was validated. If it isn't (the
    writer swapped it between the check and the rename), the impostor is removed
    from ``logs/``."""
    try:
        os.lstat(name, dir_fd=lfd)
        return False  # never overwrite
    except FileNotFoundError:
        pass
    except OSError:
        return False
    try:
        os.rename(name, name, src_dir_fd=dfd, dst_dir_fd=lfd)
    except OSError:
        return False
    try:
        moved = os.lstat(name, dir_fd=lfd)
    except OSError:
        return False
    if (moved.st_dev, moved.st_ino) != (st.st_dev, st.st_ino) or not stat.S_ISREG(moved.st_mode):
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=lfd)
        return False
    return True


def maybe_sweep(log_dir: Path, cfg: dict[str, Any] | None = None) -> None:
    """The opportunistic sweep at the end of an owner's own run: only when an
    inbox exists, throttled and time-boxed. Best-effort."""
    with contextlib.suppress(Exception):
        if not supported() or not os.path.isdir(inbox_dir(Path(log_dir))) or os.path.islink(inbox_dir(Path(log_dir))):
            return
        if owner_of_logs(Path(log_dir)) != os.geteuid():
            return
        sweep_inbox(Path(log_dir), cfg, budget_s=SWEEP_BUDGET_S, throttle_s=SWEEP_THROTTLE_S)


def trusted_writer(row: dict[str, Any], log_dir: Path) -> bool:
    """Whose "running" row may block a ``runspec exec --skip-if-running``: this
    account's, the venv owner's, or root's — never another operator's (a forged
    inbox file could otherwise hold a schedule off forever). Index rows written
    directly are trusted (only the owner writes the index)."""
    if row.get("source") != "inbox":
        return True
    who = row.get("os_user")
    allowed = {effective_user(), user_name(owner_of_logs(Path(log_dir))), user_name(0)}
    return bool(who) and who in allowed
