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
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

_configured: bool = False  # idempotency guard — reset in tests via monkeypatch
_summary_state: dict[str, Any] | None = None  # populated when summary is enabled
_debug: bool = False  # mirrors the --debug flag; read by the excepthook for console rendering

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

# Directory of the installed runspec package — used to filter internal library
# frames out of the *displayed* compact traceback (the full trace still hits the file).
_RUNSPEC_DIR = os.path.dirname(os.path.abspath(__file__))


def configure_logging(
    log_cfg: dict[str, Any] | None,
    *,
    runnable_name: str,
    debug: bool = False,
    no_summary: bool = False,
    autonomy: str | None = None,
    agent: bool = False,
    command_path: list[str] | None = None,
    invocation_args: dict[str, Any] | None = None,
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

    Run summary (when `log_cfg["summary"]` is true and `no_summary` is false)
    counts log events by level and emits a single record at process exit
    with duration, exit code, exception class, and per-level counts.
    """
    global _configured, _summary_state, _debug
    if log_cfg is None or _configured:
        return

    _debug = debug
    floor = logging.DEBUG if debug else logging.INFO

    # Unique ID for this invocation — injected into every JSON log record so
    # multi-user or multi-run log files can be filtered by invocation.
    run_id = str(uuid.uuid4())

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Sensitive filter goes on each handler so it runs during propagation from
    # child loggers (root logger-level filters are not called during callHandlers
    # propagation — only handler-level filters are).
    sensitive = _SensitiveFilter()

    # Below WARNING → stdout (treated as the runnable's primary output).
    # Drop run-summary records — they go to the file only; the human-visible
    # form is written directly to stderr by the atexit hook.
    # Also suppress print-captured records (_from_print) to avoid double output.
    out_handler = logging.StreamHandler(sys.stdout)
    out_handler._runspec_stream = "stdout"  # type: ignore[attr-defined]
    out_handler.setLevel(floor)
    out_handler.addFilter(sensitive)
    out_handler.addFilter(lambda r: r.levelno < logging.WARNING and r.name != _RUN_SUMMARY_LOGGER and r.name != _EXCEPTION_LOGGER and not getattr(r, "_from_print", False))
    out_handler.setFormatter(_ConsoleFormatter(show_tracebacks=debug))
    root.addHandler(out_handler)

    # WARNING and above → stderr (Unix convention for diagnostics).
    err_handler = logging.StreamHandler(sys.stderr)
    err_handler._runspec_stream = "stderr"  # type: ignore[attr-defined]
    err_handler.setLevel(logging.WARNING)
    err_handler.addFilter(sensitive)
    err_handler.addFilter(lambda r: r.name != _RUN_SUMMARY_LOGGER and r.name != _EXCEPTION_LOGGER)
    err_handler.setFormatter(_ConsoleFormatter(show_tracebacks=debug))
    root.addHandler(err_handler)

    # File handler: always active, always JSON; level follows --debug
    # (INFO by default — keeps third-party DEBUG noise out of the audit log).
    log_dir = _resolve_log_dir()
    if log_cfg.get("store") == "per-run":
        # One file per invocation — multi-writer safe (no shared file, so no
        # rotation race when many users / parallel runs log at once). The
        # run_id in the name matches extra.run_id in the records, and the UTC
        # timestamp makes `ls` sort chronologically. rotate/keep are inert
        # here; retention is delegated to `runspec logs compact`/`prune`.
        # delay=True so a run that emits nothing leaves no empty file behind.
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = log_dir / f"{runnable_name}.{ts}.{run_id}.log"
        fh: logging.Handler = logging.FileHandler(log_path, encoding="utf-8", delay=True)
    else:
        log_path = log_dir / f"{runnable_name}.log"
        fh = _make_file_handler(log_path, log_cfg["rotate"], log_cfg["keep"])
    fh.setLevel(floor)
    fh.addFilter(sensitive)
    fh.addFilter(_RunIdFilter(run_id))
    fh.setFormatter(_JsonFormatter())
    root.addHandler(fh)

    # Run-summary counter handler — silently increments per-level counts.
    counter = _RunSummaryCounter()
    root.addHandler(counter)

    # Uncaught-exception handling is always installed (independent of the summary
    # toggle) so the structured exception record reaches the audit file even when
    # summary is off. The hook also reads _summary_state when present.
    _install_excepthook()

    runnable_prefix = runnable_name.upper().replace("-", "_")
    summary_enabled = bool(log_cfg.get("summary", True)) and not no_summary and not _env_truthy(f"RUNSPEC_{runnable_prefix}_ARG_NO_SUMMARY")
    if summary_enabled:
        user, user_target = _get_invoker()
        _summary_state = {
            "counter": counter,
            "start": time.monotonic(),
            "runnable": runnable_name,
            "autonomy": autonomy,
            "agent": agent,
            "command_path": list(command_path or []),
            "exception": None,
            "emitted": False,
            "user": user,
            "user_target": user_target,
            "run_id": run_id,
            "invocation_args": invocation_args or {},
        }
        atexit.register(_emit_run_summary)

    # Tee sys.stdout so print() calls also land in the file audit log.
    # Handlers above captured the real sys.stdout reference before this swap.
    # Register flush AFTER _emit_run_summary (LIFO → flush runs FIRST at exit).
    _print_logger = logging.getLogger("runspec.print")
    _tee = _StdoutTee(sys.stdout, _print_logger)
    atexit.register(_tee.flush_remaining)
    sys.stdout = _tee

    _configured = True


def _resolve_log_dir() -> Path:
    """Use `{sys.prefix}/logs`; fall back to `~/logs` if not writable.

    The venv root is the right home for an installed package's logs — one
    logs dir per environment, survives `pip install -e .`, easy to locate,
    and avoids scattering log files across every package directory.
    """
    candidate = Path(sys.prefix) / "logs"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".wtest"
        probe.touch()
        probe.unlink()
        return candidate
    except (OSError, PermissionError):
        fallback = Path.home() / "logs"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


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


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


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
    ws = "s" if warnings != 1 else ""
    es = "s" if errors != 1 else ""
    events = f"{total} events ({warnings} warning{ws}, {errors} error{es})"
    if state["exception"] or exit_code != 0:
        exc = state["exception"]
        exc_part = f", {exc['type']}" if exc else ""
        return f"runspec: {runnable} failed in {secs:.2f}s — exit {exit_code}{exc_part} — {events}{user_part}"
    return f"runspec: {runnable} completed in {secs:.2f}s — {events}{user_part}"


def _emit_run_summary() -> None:
    """atexit hook — emit one summary record to the file and one line to stderr."""
    state = _summary_state
    if state is None or state["emitted"]:
        return
    state["emitted"] = True

    duration_ms = int((time.monotonic() - state["start"]) * 1000)
    # Best-effort exit code — sys.exit() sets sys.last_value; for normal exits
    # there's no reliable hook so we infer from the captured exception.
    exit_code = 1 if state["exception"] else 0

    # File record via the standard logger — picked up by the file handler only
    # (console handlers filter out runspec.runsummary by logger name).
    invocation_args = state.get("invocation_args", {})
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
                "autonomy": state["autonomy"],
                "exception": state["exception"],
                "events": dict(state["counter"].counts),
                "user": state["user"],
                "user_target": state["user_target"],
                "invocation_args": {k: v["value"] for k, v in invocation_args.items()},
                "arg_sources": {k: v["source"] for k, v in invocation_args.items()},
            },
        )

    with contextlib.suppress(Exception):
        sys.stderr.write(_format_summary_line(state, duration_ms, exit_code) + "\n")
        sys.stderr.flush()
