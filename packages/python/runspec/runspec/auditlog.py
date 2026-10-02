"""Append-only audit events — ``runspec.audit()``.

Every run already leaves a run record (``run_start`` / ``run_summary``). This
module is for the *important moments inside* a run, or outside one: a refund
issued, a venv provisioned, a deny rule added. Each event is written, best-effort,
to up to three places:

1. **The venv ledger** ``{sys.prefix}/logs/audit.jsonl`` — opened ``O_APPEND``,
   never rotated or truncated, and **hash-chained**: each line carries the hash
   of the previous line (``prev``) and its own (``hash``), so editing or deleting
   a line breaks the chain and :func:`verify` / ``runspec audit verify`` names the
   first bad line. Only the venv owner (or root) appends to it; another account's
   events ride its per-run inbox file and the owner's sweep folds them in with the
   kernel-attested account name (:func:`fold_inbox_lines`).
2. **The OS log** — syslog/journald on POSIX (facility ``auth`` by default), the
   Application event log on Windows. The user who wrote the event can't edit it,
   and it's usually already shipped off the machine. The message carries the
   ledger hash, so truncating the ledger is detectable against it.
3. **The run's own log** — a file-only record on the ``runspec.audit`` logger, so
   History / ``runspec logs`` show the event next to the run.

Honest limits: local files and local OS logs are tamper-*evident*, not
tamper-proof — root or a local administrator can always erase them. An immutable
record has to leave the machine (log shipping from syslog/journald).

Stdlib only. Never raises into the caller except: an invalid event name
(``ValueError`` — a programming error), and ``strict=True`` when no sink took the
event (:class:`AuditError`).
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import logging
import os
import re
import socket
import subprocess
import sys
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from . import logging_setup

__all__ = ["AuditError", "audit", "verify", "read_ledger", "ledger_path", "LEDGER_NAME", "GENESIS"]

LEDGER_NAME = "audit.jsonl"
GENESIS = "sha256:" + "0" * 64
RECORD_VERSION = 1
# Windows Application event-log id for audit events (eventcreate allows 1–1000).
WINDOWS_EVENT_ID = 200
# Prefix on every OS-log line so a log processor can pick audit events out.
SYSLOG_TAG = "runspec-audit"

_EVENT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")
_OUTCOME_RE = re.compile(r"^[a-z0-9_.-]{0,40}$")
_TAIL_BYTES = 65536


class AuditError(Exception):
    """``runspec.audit(..., strict=True)`` could not write the event anywhere."""


# ── public API ───────────────────────────────────────────────────────────────


def audit(
    event: str,
    outcome: str = "",
    *,
    target: Mapping[str, Any] | None = None,
    detail: Mapping[str, Any] | None = None,
    strict: bool = False,
    ledger_dir: str | os.PathLike[str] | None = None,
    syslog: bool | None = None,
) -> dict[str, Any] | None:
    """Record one audit event. Returns the record as written (with ``hash`` when
    it reached the ledger), or ``None`` when ``RUNSPEC_AUDIT=off``.

    ``event`` is a dotted lowercase name you choose (``payment.refund``,
    ``venv.provision``); ``outcome`` is a short word (``started``, ``succeeded``,
    ``failed``, ``denied``…). ``target`` says what was acted on, ``detail`` adds
    context; both are JSON objects and secret-looking keys/values are redacted.

    Inside a ``parse()``'d run the event is tied to the run (``run_id``,
    ``runnable``, ``command``, ``agent``, ``trigger``) automatically.

    ``strict=True`` raises :class:`AuditError` if no sink took the event — use it
    for actions that must not proceed unrecorded. ``ledger_dir`` writes the ledger
    in that directory instead of the venv's ``logs/``; ``syslog`` overrides
    ``[config.audit] syslog`` for this call.
    """
    if not isinstance(event, str) or not _EVENT_RE.match(event):
        raise ValueError(f"runspec.audit: event {event!r} must be lowercase letters, digits, '.', '_' or '-' (e.g. 'payment.refund')")
    outcome = str(outcome or "").strip().lower()
    if not _OUTCOME_RE.match(outcome):
        raise ValueError(f"runspec.audit: outcome {outcome!r} must be a short lowercase word (e.g. 'succeeded')")
    if logging_setup.audit_disabled():
        return None

    ctx = logging_setup.current_run()
    record = _build_record(event, outcome, target, detail, ctx)
    wrote: list[str] = []

    path = _ledger_target(ledger_dir, ctx)
    if path is not None:
        with contextlib.suppress(Exception):
            record = _append(path, record)
            wrote.append("ledger")

    cfg = ctx.get("audit_cfg") or {}
    use_os_log = cfg.get("syslog", True) if syslog is None else bool(syslog)
    if use_os_log and _os_log(record, facility=str(cfg.get("facility", "auth"))):
        wrote.append("os")

    if ctx:
        with contextlib.suppress(Exception):
            lg = logging.getLogger(logging_setup._AUDIT_LOGGER)
            if lg.hasHandlers():
                lg.info(f"audit {event} {outcome}".rstrip(), extra={"event": "audit", "audit": record})
                wrote.append("run_log")

    if strict and not wrote:
        raise AuditError(f"runspec.audit: could not record {event!r} anywhere (ledger, OS log and run log all unavailable)")
    return record


def ledger_path(log_dir: str | os.PathLike[str] | None = None) -> Path:
    """The ledger file in ``log_dir`` (default: the venv's ``logs/``)."""
    return Path(log_dir if log_dir is not None else Path(sys.prefix) / "logs") / LEDGER_NAME


def read_ledger(path: str | os.PathLike[str]) -> Iterator[dict[str, Any]]:
    """Yield the ledger's records in order (unparsable lines are skipped — use
    :func:`verify` to find them)."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line:
            continue
        with contextlib.suppress(ValueError):
            rec = json.loads(line)
            if isinstance(rec, dict):
                yield rec


def verify(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Check the ledger's hash chain. Returns ``{path, ok, records, first_bad,
    reason}`` — ``first_bad`` is the 1-based line number of the first line that
    fails (``None`` when the chain is intact), ``reason`` says why. A missing
    file is ``ok`` with 0 records."""
    result: dict[str, Any] = {"path": str(path), "ok": True, "records": 0, "first_bad": None, "reason": "", "last_hash": GENESIS}
    try:
        with open(path, "rb") as fh:
            raw_lines = fh.readlines()
    except FileNotFoundError:
        return result
    except OSError as exc:
        return {**result, "ok": False, "reason": f"can't read: {exc}"}
    prev = GENESIS
    for n, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
            if not isinstance(rec, dict):
                raise ValueError
        except ValueError:
            return {**result, "ok": False, "first_bad": n, "reason": "not a JSON record"}
        if rec.get("prev") != prev:
            return {**result, "ok": False, "first_bad": n, "reason": "chain broken — prev doesn't match the line before (a line was removed, inserted or reordered)"}
        body = {k: v for k, v in rec.items() if k != "hash"}
        if rec.get("hash") != _hash(body):
            return {**result, "ok": False, "first_bad": n, "reason": "hash mismatch — the line was edited"}
        prev = rec["hash"]
        result["records"] += 1
    result["last_hash"] = prev
    return result


# ── inbox fold (called by the owner's sweep) ─────────────────────────────────


def fold_inbox_lines(log_dir: Path, lines: list[str], *, os_user: str) -> int:
    """Append the audit events in a swept inbox file to the owner's ledger.

    ``os_user`` is the kernel-attested owner of the inbox file. It replaces the
    ``actor`` the writer claimed (kept as ``claimed_actor`` when different), and
    the record is marked ``via = "inbox"``. Returns how many were folded.
    Best-effort; never raises."""
    n = 0
    path = ledger_path(log_dir)
    for line in lines:
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(rec, dict) or rec.get("logger") != logging_setup._AUDIT_LOGGER:
            continue
        ev = (rec.get("extra") or {}).get("audit")
        if not isinstance(ev, dict) or not isinstance(ev.get("event"), str):
            continue
        ev = {k: v for k, v in ev.items() if k not in ("prev", "hash")}
        if ev.get("actor") != os_user:
            ev["claimed_actor"] = ev.get("actor")
            ev["actor"] = os_user
        ev["via"] = "inbox"
        with contextlib.suppress(Exception):
            _append(path, ev)
            n += 1
    return n


# ── internals ────────────────────────────────────────────────────────────────


def _actor() -> tuple[str, int | None]:
    if hasattr(os, "geteuid"):
        uid = os.geteuid()
        name = logging_setup._effective_user_name()
        return name or str(uid), uid
    try:
        import getpass

        return getpass.getuser(), None
    except Exception:  # noqa: BLE001
        return "", None


def _redact(obj: Any, key: str = "") -> Any:
    if key and logging_setup._SENSITIVE_KEYS.match(key):
        return "[REDACTED]"
    if isinstance(obj, Mapping):
        return {str(k): _redact(v, str(k)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    if isinstance(obj, str):
        return logging_setup._redact_value("", obj)
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    return logging_setup._redact_value("", str(obj))


def _build_record(event: str, outcome: str, target: Mapping[str, Any] | None, detail: Mapping[str, Any] | None, ctx: dict[str, Any]) -> dict[str, Any]:
    actor, uid = _actor()
    rec: dict[str, Any] = {
        "v": RECORD_VERSION,
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "id": str(uuid.uuid4()),
        "event": event,
        "outcome": outcome,
        "actor": actor,
        "uid": uid,
        "host": _hostname(),
        "pid": os.getpid(),
        "venv": sys.prefix,
        "runnable": ctx.get("runnable", ""),
        "command": " ".join(ctx.get("command_path") or []),
        "run_id": ctx.get("run_id", ""),
        "agent": bool(ctx.get("agent", os.environ.get("RUNSPEC_AGENT", "") not in ("", "0"))),
        "trigger": ctx.get("trigger", "") or os.environ.get("RUNSPEC_TRIGGER", ""),
        "target": _redact(dict(target or {})),
        "detail": _redact(dict(detail or {})),
    }
    # Normalise through JSON once so the hash is computed over exactly what's stored.
    out: dict[str, Any] = json.loads(json.dumps(rec, default=str))
    return out


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:  # noqa: BLE001
        return ""


def _hash(body: dict[str, Any]) -> str:
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _ledger_target(ledger_dir: str | os.PathLike[str] | None, ctx: dict[str, Any]) -> Path | None:
    """Where this process appends, or ``None`` (the event reaches the ledger via
    the inbox sweep, or there's nowhere writable)."""
    if ledger_dir is not None:
        return ledger_path(ledger_dir)
    if ctx.get("inbox"):
        return None  # a non-owner of a shared venv: the owner's sweep folds it in
    venv_logs = Path(sys.prefix) / "logs"
    if _may_append(venv_logs):
        return ledger_path(venv_logs)
    home_logs = Path.home() / "logs"
    if logging_setup._writable_dir(home_logs):
        return ledger_path(home_logs)
    return None


def _may_append(log_dir: Path) -> bool:
    """The venv ledger has one writer: the owner of ``logs/`` (root too — it
    hands a file it creates to the owner). Anyone else would create a file the
    owner can't append to."""
    if not hasattr(os, "geteuid"):
        return logging_setup._writable_dir(log_dir)
    try:
        if not log_dir.is_dir() and os.geteuid() != 0:
            # The venv owner creating logs/ for the first time.
            return Path(sys.prefix).stat().st_uid == os.geteuid() and logging_setup._writable_dir(log_dir)
        if not log_dir.is_dir():
            return logging_setup._writable_dir(log_dir, created_owner_from=Path(sys.prefix))
        return os.geteuid() in (0, log_dir.stat().st_uid)
    except OSError:
        return False


def _last_hash(fd: int) -> str:
    """The ``hash`` of the last line in the open ledger (``GENESIS`` when empty).
    An unparsable last line chains on the hash of its raw bytes, so ``verify``
    still reports it as the bad line instead of the chain silently restarting."""
    size = os.fstat(fd).st_size
    if size == 0:
        return GENESIS
    start = max(0, size - _TAIL_BYTES)
    data = os.pread(fd, size - start, start) if hasattr(os, "pread") else _read_at(fd, start, size - start)
    lines = [ln for ln in data.split(b"\n") if ln.strip()]
    if not lines:
        return GENESIS
    last = lines[-1]
    try:
        rec = json.loads(last)
        h = rec.get("hash") if isinstance(rec, dict) else None
        if isinstance(h, str) and h.startswith("sha256:"):
            return h
    except ValueError:
        pass
    return "sha256:" + hashlib.sha256(last).hexdigest()


def _read_at(fd: int, offset: int, length: int) -> bytes:
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, length)


def _append(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Append one chained line to the ledger under an exclusive lock. Raises on
    failure (callers suppress)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o644)
    try:
        if created:
            logging_setup._chown_like(path, path.parent)
        _lock(fd)
        try:
            body = {k: v for k, v in record.items() if k not in ("prev", "hash")}
            body["prev"] = _last_hash(fd)
            body["hash"] = _hash(body)
            os.write(fd, (json.dumps(body, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
            with contextlib.suppress(OSError):
                os.fsync(fd)
            return body
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _lock(fd: int) -> None:
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)
    except ImportError:  # Windows
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]


def _unlock(fd: int) -> None:
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
    except ImportError:  # Windows
        import msvcrt

        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]


_FACILITIES = ("auth", "authpriv", "user", "daemon", "local0", "local1", "local2", "local3", "local4", "local5", "local6", "local7")


def _os_log(record: dict[str, Any], *, facility: str = "auth") -> bool:
    """Write the event to the OS log. True when a log daemon was there to take it."""
    line = f"{SYSLOG_TAG} " + json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
    if sys.platform == "win32":
        return _windows_event(line)
    try:
        import syslog as _syslog
    except ImportError:
        return False
    sockets = ("/dev/log", "/var/run/syslog", "/var/run/log")
    if not any(os.path.exists(s) for s in sockets):
        return False
    name = facility if facility in _FACILITIES else "auth"
    fac = getattr(_syslog, f"LOG_{name.upper()}", _syslog.LOG_AUTH)
    try:
        _syslog.openlog("runspec", _syslog.LOG_PID, fac)
        _syslog.syslog(_syslog.LOG_NOTICE, line)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        with contextlib.suppress(Exception):
            _syslog.closelog()


def _windows_event(message: str) -> bool:
    """Application event log via the built-in ``eventcreate`` (no pywin32, no
    admin, no pre-registered source)."""
    try:
        result = subprocess.run(
            ["eventcreate", "/T", "INFORMATION", "/ID", str(WINDOWS_EVENT_ID), "/L", "APPLICATION", "/SO", "runspec", "/D", message[:1000]],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001
        return False
