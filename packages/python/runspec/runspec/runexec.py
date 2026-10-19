"""runexec.py — ``runspec exec``: run one installed runnable, recorded end-to-end.

The wrapper records what a runnable can't record about itself (design:
``docs/design/runspec-exec.md``): runs with no ``[config.logging]``, failures
before logging starts, the *observed* exit code, stdout/stderr, and overlap
skips. The run id is minted here and handed to the child in ``RUNSPEC_RUN_ID``,
so the wrapper's row and the child's own writes are one row in the runs index.

Everything that touches the runs index or the log file is best-effort, like the
logging hook: a busy or unwritable index never fails or changes the run.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from runspec import inbox, runsdb
from runspec.execute import build_command, find_runnable
from runspec.logging_setup import _get_invoker, _resolve_log_dir

DEFAULT_CAPTURE_BYTES = 64 * 1024
EXIT_TIMEOUT = 124
EXIT_NOT_EXECUTABLE = 126
EXIT_NOT_FOUND = 127
_LOGGER = "runspec.exec"


class ExecError(Exception):
    """A usage problem the CLI reports without recording a run (e.g. ``--lock`` on
    Windows)."""


# ── output capture ──────────────────────────────────────────────────────────────


class _Tail:
    """Pump one child stream to our own stream while keeping its last ``limit``
    bytes. Runs on a thread; ``total`` counts every byte seen."""

    def __init__(self, src: IO[bytes], dst: IO[bytes] | None, limit: int) -> None:
        self.src, self.dst, self.limit = src, dst, limit
        self.buf = bytearray()
        self.total = 0
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self) -> None:
        fd = self.src.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            self.total += len(chunk)
            if self.dst is not None:
                with contextlib.suppress(Exception):
                    self.dst.write(chunk)
                    self.dst.flush()
            self.buf += chunk
            if len(self.buf) > self.limit:
                del self.buf[: len(self.buf) - self.limit]
        with contextlib.suppress(Exception):
            self.src.close()

    def text(self) -> str:
        return bytes(self.buf).decode("utf-8", errors="replace")


def _binary_stream(stream: Any) -> IO[bytes] | None:
    return getattr(stream, "buffer", None)


# ── records ─────────────────────────────────────────────────────────────────────


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _record(message: str, run_id: str, extra: dict[str, Any], level: str = "INFO") -> str:
    """One audit-log line in the logging hook's JSON shape (``_JsonFormatter``)."""
    return json.dumps(
        {
            "ts": _now().isoformat(),
            "level": level,
            "logger": _LOGGER,
            "message": message,
            "extra": {**extra, "run_id": run_id},
        },
        default=str,
    )


def _log_file_for(log_dir: Path, row: dict[str, Any] | None, runnable: str, start: datetime.datetime, run_id: str) -> Path:
    """Where this run's exec records go: the run's own log file when the child set
    one and it's in our log dir (one file per run), else a new per-run file in the
    standard ``{runnable}.{utc-ts}.{run_id}.log`` grammar so ``runspec logs``
    prune/compact/view manage it like any other."""
    stored = (row or {}).get("log_file")
    name = Path(stored).name if isinstance(stored, str) else ""  # a bare filename, never a path
    if name and (log_dir / name).exists():
        return log_dir / name
    ts = start.strftime("%Y%m%dT%H%M%SZ")
    return log_dir / f"{runnable}.{ts}.{run_id}.log"


def _append(path: Path, lines: list[str]) -> None:
    with contextlib.suppress(OSError), open(path, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


class _Sink:
    """Where this run's exec records go. Direct mode appends to a path in
    ``logs/`` (only the owner writes there). Inbox mode writes through the held
    handle of the run's inbox file — **never by path**: the inbox is
    world-writable, so the name could have been replaced."""

    def __init__(self, run: inbox.InboxRun | None = None) -> None:
        self.run = run

    def write(self, path: Path, lines: list[str]) -> None:
        if self.run is not None:
            for line in lines:
                with contextlib.suppress(Exception):
                    self.run.stream.write(line + "\n")
            with contextlib.suppress(Exception):
                self.run.stream.flush()
        else:
            _append(path, lines)


# ── locking ─────────────────────────────────────────────────────────────────────


def _try_lock(path: str) -> IO[str] | None:
    """Take an exclusive non-blocking lock on ``path``; the returned handle holds
    it until closed. ``None`` when another process holds it."""
    try:
        import fcntl
    except ImportError as exc:  # Windows
        raise ExecError("--lock is POSIX-only (no fcntl on this platform)") from exc
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = open(p, "a+")  # noqa: SIM115 — held open for the child's lifetime
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


# ── the run ─────────────────────────────────────────────────────────────────────


def exec_runnable(
    name: str,
    argv: list[str],
    *,
    label: str | None = None,
    trigger: str | None = None,
    lock: str | None = None,
    skip_if_running: bool = False,
    capture_bytes: int = DEFAULT_CAPTURE_BYTES,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    stdout: Any = None,
    stderr: Any = None,
    stdin: Any = None,
    on_start: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> int:
    """Run installed runnable ``name`` with ``argv`` and record it. Returns the
    exit code to exit with (the child's; 124 on timeout; 127 when the runnable
    isn't installed; 0 when skipped). ``env`` is extra environment for the child
    (carried through ``become`` when the runnable escalates).

    For an in-process caller such as a terminal UI: ``stdin`` is the child's
    standard input when there is no env frame to feed it (``None`` inherits the
    caller's, as the CLI wants; ``subprocess.DEVNULL`` keeps a child off a
    terminal the caller owns), and ``on_start`` is called with the child process
    as soon as it starts, so the caller can stop it — the run is still recorded,
    with the exit code the child ended with."""
    out_stream = stdout if stdout is not None else sys.stdout
    err_stream = stderr if stderr is not None else sys.stderr
    label = label or None
    trigger = trigger if trigger is not None else os.environ.get("RUNSPEC_TRIGGER", "").strip()
    run_id = str(uuid.uuid4())
    start = _now()
    start_iso = start.isoformat()
    user, user_target = _get_invoker()

    spec = find_runnable(name)
    history_cfg: dict[str, Any] = ((spec or {}).get("config") or {}).get("history") or {}
    indexed = bool(history_cfg.get("db", True))

    # A non-owner of a shared venv records the run in the logs inbox (the owner's
    # sweep indexes it) instead of writing the runs index itself —
    # docs/design/venv-logs-inbox.md §9. The wrapper creates the run's file and
    # holds its lock for the child's whole life.
    venv_logs = Path(sys.prefix) / "logs"
    box: inbox.InboxRun | None = None
    with contextlib.suppress(Exception):
        owner = inbox.venv_owner()
        if owner is not None and hasattr(os, "geteuid") and os.geteuid() in (owner, 0) and venv_logs.is_dir():
            inbox.ensure_inbox(venv_logs, owner)
        if inbox.uses_inbox(venv_logs, owner):
            box = inbox.open_run(venv_logs, name, run_id, owner=owner, when=start)
    log_dir = venv_logs if box is not None else _resolve_log_dir()
    write_row = indexed and box is None
    sink = _Sink(box)

    base_row = {
        "run_id": run_id,
        "runnable": name,
        "command": "",
        "start_ts": start_iso,
        "user": user,
        "user_target": user_target,
        "agent": os.environ.get("RUNSPEC_AGENT", "").lower() in ("1", "true", "yes"),
        "trigger": trigger,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "proc_start": runsdb.proc_start_token(),
        "label": label,
        "run_as": (spec or {}).get("run_as") or None,
        "os_user": inbox.effective_user() or None,
        "source": "direct",
    }
    if box is not None:
        start_extra: dict[str, Any] = {"event": "run_start", "history_db": False}
        if indexed:
            start_extra = {
                "event": "run_start",
                **{k: v for k, v in base_row.items() if k not in ("run_id", "os_user", "source", "command")},
                "start_ts": start_iso,
                "venv": sys.prefix,
            }
        sink.write(box.path, [_record(f"exec started '{name}'", run_id, start_extra)])

    def _finish_without_child(status: str, exit_code: int, message: str, reason: str) -> int:
        """Record a run that never started a child — not installed, not launchable,
        or skipped — as one finished row + an exec_result record."""
        log_path = box.path if box is not None else _log_file_for(log_dir, None, name, start, run_id)
        if write_row:
            runsdb.begin_run(
                log_dir,
                {**base_row, "status": status, "exit_code": exit_code, "end_ts": _now().isoformat(), "duration_ms": 0, "log_file": log_path.name},
            )
        sink.write(
            log_path,
            [
                _record(
                    message,
                    run_id,
                    {"event": "exec_result", "status": status, "exit_code": exit_code, "duration_ms": 0, "label": label, "trigger": trigger, "reason": reason},
                    level="INFO" if status == "skipped" else "ERROR",
                )
            ],
        )
        with contextlib.suppress(Exception):
            err_stream.write(f"runspec exec: {message}\n")
            err_stream.flush()
        return exit_code

    if spec is not None and spec.get("run_as") and spec["run_as"] == _effective_user():
        # Already the run_as account (e.g. a crontab line in that account's own
        # crontab): run directly — `sudo -u <self>` would still need a sudo grant.
        spec = {**spec, "run_as": ""}

    if spec is None:
        return _finish_without_child("failed", EXIT_NOT_FOUND, f"runnable '{name}' is not installed in this venv", "not_installed")
    if not spec.get("command"):
        return _finish_without_child("failed", EXIT_NOT_FOUND, f"runnable '{name}' has no entry-point binary in this venv", "no_binary")

    # ── skips (before anything is spawned) ──
    lock_fh = None
    if lock:
        lock_fh = _try_lock(lock)
        if lock_fh is None:
            return _finish_without_child("skipped", 0, f"skipped '{name}' — lock {lock} is held (previous run still going)", "lock_held")
    if skip_if_running:
        # index ∪ inbox — but an inbox run only counts when it's this account's,
        # the owner's or root's: a forged "running" file by another operator must
        # not be able to hold a schedule off.
        live = [
            r
            for r in runsdb.list_runs_with_inbox(venv_logs if box is not None else log_dir, running_only=True, label=label, runnable=None if label else name, limit=20)
            if inbox.trusted_writer(r, venv_logs) and r.get("run_id") != run_id
        ]
        if live:
            if lock_fh:
                lock_fh.close()
            what = f"label {label}" if label else f"'{name}'"
            return _finish_without_child("skipped", 0, f"skipped '{name}' — a run with {what} is already running", "already_running")

    try:
        if write_row:
            runsdb.begin_run(log_dir, {**base_row, "status": "running"})

        child_env = {**(env or {}), "RUNSPEC_RUN_ID": run_id}
        if hasattr(os, "geteuid"):
            # Binds a child that runs as the owner (a direct row) to this wrapper's
            # inbox file, and vice versa (the binding table, runsdb.inbox_binding).
            child_env["RUNSPEC_EXEC_UID"] = str(os.geteuid())
        if box is not None:
            child_env["RUNSPEC_RUN_LOG"] = str(box.path)
        if trigger:
            child_env["RUNSPEC_TRIGGER"] = trigger
        cmd, extra_env, frame = build_command(spec, list(argv), child_env)
        capture = capture_bytes > 0
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                env={**os.environ, **extra_env},
                stdin=subprocess.PIPE if frame else stdin,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
            )
        except OSError as exc:
            if write_row:  # drop the running placeholder; record the launch failure
                runsdb.finish_run(log_dir, run_id, exit_code=EXIT_NOT_EXECUTABLE, duration_ms=0, status="failed", cfg=history_cfg, runnable=name)
            log_path = box.path if box is not None else _log_file_for(log_dir, None, name, start, run_id)
            result = {"event": "exec_result", "status": "failed", "exit_code": EXIT_NOT_EXECUTABLE, "duration_ms": 0, "label": label, "trigger": trigger, "reason": "launch_error"}
            sink.write(log_path, [_record(f"could not start '{name}': {exc}", run_id, result, level="ERROR")])
            with contextlib.suppress(Exception):
                err_stream.write(f"runspec exec: could not start '{name}': {exc}\n")
            return EXIT_NOT_EXECUTABLE

        if frame:
            _feed_stdin(proc, frame)
        if on_start is not None:
            with contextlib.suppress(Exception):  # a caller's bug must not orphan the run
                on_start(proc)
        tails: list[_Tail] = []
        if capture:
            assert proc.stdout is not None and proc.stderr is not None
            tails = [
                _Tail(proc.stdout, _binary_stream(out_stream), capture_bytes),
                _Tail(proc.stderr, _binary_stream(err_stream), capture_bytes),
            ]

        restore = _forward_signals(proc)
        timed_out = False
        try:
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                proc.wait()
                rc = EXIT_TIMEOUT
        finally:
            restore()
        for t in tails:
            t.thread.join(timeout=5)
        if rc < 0:  # killed by a signal
            rc = 128 + (-rc)
        duration_ms = int((time.monotonic() - t0) * 1000)
        status = "completed" if rc == 0 else "failed"

        # ── finalize: one row, observed outcome (the wrapper writes last) ──
        target_id = run_id
        row = runsdb.get_run(log_dir, run_id) if write_row else None
        if write_row and row is not None and row.get("pid") == os.getpid() and row.get("status") == "running":
            # The child never touched our row — no logging, failed before logging,
            # or an older runspec that minted its own id. Adopt the latter.
            adopted = runsdb.adopt_child_row(log_dir, run_id, runnable=name, since_ts=start_iso, label=label)
            if adopted:
                target_id = adopted
        if write_row:
            runsdb.finish_run(log_dir, target_id, exit_code=rc, duration_ms=duration_ms, status=status, cfg=history_cfg, runnable=name)
            row = runsdb.get_run(log_dir, target_id)

        # ── output + result records in the run's log file ──
        log_path = box.path if box is not None else _log_file_for(log_dir, row, name, start, target_id)
        lines = []
        for stream_name, t in zip(("stdout", "stderr"), tails, strict=False):  # no tails when capture is off
            if t.total:
                lines.append(
                    _record(
                        f"captured {stream_name} ({min(t.total, capture_bytes)} of {t.total} bytes)",
                        target_id,
                        {"event": "exec_output", "stream": stream_name, "text": t.text(), "total_bytes": t.total, "truncated": t.total > capture_bytes},
                    )
                )
        result_msg = f"{name} {status} — exit {rc} in {duration_ms / 1000:.2f}s" + (" (timed out)" if timed_out else "")
        lines.append(
            _record(
                result_msg,
                target_id,
                {"event": "exec_result", "status": status, "exit_code": rc, "duration_ms": duration_ms, "label": label, "trigger": trigger, "reason": "timeout" if timed_out else ""},
                level="INFO" if rc == 0 else "ERROR",
            )
        )
        sink.write(log_path, lines)
        if write_row and row is not None and not row.get("log_file"):
            runsdb.set_log_file(log_dir, target_id, log_path.name)
        return rc
    finally:
        if lock_fh is not None:
            lock_fh.close()
        if box is not None:
            with contextlib.suppress(Exception):
                box.stream.close()  # releases the run's lock — the sweep may take it now


def _feed_stdin(proc: subprocess.Popen[bytes], frame: bytes) -> None:
    """Write the SPEC env frame to an escalated child's stdin, then pass this
    process's own stdin through (the prelude consumes exactly the frame, so the
    runnable still sees anything piped to ``runspec exec``). The copy runs on a
    daemon thread; a closed or unreadable stdin just ends the child's input."""
    pipe = proc.stdin
    if pipe is None:
        return
    try:
        pipe.write(frame)
        pipe.flush()
    except OSError:
        with contextlib.suppress(OSError):
            pipe.close()
        return

    def pump() -> None:
        try:
            src = sys.stdin.buffer if sys.stdin is not None else None
            while src is not None:
                chunk = src.read1(65536) if hasattr(src, "read1") else src.read(65536)
                if not chunk:
                    break
                pipe.write(chunk)
                pipe.flush()
        except Exception:  # noqa: BLE001 — stdin closed/unreadable, or the child exited
            pass
        finally:
            with contextlib.suppress(OSError):
                pipe.close()

    threading.Thread(target=pump, name="runspec-exec-stdin", daemon=True).start()


def _effective_user() -> str:
    """The effective account name (POSIX), or "" when it can't be determined."""
    try:
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:  # noqa: BLE001 — Windows (no pwd / geteuid) or an unknown uid
        return ""


def _forward_signals(proc: subprocess.Popen[bytes]) -> Any:
    """Forward SIGTERM/SIGINT/SIGHUP to the child while it runs, so stopping the
    wrapper stops the run (and the wrapper still records the outcome). Returns a
    function that restores the previous handlers. No-op off the main thread."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous: dict[int, Any] = {}

    def _handler(signum: int, _frame: Any) -> None:
        with contextlib.suppress(Exception):
            proc.send_signal(signum)

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        with contextlib.suppress(Exception):
            previous[sig] = signal.signal(sig, _handler)

    def _restore() -> None:
        for sig, h in previous.items():
            with contextlib.suppress(Exception):
                signal.signal(sig, h)

    return _restore
