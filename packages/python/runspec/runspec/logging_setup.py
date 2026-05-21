"""Configure Python stdlib logging from [config.logging]. Zero new deps."""

from __future__ import annotations

import datetime
import json
import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any

_configured: bool = False  # idempotency guard — reset in tests via monkeypatch

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


def configure_logging(
    log_cfg: dict[str, Any] | None,
    *,
    runnable_name: str,
    config_path: Path,
    debug: bool = False,
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
    `debug=True` (set by the auto-added `--debug` flag / `RUNSPEC_DEBUG`
    env var) to include DEBUG records (and tracebacks on stdout) everywhere.
    One knob — stdout and file move together. Stderr stays pinned at
    WARNING regardless.

    File handler is always JSON; level follows the same `--debug` toggle as
    stdout (defaults to INFO).
    """
    global _configured
    if log_cfg is None or _configured:
        return

    floor = logging.DEBUG if debug else logging.INFO

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addFilter(_SensitiveFilter())

    # Below WARNING → stdout (treated as the runnable's primary output)
    out_handler = logging.StreamHandler(sys.stdout)
    out_handler.setLevel(floor)
    out_handler.addFilter(lambda r: r.levelno < logging.WARNING)
    out_handler.setFormatter(_ConsoleFormatter(show_tracebacks=debug))
    root.addHandler(out_handler)

    # WARNING and above → stderr (Unix convention for diagnostics)
    err_handler = logging.StreamHandler(sys.stderr)
    err_handler.setLevel(logging.WARNING)
    err_handler.setFormatter(_ConsoleFormatter(show_tracebacks=debug))
    root.addHandler(err_handler)

    # File handler: always active, always JSON; level follows --debug
    # (INFO by default — keeps third-party DEBUG noise out of the audit log).
    log_dir = _resolve_log_dir(config_path)
    log_path = log_dir / f"{runnable_name}.log"
    fh = _make_file_handler(log_path, log_cfg["rotate"], log_cfg["keep"])
    fh.setLevel(floor)
    fh.setFormatter(_JsonFormatter())
    root.addHandler(fh)

    _configured = True


def _resolve_log_dir(config_path: Path) -> Path:
    """Use {package_dir}/logs; fall back to ~/logs if not writable."""
    candidate = config_path.parent / "logs"
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
        if key in _LOGRECORD_ATTRS or key.startswith("_"):
            continue
        result[key] = _redact_value(key, val) if isinstance(val, str) else val
    return result


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
        extra = _collect_extra(record)
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
