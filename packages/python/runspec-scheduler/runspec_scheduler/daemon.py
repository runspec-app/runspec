"""daemon.py — the long-running scheduler process (``runspec-scheduler run``).

Loads the venv-scoped config, builds one APScheduler job per enabled schedule,
and execs the target runnable on each fire. Hot-reloads when the config file's
mtime changes (so ``add``/``remove`` — separate short-lived processes — take
effect without a restart). Keeps its own rotating audit log at
``{sys.prefix}/logs/scheduler.log`` recording each fire and its exit code.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import sys
import threading
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from .config import ScheduleEntry, load_schedules
from .discovery import resolve_target
from .runner import is_generic_venv_name, run_runnable, venv_name
from .timing import when_to_trigger

logger = logging.getLogger("runspec_scheduler.daemon")

_LOG_BACKUP_DAYS = 7
_OUTPUT_SNIPPET = 500  # chars of target output kept in the audit line


def _default_log_path() -> Path:
    return Path(sys.prefix) / "logs" / "scheduler.log"


def configure_logging(log_file: str | None = None, debug: bool = False) -> None:
    """Stderr + a rotating file handler (mirrors console-room's setup)."""
    level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    path = Path(log_file) if log_file else _default_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fileh = TimedRotatingFileHandler(path, when="midnight", backupCount=_LOG_BACKUP_DAYS)
        fileh.setFormatter(fmt)
        root.addHandler(fileh)
    except OSError as exc:  # pragma: no cover - unusual FS conditions
        logger.warning("could not open log file %s: %s", path, exc)


class Daemon:
    """Owns the APScheduler instance and the config hot-reload loop."""

    def __init__(self, config_path: Path, reload_interval: int = 5) -> None:
        self.config_path = config_path
        self.reload_interval = max(1, int(reload_interval))
        self._scheduler: Any = None
        self._mtime: float | None = None
        self._stop = threading.Event()

    # ── job wiring ────────────────────────────────────────────────────────
    def _rebuild(self) -> None:
        """(Re)load the config and sync APScheduler jobs to the enabled set."""
        from apscheduler.schedulers.background import BackgroundScheduler

        if self._scheduler is None:
            self._scheduler = BackgroundScheduler()
            self._scheduler.start()

        scheduler = self._scheduler
        try:
            entries = load_schedules(self.config_path)
        except Exception:
            logger.exception("could not load config %s — keeping current jobs", self.config_path)
            return

        scheduler.remove_all_jobs()
        count = 0
        for entry in entries:
            if not entry.enabled:
                continue
            try:
                scheduler.add_job(
                    self._fire,
                    when_to_trigger(entry.when),
                    args=[entry.id],
                    id=entry.id,
                    replace_existing=True,
                )
                count += 1
            except Exception:
                logger.exception("could not schedule job %r", entry.id)
        logger.info("loaded %d schedule(s) from %s", count, self.config_path)

    def _fire(self, schedule_id: str) -> None:
        """APScheduler callback: resolve the target and run it."""
        try:
            entries = load_schedules(self.config_path)
        except Exception:
            logger.exception("fire %r: could not read config", schedule_id)
            return
        entry = next((e for e in entries if e.id == schedule_id), None)
        if entry is None:
            return
        self.fire_entry(entry)

    def fire_entry(self, entry: ScheduleEntry) -> int | None:
        """Run one schedule's runnable now. Returns the exit code (or None)."""
        target = resolve_target(entry.runnable)
        if target is None:
            logger.error("schedule %r: runnable %r not found in this venv", entry.id, entry.runnable)
            return None
        run_as = entry.run_as or target.run_as
        try:
            result = run_runnable(
                target.binary,
                [],
                dict(entry.args),
                source_toml=target.source,
                run_as=run_as,
                become_method=entry.become_method or target.become_method,
                become_flags=target.become_flags,
                env_file=entry.env_file,
            )
        except Exception:
            logger.exception("schedule %r: run failed", entry.id)
            return None
        snippet = (result.stdout or result.stderr or "").strip()[:_OUTPUT_SNIPPET]
        logger.info(
            "schedule %r fired: /%s exit=%s%s",
            entry.id,
            entry.runnable,
            result.returncode,
            f" — {snippet}" if snippet else "",
        )
        return result.returncode

    # ── run loop ──────────────────────────────────────────────────────────
    def run(self) -> None:
        if is_generic_venv_name(venv_name()):
            logger.warning(
                "venv name %r is generic — the console identifies runnables by venv name; consider a descriptive venv.",
                venv_name(),
            )
        self._install_signal_handlers()
        self._rebuild()
        self._mtime = self._current_mtime()
        logger.info("runspec-scheduler daemon started (config: %s)", self.config_path)
        while not self._stop.is_set():
            self._stop.wait(self.reload_interval)
            if self._stop.is_set():
                break
            mtime = self._current_mtime()
            if mtime != self._mtime:
                logger.info("config changed — reloading")
                self._mtime = mtime
                self._rebuild()
        self._shutdown()

    def _current_mtime(self) -> float | None:
        try:
            return self.config_path.stat().st_mtime
        except OSError:
            return None

    def _install_signal_handlers(self) -> None:
        def _handle(signum: int, _frame: object) -> None:
            logger.info("received signal %s — shutting down", signum)
            self._stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            # ValueError if we're not on the main thread — best-effort.
            with contextlib.suppress(ValueError):  # pragma: no cover
                signal.signal(sig, _handle)

    def _shutdown(self) -> None:
        scheduler = self._scheduler
        self._scheduler = None
        if scheduler is not None:
            try:
                scheduler.shutdown(wait=False)
            except Exception:
                logger.exception("error shutting down scheduler")
        logger.info("runspec-scheduler daemon stopped")


def run_daemon(config_path: Path, reload_interval: int = 5) -> None:
    """Entry point used by the ``run`` verb."""
    Daemon(config_path, reload_interval).run()
