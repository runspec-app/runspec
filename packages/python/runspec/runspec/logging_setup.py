"""Configure Python stdlib logging from [config.logging]. Zero new deps."""

from __future__ import annotations

import atexit
import contextlib
import datetime
import json
import logging
import logging.handlers
import os
import re
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

_configured: bool = False  # idempotency guard — reset in tests via monkeypatch
_summary_state: dict[str, Any] | None = None  # populated when summary is enabled
_debug: bool = False  # mirrors the --debug flag; read by the excepthook for console rendering
# What a `runspec exec` wrapper handed down besides the run id (taken by
# adopt_exec_run_id): the inbox file to join (RUNSPEC_RUN_LOG) and the wrapper's
# uid (RUNSPEC_EXEC_UID). Reset in tests via monkeypatch.
_exec_handoff: dict[str, Any] = {}
# What `runspec.audit()` needs to tie an event to the current run: run id,
# runnable, command path, provenance, where the run logs and whether that's the
# inbox. Set by configure_logging; empty outside a configured run. Reset in tests
# via monkeypatch.
_run_context: dict[str, Any] = {}

# `RUNSPEC_AUDIT=off` — for test suites and CI only. Restores the pre-default
# behaviour: a runnable with no [config.logging] configures nothing, and
# runspec.audit() writes nothing. There is deliberately no package-level switch.
AUDIT_ENV = "RUNSPEC_AUDIT"

_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(KB|MB|GB)$", re.IGNORECASE)
_SIZE_MULT: dict[str, int] = {"KB": 1024, "MB": 1024**2, "GB": 1024**3}
_TIMED: dict[str, tuple[str, int]] = {
    "daily": ("D", 1),
    "midnight": ("midnight", 1),
    "weekly": ("W0", 1),
}

# Keys whose values are always redacted regardless of content.
_SENSITIVE_KEYS = re.compile(r"^(password|passwd|pwd|token|api[_\-]?key|secret)$", re.IGNORECASE)

# Standard LogRecord attributes — anything else on the record is user-supplied extra.
_LOGRECORD_ATTRS: frozenset[str] = frozenset(
    {
        "args",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

# Pre-compiled patterns: passwords, tokens, bearer/basic auth, URL credentials,
# JSON fields, and form-encoded values in HTTP bodies.
_SENSITIVE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(password|passwd|pwd)\s*[=:]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)(token|api[_\-]?key|secret)\s*[=:]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)Authorization:\s*(Bearer|Basic)\s+\S+"), r"Authorization: \1 [REDACTED]"),
    (re.compile(r"https?://[^:@\s]+:[^@\s]+@"), "https://[REDACTED]@"),
    (re.compile(r'(?i)"(password|token|api_key|secret)"\s*:\s*"[^"]*"'), r'"\1": "[REDACTED]"'),
    (re.compile(r"(?i)(password|passwd|token)=([^&\s\"]+)"), r"\1=[REDACTED]"),
]

_RUN_SUMMARY_LOGGER = "runspec.runsummary"
# Uncaught exceptions are emitted on this dedicated logger so the file handler
# records them while the console handlers drop them by name — console display is
# handled explicitly inside the excepthook (debug-gated).
_EXCEPTION_LOGGER = "runspec.exception"
# runspec.audit() events — file-only like the two above (never stdout/stderr).
_AUDIT_LOGGER = "runspec.audit"
# Loggers the console handlers drop by name: their records belong in the file.
_FILE_ONLY_LOGGERS = frozenset({_RUN_SUMMARY_LOGGER, _EXCEPTION_LOGGER, _AUDIT_LOGGER})

# Directory of the installed runspec package — used to filter internal library
# frames out of the *displayed* compact traceback (the full trace still hits the file).
_RUNSPEC_DIR = os.path.dirname(os.path.abspath(__file__))


# Loggers of HTTP client libraries whose per-request INFO lines are noise on
# stdout (httpx logs every request at INFO). Matched with their children.
_HTTP_CLIENT_LOGGERS = ("httpx", "httpcore", "urllib3")


def _is_http_client_chatter(name: str) -> bool:
    return any(name == n or name.startswith(n + ".") for n in _HTTP_CLIENT_LOGGERS)


def audit_disabled() -> bool:
    """True when ``RUNSPEC_AUDIT=off`` (tests/CI only — see :data:`AUDIT_ENV`)."""
    return os.environ.get(AUDIT_ENV, "").strip().lower() in ("off", "0", "false", "no")


def default_logging_config() -> dict[str, Any]:
    """The normalised [config.logging] a runnable gets when it declares none —
    logging and the run audit are on by default (runnables are CLI *and* agent
    tools, so an unrecorded run is the wrong default)."""
    from .loader import _normalise_logging

    cfg = _normalise_logging({})
    assert cfg is not None
    return cfg


def resolve_logging_config(declared: dict[str, Any] | None, *, implicit: bool = True) -> dict[str, Any] | None:
    """The logging config ``parse()`` acts on.

    ``declared`` is the normalised [config.logging] (``None`` when absent). An
    absent section means the defaults unless ``implicit`` is false (the core
    ``runspec`` CLI and the test harness) or ``RUNSPEC_AUDIT=off`` is set — then
    ``None``, the old opt-in behaviour. A declared section is always honoured."""
    if declared is not None:
        return declared
    if not implicit or audit_disabled():
        return None
    return default_logging_config()


def current_run() -> dict[str, Any]:
    """A copy of the current run's context for ``runspec.audit()`` — empty when
    this process has no configured run (a library caller, the console app)."""
    return dict(_run_context)


def configure_logging(
    log_cfg: dict[str, Any] | None,
    *,
    runnable_name: str,
    debug: bool = False,
    autonomy: str | None = None,
    agent: bool = False,
    trigger: str = "",
    skill: str = "",
    label: str = "",
    command_path: list[str] | None = None,
    invocation_args: dict[str, Any] | None = None,
    history_cfg: dict[str, Any] | None = None,
    run_id: str | None = None,
    run_as: str = "",
    audit_cfg: dict[str, Any] | None = None,
) -> None:
    """
    Configure root logger from normalised [config.logging].
    No-op when log_cfg is None. Idempotent — second call is silently ignored.

    Console routing follows Unix stream conventions so a single `logger.X` call
    works in both CLI mode (terminal output) and agent mode (captured by
    `runspec serve` as the MCP tool response):

      INFO     → stdout (plain message — reads like print())
      WARNING+ → stderr (prefixed with the level name)

    DEBUG is suppressed by default on both stdout and the file. Pass
    `debug=True` (set by the auto-added `--debug` flag / `RUNSPEC_ARG_DEBUG`
    env var) to include DEBUG records (and tracebacks on stdout) everywhere.
    One knob — stdout and file move together. Stderr stays pinned at
    WARNING regardless.

    File handler is always JSON; level follows the same `--debug` toggle as
    stdout (defaults to INFO). Log files land under `{sys.prefix}/logs/`
    (the venv root) so they survive package reinstalls and aren't scattered
    across package directories; falls back to `~/logs/` if the venv root
    isn't writable.

    `log_cfg["store"]` selects the file layout: `"single"` (default) is one
    rotating `{runnable}.log`; `"per-run"` writes one
    `{runnable}.{utc-ts}.{run_id}.log` per invocation — safe for multiple
    users / parallel runs (no shared file, hence no rotation race) at the cost
    of moving retention to `runspec logs compact`/`prune`.

    Run summary (when `log_cfg["summary"]` is true) counts log events by level
    and emits a single record at process exit with duration, exit code,
    exception class, and per-level counts to the audit file. The human-readable
    one-line echo to stderr is opt-in via `log_cfg["summary_console"]` (default
    false) so stdout stays clean for pipes and the line never reaches a
    stderr=ERROR log processor by default. The record itself is always
    written: `summary = false` is accepted for back-compat but ignored (0.71.0).
    """
    global _configured, _summary_state, _debug
    if log_cfg is None or _configured:
        return

    _debug = debug
    floor = logging.DEBUG if debug else logging.INFO

    # Unique ID for this invocation — injected into every JSON log record so
    # multi-user or multi-run log files can be filtered by invocation. Under
    # `runspec exec` the wrapper has already minted it and written the row, so
    # adopt its id (docs/design/runspec-exec.md §3.3).
    # `run_id` is passed by parse() from adopt_exec_run_id().
    run_id = run_id or str(uuid.uuid4())
    # enabled = false ⇒ "audit-only": leave the process's logging to the
    # developer (no root handlers/level, no console routing, no print tee, no
    # takeover excepthook) and attach the file handler only to runspec's own
    # file-only loggers, so the run record and audit events are still written.
    takeover = bool(log_cfg.get("enabled", True))

    root = logging.getLogger()
    if takeover:
        root.setLevel(logging.DEBUG)

    # Sensitive filter goes on each handler so it runs during propagation from
    # child loggers (root logger-level filters are not called during callHandlers
    # propagation — only handler-level filters are).
    sensitive = _SensitiveFilter()

    # Below WARNING → stdout (treated as the runnable's primary output).
    # Drop run-summary records — they go to the file only; the optional
    # human-visible echo is written directly to stderr by the atexit hook
    # (gated by `summary_console`).
    # Also suppress print-captured records (_from_print) to avoid double output.
    out_handler = logging.StreamHandler(sys.stdout)
    out_handler._runspec_stream = "stdout"  # type: ignore[attr-defined]
    out_handler.setLevel(floor)
    out_handler.addFilter(sensitive)
    out_handler.addFilter(lambda r: r.levelno < logging.WARNING and r.name not in _FILE_ONLY_LOGGERS and not getattr(r, "_from_print", False))
    if not debug:
        # HTTP client libraries log every request at INFO ("HTTP Request: GET
        # … 200 OK"). On stdout that lands ahead of a runnable's JSON output,
        # so keep it in the audit file and show it on stdout only with --debug.
        out_handler.addFilter(lambda r: not _is_http_client_chatter(r.name))
    out_handler.setFormatter(_ConsoleFormatter(show_tracebacks=debug))
    if takeover:
        root.addHandler(out_handler)

    # WARNING and above → stderr (Unix convention for diagnostics).
    err_handler = logging.StreamHandler(sys.stderr)
    err_handler._runspec_stream = "stderr"  # type: ignore[attr-defined]
    err_handler.setLevel(logging.WARNING)
    err_handler.addFilter(sensitive)
    err_handler.addFilter(lambda r: r.name not in _FILE_ONLY_LOGGERS)
    err_handler.setFormatter(_ConsoleFormatter(show_tracebacks=debug))
    if takeover:
        root.addHandler(err_handler)

    # File handler: always active, always JSON; level follows --debug
    # (INFO by default — keeps third-party DEBUG noise out of the audit log).
    # A non-owner of a shared venv writes its per-run file into the logs inbox
    # (docs/design/venv-logs-inbox.md); the owner and root create/repair it.
    write_history = bool((history_cfg or {}).get("db", True))
    provisioned, inbox_run, run_id = _setup_inbox(runnable_name, run_id)
    fh: logging.Handler | None = None
    if inbox_run is not None:
        log_dir = inbox_run.path.parent.parent
        log_path = inbox_run.path
        fh = inbox_run.handler()
    else:
        log_dir = _resolve_log_dir()
        log_path = log_dir / f"{runnable_name}.log"
        if not provisioned:
            provisioned = _provision_inbox(log_dir)
    if fh is None and log_cfg.get("store") != "per-run" and _single_store_ok(log_dir):
        try:
            fh = _make_file_handler(log_path, log_cfg["rotate"], log_cfg["keep"])
        except PermissionError:
            # Another account's {runnable}.log in a shared dir: log per-run
            # instead of failing the run over its audit log.
            fh = None
    if fh is None:
        # One file per invocation — multi-writer safe (no shared file, so no
        # rotation race when many users / parallel runs log at once). The
        # run_id in the name matches extra.run_id in the records, and the UTC
        # timestamp makes `ls` sort chronologically. rotate/keep are inert
        # here; retention is delegated to `runspec logs compact`/`prune`.
        # delay=True so a run that emits nothing leaves no empty file behind.
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = log_dir / f"{runnable_name}.{ts}.{run_id}.log"
        fh = logging.FileHandler(log_path, encoding="utf-8", delay=True)
    fh.setLevel(floor)
    fh.addFilter(sensitive)
    fh.addFilter(_RunIdFilter(run_id))
    fh.setFormatter(_JsonFormatter())
    if takeover:
        root.addHandler(fh)
    else:
        for name in _FILE_ONLY_LOGGERS:
            own = logging.getLogger(name)
            own.setLevel(logging.DEBUG)
            own.propagate = False
            own.addHandler(fh)
    _run_context.clear()
    _run_context.update(
        {
            "run_id": run_id,
            "runnable": runnable_name,
            "command_path": list(command_path or []),
            "agent": agent,
            "trigger": trigger,
            "log_dir": str(log_dir),
            "inbox": inbox_run is not None,
            "audit_cfg": dict(audit_cfg or {}),
        }
    )
    if provisioned in ("created", "repaired"):
        # Audit-log only (the console handlers drop this logger): never stdout/stderr.
        with contextlib.suppress(Exception):
            logging.getLogger(_RUN_SUMMARY_LOGGER).info(
                f"logs inbox {provisioned}",
                extra={"event": "inbox_created" if provisioned == "created" else "inbox_repaired", "path": str(log_dir / "inbox")},
            )

    # Run-summary counter handler — silently increments per-level counts.
    counter = _RunSummaryCounter()
    if takeover:
        root.addHandler(counter)

    # Uncaught-exception handling is always installed (independent of the summary
    # toggle) so the structured exception record reaches the audit file even when
    # summary is off. The hook also reads _summary_state when present. In
    # audit-only mode it records and then hands over to the existing hook, so the
    # developer's own exception handling is unchanged.
    if takeover:
        _install_excepthook()
    else:
        _install_recording_excepthook()

    # The structured run record (the data the console history/analytics tabs and
    # `runspec logs` read) is part of the always-on run audit, so it is always
    # written: `[config.logging] summary = false` is still accepted but ignored
    # (0.71.0). `summary_console` gates the human-readable terminal echo — off by
    # default so stdout stays clean for pipes and the line never reaches a stream
    # that log processors (e.g. Rundeck, which tags stderr as ERROR) would flag.
    write_record = True
    write_console = bool(log_cfg.get("summary_console", False))
    # The runs index (runspec_runs.db) is a derived index over the audit log, so
    # it follows the same coverage: it writes when logging is configured, gated by
    # [config.history] db (default true, best-effort). See runsdb.py / the design
    # doc docs/design/console-history-db.md. An inbox writer never touches SQLite:
    # its run_start / run_summary records are what the owner's sweep indexes.
    if inbox_run is not None or write_record or write_console or write_history:
        user, user_target = _get_invoker()
        _summary_state = {
            "counter": counter,
            "start": time.monotonic(),
            "runnable": runnable_name,
            "autonomy": autonomy,
            "agent": agent,
            # Provenance of what *caused* this run (the event source id —
            # "schedule"/"outlook"/… — or "" for an interactive/manual run). An
            # axis orthogonal to `agent`: a run can be both scheduled and an LLM
            # turn. Set from RUNSPEC_TRIGGER, mirroring RUNSPEC_AGENT → agent.
            "trigger": trigger,
            # The saved skill that drove this run (RUNSPEC_SKILL), "" otherwise — a
            # further provenance axis alongside trigger/agent.
            "skill": skill,
            # Where the run was launched from (RUNSPEC_LABEL, "cli" when unset).
            "label": label,
            "command_path": list(command_path or []),
            "exception": None,
            "emitted": False,
            "user": user,
            "user_target": user_target,
            "run_id": run_id,
            "invocation_args": invocation_args or {},
            "write_record": write_record,
            "write_console": write_console,
            # runs-index (runspec_runs.db) — db_dir/history_cfg are what the atexit
            # finalize needs; the start row is written just below.
            "write_history": write_history and inbox_run is None,
            "history_cfg": history_cfg or {},
            "db_dir": log_dir,
            "inbox": inbox_run is not None,
            "run_as": run_as,
        }
        atexit.register(_emit_run_summary)
        if inbox_run is not None:
            _write_run_start(_summary_state, indexed=write_history)
        elif write_history:
            _begin_history_row(_summary_state, log_dir, log_path.name)

    # Tee sys.stdout so print() calls also land in the file audit log.
    # Handlers above captured the real sys.stdout reference before this swap.
    # Register flush AFTER _emit_run_summary (LIFO → flush runs FIRST at exit).
    if takeover:
        _print_logger = logging.getLogger("runspec.print")
        _tee = _StdoutTee(sys.stdout, _print_logger)
        atexit.register(_tee.flush_remaining)
        sys.stdout = _tee

    _configured = True


# A run id is embedded in the per-run log filename, so it must fit that grammar
# (runspec.logs._PER_RUN_RE): hex + dashes. Anything else is ignored.
_RUN_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def mark_mock_run(exit_code: int) -> None:
    """Flag this run as a mock playback (``RUNSPEC_MOCK``) and pin the exit
    code it is about to exit with, so the run record carries ``mock: true`` and
    the recorded code. No-op when no run record is being written."""
    state = _summary_state
    if state is not None:
        state["mock"] = True
        state["exit_code"] = exit_code


def adopt_exec_run_id() -> str | None:
    """Take the run id a ``runspec exec`` wrapper handed down in ``RUNSPEC_RUN_ID``.

    The variable is **removed from the environment** either way, so a runnable
    that shells out to other runnables doesn't pass the same id to them (they'd
    overwrite this run's row). Returns ``None`` when absent or malformed."""
    rid = os.environ.pop("RUNSPEC_RUN_ID", "").strip()
    # The inbox half of the handoff (docs/design/venv-logs-inbox.md §9) — popped
    # for the same reason: a runnable this one shells out to must not join our file.
    run_log = os.environ.pop("RUNSPEC_RUN_LOG", "").strip()
    exec_uid = os.environ.pop("RUNSPEC_EXEC_UID", "").strip()
    _exec_handoff.clear()
    if _RUN_ID_RE.match(rid):
        if run_log:
            _exec_handoff["run_log"] = run_log
        if exec_uid.isdigit():
            _exec_handoff["exec_uid"] = int(exec_uid)
        return rid
    return None


def _setup_inbox(runnable_name: str, run_id: str) -> tuple[str, Any, str]:
    """The inbox half of choosing where this run logs. Returns ``(provisioned,
    inbox_run, run_id)``:

    * the owner or root first create/repair ``{venv}/logs/inbox`` (one ``lstat``
      when it's already fine) — ``provisioned`` is what that did;
    * anyone but the owner (root included) then writes into the inbox when it's
      sane — ``inbox_run`` is the open, locked file (``None`` otherwise: the
      caller falls back to the old resolution).

    A ``runspec exec`` wrapper's inbox file (``RUNSPEC_RUN_LOG``) is joined when
    it's ours; when it isn't (the child runs as another account) we can't share
    it, so the run gets a fresh id rather than collide with the wrapper's.
    Best-effort — any failure returns ``("", None, run_id)``."""
    try:
        from . import inbox

        if not inbox.supported():
            return "", None, run_id
        venv_logs = Path(sys.prefix) / "logs"
        owner = inbox.venv_owner()
        if owner is None:
            return "", None, run_id
        euid = os.geteuid()
        provisioned = ""
        if euid in (owner, 0) and venv_logs.is_dir() and not venv_logs.is_symlink():
            provisioned = inbox.ensure_inbox(venv_logs, owner)
        if euid == owner:
            return provisioned, None, run_id
        join = _exec_handoff.get("run_log")
        if join:
            run = inbox.open_run(venv_logs, runnable_name, run_id, owner=owner, join=join)
            if run is not None:
                return provisioned, run, run_id
            run_id = str(uuid.uuid4())  # can't join the wrapper's file: don't collide with it
        return provisioned, inbox.open_run(venv_logs, runnable_name, run_id, owner=owner), run_id
    except Exception:
        return "", None, run_id


def _provision_inbox(log_dir: Path) -> str:
    """After the old resolution: the owner or root create/repair the inbox of the
    venv's own logs dir (e.g. the run that just created ``logs/``)."""
    with contextlib.suppress(Exception):
        from . import inbox

        if inbox.supported() and log_dir == Path(sys.prefix) / "logs":
            owner = inbox.venv_owner()
            if owner is not None and os.geteuid() in (owner, 0):
                return inbox.ensure_inbox(log_dir, owner)
    return ""


def _write_run_start(state: dict[str, Any], *, indexed: bool) -> None:
    """The first record of an inbox file — what the owner's sweep indexes the run
    from (no SQLite for a non-owner). A run that opted out of the index
    (``[config.history] db = false``) writes only a marker, so the sweep moves the
    file without indexing it."""
    with contextlib.suppress(Exception):
        import socket

        from . import runsdb

        if not indexed:
            extra: dict[str, Any] = {"event": "run_start", "run_id": state["run_id"], "history_db": False}
        else:
            extra = {
                "event": "run_start",
                "run_id": state["run_id"],
                "runnable": state["runnable"],
                "command_path": state["command_path"],
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "proc_start": runsdb.proc_start_token(),
                "user": state["user"],
                "user_target": state["user_target"],
                "agent": state["agent"],
                "trigger": state["trigger"],
                "skill": state.get("skill", ""),
                "label": state.get("label") or None,
                "autonomy": state["autonomy"],
                "run_as": state.get("run_as") or "",
                "invocation_args": {k: v["value"] for k, v in state.get("invocation_args", {}).items()},
                "start_ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "venv": sys.prefix,
            }
            if "exec_uid" in _exec_handoff:
                extra["exec_uid"] = _exec_handoff["exec_uid"]
        logging.getLogger(_RUN_SUMMARY_LOGGER).info("run started", extra=extra)


def _effective_user_name() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:  # noqa: BLE001 — Windows / unknown uid
        return ""


def _resolve_log_dir() -> Path:
    """Use `{sys.prefix}/logs`; fall back to `~/logs` if not writable.

    The venv root is the right home for an installed package's logs — one
    logs dir per environment, survives `pip install -e .`, easy to locate,
    and avoids scattering log files across every package directory.

    Never raises: when neither is writable (a service account whose home is
    `/nonexistent`, a read-only home) the run logs to a private temp dir and says
    so once on stderr — logging must never be the reason a run fails.
    """
    candidate = Path(sys.prefix) / "logs"
    if _writable_dir(candidate, created_owner_from=Path(sys.prefix)):
        return candidate
    fallback = Path.home() / "logs"
    if _writable_dir(fallback):
        return fallback
    last = Path(tempfile.mkdtemp(prefix="runspec-logs-"))
    with contextlib.suppress(Exception):
        sys.stderr.write(f"runspec: neither {candidate} nor {fallback} is writable — this run's audit log is in {last}\n")
    return last


def _writable_dir(d: Path, *, created_owner_from: Path | None = None) -> bool:
    """Can this process create files in ``d`` (creating ``d`` if missing)?

    The probe is a uniquely named temp file (``mkstemp``), not a fixed name: two
    runs starting at once used to race on a shared ``.wtest`` (one's ``unlink``
    removed the other's probe → a spurious fallback to ``~/logs``), and a
    ``.wtest`` left behind by another user made the probe fail outright.

    ``created_owner_from``: when *this* call creates ``d`` as root, hand it to the
    owner of that path (the venv root). Otherwise a root-run runnable that
    happens to run first leaves a root-owned ``logs/`` that locks the venv's own
    service account — and everyone else — out."""
    try:
        created = not d.exists()
        d.mkdir(parents=True, exist_ok=True)
        if created and created_owner_from is not None:
            _chown_like(d, created_owner_from)
        fd, probe = tempfile.mkstemp(prefix=".wtest.", dir=d)
        os.close(fd)
        os.unlink(probe)
        return True
    except (OSError, ValueError):
        return False


def _chown_like(path: Path, ref: Path) -> None:
    """When running as root, give ``path`` the owner/group of ``ref``. Best-effort;
    a no-op for non-root processes and on Windows."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    with contextlib.suppress(OSError):
        st = ref.stat()
        if st.st_uid != 0:
            os.chown(path, st.st_uid, st.st_gid)


def _single_store_ok(log_dir: Path) -> bool:
    """May this process use the shared rotating ``{runnable}.log`` in ``log_dir``?

    Only when it owns the dir (or on Windows, which has no ownership to check). A
    shared dir written by several accounts must use per-run files: the rotating
    file is created by whoever runs first, and every other account then either
    can't append to it (the run used to fail with ``PermissionError``) or races
    its rotation (docs/design/multi-writer-audit-logging.md)."""
    if not hasattr(os, "geteuid"):
        return True
    try:
        return log_dir.stat().st_uid == os.geteuid()
    except OSError:
        return False


def _make_file_handler(path: Path, rotate: str, keep: int) -> logging.Handler:
    m = _SIZE_RE.match(rotate)
    if m:
        max_bytes = int(float(m.group(1)) * _SIZE_MULT[m.group(2).upper()])
        return logging.handlers.RotatingFileHandler(path, maxBytes=max_bytes, backupCount=keep, encoding="utf-8")
    t = _TIMED.get(rotate.lower())
    if t:
        return logging.handlers.TimedRotatingFileHandler(path, when=t[0], interval=t[1], backupCount=keep, encoding="utf-8")
    raise ValueError(f"✗  [config.logging] rotate {rotate!r} not recognised.\n   Valid: '10 MB', '100 KB', '1 GB', 'daily', 'midnight', 'weekly'")


def _redact_value(key: str, val: str) -> str:
    if _SENSITIVE_KEYS.match(key):
        return "[REDACTED]"
    for pattern, replacement in _SENSITIVE:
        val = pattern.sub(replacement, val)
    return val


def _collect_extra(record: logging.LogRecord) -> dict[str, Any]:
    """Collect user-supplied extra fields, redacting sensitive string values."""
    result: dict[str, Any] = {}
    for key, val in list(record.__dict__.items()):
        if key in _LOGRECORD_ATTRS or key == "exc_structured" or key.startswith("_"):
            continue
        result[key] = _redact_value(key, val) if isinstance(val, str) else val
    return result


def _get_invoker() -> tuple[str, str | None]:
    """Return (invoker, sudo_target). sudo_target is None when not running under sudo."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        target = os.environ.get("USER") or os.environ.get("LOGNAME") or "root"
        return sudo_user, target
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or os.environ.get("USERNAME") or "unknown"
    return user, None


class _SensitiveFilter(logging.Filter):
    """Redacts passwords, tokens, and credentials from all log output."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            for pattern, replacement in _SENSITIVE:
                msg = pattern.sub(replacement, msg)
            record.msg = msg
            record.args = ()  # already formatted — prevent double-substitution
        except Exception:
            pass  # never disrupt logging on filter errors
        return True


class _RunSummaryCounter(logging.Handler):
    """Counts log records by level. Emits nothing — read at process exit."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.counts: dict[str, int] = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}

    def emit(self, record: logging.LogRecord) -> None:
        # Don't count runspec's own bookkeeping records (the summary record and
        # the uncaught-exception record) — the exception is surfaced via the
        # summary's exit_code/exception fields, not as a user-visible event.
        if record.name in (_RUN_SUMMARY_LOGGER, _EXCEPTION_LOGGER):
            return
        if record.levelname in self.counts:
            self.counts[record.levelname] += 1


class _RunIdFilter(logging.Filter):
    """Injects _run_id onto every LogRecord so the JSON formatter can include it."""

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self._run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record._run_id = self._run_id
        return True


class _StdoutTee:
    """Replaces sys.stdout; tees writes to original stdout AND logger.info.

    print() calls reach the original stdout unchanged (so MCP piping/agent
    capture still works), and each complete line is also forwarded to the
    logging pipeline with _from_print=True so the file handler captures it
    in the audit log.  The _from_print marker is checked by the out_handler
    filter to prevent double-printing.
    """

    def __init__(self, original: Any, logger: logging.Logger) -> None:
        self._original = original
        self._logger = logger
        self._buf = ""
        self.encoding = getattr(original, "encoding", "utf-8")
        self.errors = getattr(original, "errors", "replace")

    def write(self, data: str) -> int:
        self._original.write(data)
        self._buf += data
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line:
                self._logger.info(line, extra={"_from_print": True})
        return len(data)

    def flush(self) -> None:
        self._original.flush()

    def flush_remaining(self) -> None:
        """atexit hook — flush any partial line still in the buffer."""
        if self._buf:
            self._logger.info(self._buf, extra={"_from_print": True})
            self._buf = ""

    def fileno(self) -> int:
        return int(self._original.fileno())

    def isatty(self) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


class _JsonFormatter(logging.Formatter):
    """Structured JSON: ts, level, logger, message, exc (when present)."""

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        obj: dict[str, Any] = {
            "ts": datetime.datetime.fromtimestamp(record.created, tz=datetime.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.message,
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
            record.exc_text = obj["exc"]
        exc_structured = getattr(record, "exc_structured", None)
        if exc_structured:
            obj["exc_structured"] = exc_structured
        extra = _collect_extra(record)
        run_id = getattr(record, "_run_id", None)
        if run_id:
            extra["run_id"] = run_id
        if extra:
            obj["extra"] = extra
        return json.dumps(obj)


class _ConsoleFormatter(logging.Formatter):
    """
    Minimal console formatter — INFO reads like a print() call; higher levels
    are clearly flagged so they stand out in a stream of normal output.

      DEBUG    → "DEBUG file.py:42: message"  (file/line aids debugging)
      INFO     → "message"                    (plain print — most common case)
      WARNING  → "WARNING: message"
      ERROR    → "ERROR: message"
      CRITICAL → "CRITICAL: message"

    Tracebacks shown only when show_tracebacks=True (level == debug).
    """

    def __init__(self, show_tracebacks: bool = False) -> None:
        super().__init__()
        self._show_tb = show_tracebacks

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.levelno == logging.DEBUG:
            line = f"DEBUG {record.filename}:{record.lineno}: {msg}"
        elif record.levelno == logging.INFO:
            line = msg
        else:
            line = f"{record.levelname}: {msg}"

        extra = _collect_extra(record)
        if extra:
            line += f"  {{{' '.join(f'{k}={v}' for k, v in extra.items())}}}"

        if record.exc_info and self._show_tb:
            line += "\n" + self.formatException(record.exc_info)

        return line


# ── Uncaught exceptions ──────────────────────────────────────────────────────

_original_excepthook = sys.excepthook
_excepthook_installed: bool = False


def _build_exc_structured(exc_type: type[BaseException], exc_value: BaseException, tb: Any) -> dict[str, Any]:
    """Clean, machine-readable form of an exception for the JSON audit / UI tables.

    `frames` is the full, unfiltered call stack (innermost last). `file` is the
    absolute path so a UI can offer click-to-open; the console render shortens it.
    """
    frames = [
        {
            "file": fs.filename,
            "line": fs.lineno,
            "func": fs.name,
            "code": fs.line or None,
        }
        for fs in traceback.extract_tb(tb)
    ]
    module = os.path.splitext(os.path.basename(str(frames[-1]["file"])))[0] if frames else None
    return {
        "type": exc_type.__name__,
        "message": str(exc_value),
        "module": module,
        "frames": frames,
    }


def _format_compact_traceback(exc_type: type[BaseException], exc_value: BaseException, frames: list[dict[str, Any]]) -> str:
    """Neat, aligned, stdlib-only traceback for --debug console output.

    Internal runspec frames are dropped from the *display* (the full trace still
    lands in the audit file). Falls back to the full list if filtering empties it.
    """
    shown = [f for f in frames if not str(f["file"]).startswith(_RUNSPEC_DIR)] or frames
    locs = [f"{os.path.basename(str(f['file']))}:{f['line']}" for f in shown]
    width = max((len(loc) for loc in locs), default=0)
    lines = [f"{exc_type.__name__}: {exc_value}", ""]
    for loc, frame in zip(locs, shown, strict=False):
        code = (frame.get("code") or "").strip()
        lines.append(f"  {loc.ljust(width)}  {code}".rstrip())
    return "\n".join(lines)


def install_excepthook(*, debug: bool = False) -> None:
    """Install the uncaught-exception hook ahead of full logging configuration.

    `parse()` calls this as soon as it sees `[config.logging]`, before running
    the rest of its pipeline, so a failure inside runspec's *own* parse code
    (inference, argv parsing, validation, coercion, RunSpec build) is routed
    through the same one-liner / `--debug` handler as a runtime exception instead
    of dumping a raw traceback. `configure_logging` calls `_install_excepthook`
    again at the end of a successful parse (idempotent) and sets the final
    `--debug` value, summary state, and file handler; the structured audit record
    is written only once logging is configured — see the `_configured` guard in
    the hook.

    `debug` brings the compact-trace rendering forward so re-running a crashing
    invocation as `<cmd> --debug` shows the trace even when the failure happens
    inside the parse pipeline (before configure_logging would set it).
    """
    global _debug
    if not _excepthook_installed and debug:
        _debug = True
    _install_excepthook()


def _install_excepthook() -> None:
    """Wrap sys.excepthook so uncaught exceptions are recorded uniformly.

    Always emits a structured record to the audit file (via the file-only
    `runspec.exception` logger). On the console it prints a neat compact
    traceback when --debug is set, otherwise a single concise line. The default
    hook is *not* chained — that is the deliberate behaviour change. Idempotent.
    """
    global _excepthook_installed, _original_excepthook
    if _excepthook_installed:
        return
    _original_excepthook = sys.excepthook

    def hook(exc_type: type[BaseException], exc_value: BaseException, tb: Any) -> None:
        structured = _build_exc_structured(exc_type, exc_value, tb)

        # Audit file — once logging is configured (independent of --debug and of
        # the summary toggle). When the hook fires before configure_logging has
        # run (a failure inside runspec's own parse pipeline, where the hook is
        # installed early) the root logger has no handlers, so emitting here
        # would fall through to logging.lastResort and dump the very traceback
        # we are deliberately suppressing. Skip the record in that window — the
        # console one-liner below still fires.
        if _configured:
            with contextlib.suppress(Exception):
                logging.getLogger(_EXCEPTION_LOGGER).critical(
                    "uncaught exception",
                    exc_info=(exc_type, exc_value, tb),
                    extra={"exc_structured": structured},
                )

        # Feed the run-summary line (when summary is on) — unchanged shape.
        if _summary_state is not None:
            _summary_state["exception"] = {
                "type": exc_type.__name__,
                "message": str(exc_value),
                "traceback": "".join(traceback.format_exception(exc_type, exc_value, tb)),
            }

        # Console: full compact trace only with --debug, else a one-liner.
        try:
            if _debug:
                sys.stderr.write(_format_compact_traceback(exc_type, exc_value, structured["frames"]) + "\n")
            else:
                sys.stderr.write(f"ERROR: {exc_type.__name__}: {exc_value}  (run with --debug for traceback)\n")
            sys.stderr.flush()
        except Exception:
            _original_excepthook(exc_type, exc_value, tb)  # safety fallback

    sys.excepthook = hook
    _excepthook_installed = True


def _install_recording_excepthook() -> None:
    """Audit-only mode's hook: write the structured exception record and feed the
    run summary, then call whatever hook was installed before — console output
    stays the developer's. Idempotent."""
    global _excepthook_installed
    if _excepthook_installed:
        return
    previous = sys.excepthook

    def hook(exc_type: type[BaseException], exc_value: BaseException, tb: Any) -> None:
        with contextlib.suppress(Exception):
            structured = _build_exc_structured(exc_type, exc_value, tb)
            logging.getLogger(_EXCEPTION_LOGGER).critical(
                "uncaught exception",
                exc_info=(exc_type, exc_value, tb),
                extra={"exc_structured": structured},
            )
        if _summary_state is not None:
            _summary_state["exception"] = {
                "type": exc_type.__name__,
                "message": str(exc_value),
                "traceback": "".join(traceback.format_exception(exc_type, exc_value, tb)),
            }
        previous(exc_type, exc_value, tb)

    sys.excepthook = hook
    _excepthook_installed = True


# ── Run summary ──────────────────────────────────────────────────────────────


def _format_summary_line(state: dict[str, Any], duration_ms: int, exit_code: int) -> str:
    """One-line stderr summary — runs in atexit so it must never raise."""
    counts = state["counter"].counts
    total = sum(counts.values())
    warnings = counts["WARNING"]
    errors = counts["ERROR"] + counts["CRITICAL"]
    secs = duration_ms / 1000.0
    runnable = state["runnable"]
    user = state.get("user", "unknown")
    user_target = state.get("user_target")
    user_part = f" | user: {user} → {user_target} (sudo)" if user_target else f" | user: {user}"
    # Abbreviated level words ("warn"/"err", never "warning"/"error") keep the
    # line terse. (Stream-tagging log processors like Rundeck classify by stream,
    # not text — this echo is opt-in and stays on stderr, so a clean run isn't
    # flagged ERROR unless an operator turns `summary_console` on in that env.)
    events = f"{total} events ({warnings} warn, {errors} err)"
    if state["exception"] or exit_code != 0:
        exc = state["exception"]
        exc_part = f", {exc['type']}" if exc else ""
        return f"runspec: {runnable} failed in {secs:.2f}s — exit {exit_code}{exc_part} — {events}{user_part}"
    return f"runspec: {runnable} completed in {secs:.2f}s — {events}{user_part}"


def _begin_history_row(state: dict[str, Any], log_dir: Path, log_file: str) -> None:
    """Write the ``running`` row to the per-venv runs index at run start.

    Best-effort and fully isolated (a runsdb import or write failure never
    affects the run or the rest of logging). The row is finalized by
    :func:`_emit_run_summary` at exit; an orphaned row (crash/SIGKILL — no atexit)
    is reaped by pid-liveness later. See runsdb.py."""
    with contextlib.suppress(Exception):
        import socket

        from . import runsdb

        runsdb.begin_run(
            log_dir,
            {
                "run_id": state["run_id"],
                "runnable": state["runnable"],
                "command": " ".join(state["command_path"]),
                "status": "running",
                "start_ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "user": state["user"],
                "user_target": state["user_target"],
                "agent": state["agent"],
                "trigger": state["trigger"],
                "skill": state.get("skill", ""),
                "label": state.get("label") or None,
                "autonomy": state["autonomy"],
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "proc_start": runsdb.proc_start_token(),
                "log_file": log_file,
                "args": {k: v["value"] for k, v in state.get("invocation_args", {}).items()},
                "run_as": state.get("run_as") or None,
                # v6 attribution: a direct row is written by the effective account.
                "os_user": _effective_user_name(),
                "source": "direct",
                "exec_uid": _exec_handoff.get("exec_uid"),
            },
        )


def _emit_run_summary() -> None:
    """atexit hook — emit the summary record to the file (gated by `summary`),
    finalize the runs-index row (gated by `write_history`), and, when
    `summary_console` is on, one human-readable line to stderr."""
    state = _summary_state
    if state is None or state["emitted"]:
        return
    state["emitted"] = True

    duration_ms = int((time.monotonic() - state["start"]) * 1000)
    # Best-effort exit code — sys.exit() sets sys.last_value; for normal exits
    # there's no reliable hook so we infer from the captured exception.
    exit_code = 1 if state["exception"] else 0
    if state.get("exit_code") is not None:
        exit_code = state["exit_code"]  # pinned by mark_mock_run

    # File record via the standard logger — picked up by the file handler only
    # (console handlers filter out runspec.runsummary by logger name). Always on;
    # this is the record the console + `runspec logs` consume.
    invocation_args = state.get("invocation_args", {})
    if state.get("write_record", True):
        with contextlib.suppress(Exception):
            logging.getLogger(_RUN_SUMMARY_LOGGER).info(
                "run completed",
                extra={
                    "event": "run_summary",
                    "run_id": state.get("run_id"),
                    "runnable": state["runnable"],
                    "command_path": state["command_path"],
                    "duration_ms": duration_ms,
                    "exit_code": exit_code,
                    "agent": state["agent"],
                    "trigger": state["trigger"],
                    "skill": state.get("skill", ""),
                    "label": state.get("label") or None,
                    "autonomy": state["autonomy"],
                    "exception": state["exception"],
                    "events": dict(state["counter"].counts),
                    "user": state["user"],
                    "user_target": state["user_target"],
                    "invocation_args": {k: v["value"] for k, v in invocation_args.items()},
                    "arg_sources": {k: v["source"] for k, v in invocation_args.items()},
                    **({"mock": True} if state.get("mock") else {}),
                },
            )

    # Finalize the runs-index row (running → completed/failed + exit code +
    # duration), then its opportunistic maintenance (reap orphans + ring-prune).
    # Best-effort and isolated — never affects the run or the rest of exit.
    if state.get("write_history"):
        with contextlib.suppress(Exception):
            from . import runsdb

            runsdb.finish_run(
                state["db_dir"],
                state.get("run_id") or "",
                exit_code=exit_code,
                duration_ms=duration_ms,
                status="failed" if (state["exception"] or exit_code != 0) else "completed",
                cfg=state.get("history_cfg") or {},
                # Carry the name + command so a fallback INSERT (no start row —
                # begin_run lost its race or the index was unwritable at start) is
                # never nameless in History.
                runnable=state["runnable"],
                command=" ".join(state["command_path"]),
                # v5: what Analytics aggregates — log-record counts by level and
                # the uncaught exception (type + message; the traceback stays in
                # the log).
                events=dict(state["counter"].counts),
                exception=state["exception"],
            )

    # Human-readable echo — opt-in (`summary_console`). Stays on stderr so it
    # never pollutes stdout pipes; off by default so it never reaches Rundeck's
    # stderr=ERROR tagging unless the operator explicitly asks for it.
    if state.get("write_console", False):
        with contextlib.suppress(Exception):
            sys.stderr.write(_format_summary_line(state, duration_ms, exit_code) + "\n")
            sys.stderr.flush()
