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

_LEVEL_MAP: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

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
    agent: bool,
    runnable_name: str,
    config_path: Path,
    log_level_override: str | None = None,
) -> None:
    """
    Configure root logger from normalised [config.logging].
    No-op when log_cfg is None. Idempotent — second call is silently ignored.

    In agent mode stderr is reserved for the MCP/SSH streaming side-channel,
    so no console handler is added; all output goes to the file.
    """
    global _configured
    if log_cfg is None or _configured:
        return

    effective_level_name = log_level_override or log_cfg["level"]
    effective_level = _LEVEL_MAP[effective_level_name]

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addFilter(_SensitiveFilter())

    # Console handler: only in non-agent mode
    if not agent:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(effective_level)
        ch.setFormatter(_HumanFormatter(show_tracebacks=(effective_level_name == "debug")))
        root.addHandler(ch)

    # File handler: always active, always DEBUG, always JSON
    log_dir = _resolve_log_dir(config_path)
    log_path = log_dir / f"{runnable_name}.log"
    fh = _make_file_handler(log_path, log_cfg["rotate"], log_cfg["keep"])
    fh.setLevel(logging.DEBUG)
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


class _HumanFormatter(logging.Formatter):
    """
    Minimal human-readable console formatter.
      INFO+: HH:MM:SS LEVEL    logger: message
      DEBUG: HH:MM:SS DEBUG    logger file.py:42: message
    Tracebacks shown only when show_tracebacks=True (level == debug).
    """

    _FMT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    _FMT_DBG = "%(asctime)s %(levelname)-8s %(name)s %(filename)s:%(lineno)d: %(message)s"
    _DATEFMT = "%H:%M:%S"

    def __init__(self, show_tracebacks: bool = False) -> None:
        super().__init__(datefmt=self._DATEFMT)
        self._show_tb = show_tracebacks

    def format(self, record: logging.LogRecord) -> str:
        fmt = self._FMT_DBG if record.levelno == logging.DEBUG else self._FMT
        self._style = logging.PercentStyle(fmt)
        self._fmt = fmt
        saved_exc, saved_text = record.exc_info, record.exc_text
        if not self._show_tb:
            record.exc_info = None
            record.exc_text = None
        try:
            line = super().format(record)
            extra = _collect_extra(record)
            if extra:
                line = f"{line}  {{{' '.join(f'{k}={v}' for k, v in extra.items())}}}"
            return line
        finally:
            record.exc_info, record.exc_text = saved_exc, saved_text
