"""daemon.py — the long-running scheduler process (``runspec-scheduler run``).

One daemon per venv, running as the venv owner, firing the venv's schedules when
``[scheduler] backend = "daemon"`` (design: ``docs/design/venv-scheduler.md``).
It is a plain foreground process — it never forks or restarts itself; whatever
keeps it alive (systemd, supervisord, a cron watchdog) only starts
``{venv}/bin/runspec-scheduler run`` as the owner.

What it guarantees, and how:

* **Every fire is recorded.** A fire runs the same ``runspec exec --label
  sched:<id> --trigger schedule --lock <lock> [--timeout N] <runnable> -- …``
  command a cron line runs, so its exit code and output land in the runs index.
  A fire the daemon missed (it woke past the misfire grace) or couldn't start is
  recorded as a ``skipped`` / ``failed`` run by :mod:`records`.
* **Fires are independent processes.** Each starts in its own session with no
  pipes back to the daemon, so a hung or chatty runnable can't block the
  scheduler, a stop doesn't wait for running fires, and a fire survives a daemon
  restart (``runspec exec`` still records its end). The APScheduler job only
  spawns the process; the main loop reaps it.
* **A timeout kills the whole run.** ``runspec exec --timeout`` stops the run
  (default 6h, ``0`` = none) and records it as timed out; the daemon then kills
  whatever the run left in its process group. A backstop kills the group if
  ``exec`` itself hasn't ended two minutes after that.
* **One copy per venv.** An exclusive ``flock`` on ``runspec_scheduler.lock``
  next to the config; a second ``run`` prints "already running" and exits 0, so
  a watchdog that starts it every few minutes is safe.
* **It says it's alive.** ``logs/scheduler_state.json`` is rewritten every
  interval (pid, heartbeat, jobs and their next fire, fires in flight, the last
  config error) and read by ``runspec-scheduler status``.
* **Reloads are surgical.** Config changes (mtime, size or inode; ``SIGHUP``
  reloads at once) add, remove or reschedule only the schedules whose timing
  changed — an ``every 6h`` job keeps its next fire through unrelated edits. A
  config that breaks keeps the last good jobs; one that is broken at start is a
  fatal error (exit 1) so the keeper's restart policy applies.

APScheduler's defaults suit a web app, not a scheduler that must not lose fires,
so they're set explicitly: ``coalesce`` on, a 5-minute misfire grace (the
``catch_up_window`` for ``catch_up`` schedules), and jobs that return at once.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import IO, Any

from .config import ScheduleEntry, SchedulerSettings, load_config
from .cronsync import LABEL_PREFIX, _home_of, exec_invocation, resolve_entry
from .records import audit, record_run
from .runner import is_generic_venv_name, scripts_dir, venv_name
from .timing import when_to_trigger

logger = logging.getLogger("runspec_scheduler.daemon")

_LOG_BACKUP_DAYS = 7

#: ``runspec exec``'s exit code for a run it stopped at its timeout.
EXIT_TIMEOUT = 124
#: Seconds after the timeout before the daemon kills a run ``exec`` didn't end.
BACKSTOP_SECONDS = 120
#: Seconds between the backstop's SIGTERM and SIGKILL.
KILL_GRACE_SECONDS = 10

STATE_NAME = "scheduler_state.json"
LOCK_NAME = "runspec_scheduler.lock"

#: Inherited variables a fire must not see (they belong to the daemon's own run
#: or would point the runnable at the wrong config).
_DROP_ENV = (
    "RUNSPEC_RUN_ID",
    "RUNSPEC_RUN_LOG",
    "RUNSPEC_EXEC_UID",
    "RUNSPEC_LABEL",
    "RUNSPEC_TRIGGER",
    "RUNSPEC_CONFIG",
    "RUNSPEC_ENV_FILE",
    "RUNSPEC_RUNBOOK",
    "RUNSPEC_SCHEDULER_CONFIG",
)

#: The PATH a login shell has; keepers (cron especially) give far less.
_STANDARD_PATH = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")


def _default_log_path() -> Path:
    return Path(sys.prefix) / "logs" / "scheduler.log"


def configure_logging(log_file: str | None = None, debug: bool = False) -> None:
    """Stderr + a rotating ``scheduler.log``, replacing any root handlers already
    installed (``rs.parse()`` sets up its own, which would print every line
    twice)."""
    level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        with contextlib.suppress(Exception):
            h.close()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    stream = logging.StreamHandler(sys.__stderr__)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    path = Path(log_file) if log_file else _default_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fileh = TimedRotatingFileHandler(path, when="midnight", backupCount=_LOG_BACKUP_DAYS, encoding="utf-8")
        fileh.setFormatter(fmt)
        root.addHandler(fileh)
    except OSError as exc:  # pragma: no cover - unusual FS conditions
        logger.warning("could not open log file %s: %s", path, exc)
    # APScheduler logs every job start at INFO; its warnings and errors stay.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def ensure_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """Give fires the environment a login shell would: the standard sbin/bin
    directories on ``PATH`` and a UTF-8 ``LANG`` when no locale is set. Keepers
    start us with a thin environment (cron: ``PATH=/usr/bin:/bin``), and a
    runnable that calls ``/usr/sbin/ip`` would work by hand and fail on a
    schedule. Updates and returns ``env`` (default ``os.environ``)."""
    target: Any = os.environ if env is None else env
    parts = [p for p in (target.get("PATH") or "").split(os.pathsep) if p]
    for d in _STANDARD_PATH:
        if d not in parts:
            parts.append(d)
    target["PATH"] = os.pathsep.join(parts)
    if not target.get("LANG") and not target.get("LC_ALL"):
        target["LANG"] = "C.UTF-8"
    return dict(target)


def _now() -> datetime.datetime:
    return datetime.datetime.now().astimezone()


def installed_version() -> str:
    """The installed runspec-scheduler version ("" when it can't be read). The
    daemon records the one it started with; ``status`` compares the two to say
    a restart is needed after an upgrade."""
    try:
        from importlib.metadata import version

        return version("runspec-scheduler")
    except Exception:  # noqa: BLE001
        return ""


def _iso(dt: datetime.datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _signature(path: Path) -> tuple[int, int, int] | None:
    """What a config change looks like: mtime (ns), size and inode. Two edits in
    one mtime tick, or a file restored with its old mtime, still differ in size
    or inode (atomic writes make a new inode)."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _killpg(pgid: int, sig: int) -> bool:
    """Signal a fire's whole process group. False when nothing was there."""
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


@dataclass
class Fire:
    """One fire in flight: a ``runspec exec`` process in its own session."""

    sched_id: str
    runnable: str
    proc: subprocess.Popen[bytes]
    started: datetime.datetime
    started_mono: float
    timeout: int
    deadline: float | None = None  # monotonic backstop
    killed_at: float | None = None  # when the backstop sent SIGTERM

    @property
    def pid(self) -> int:
        return self.proc.pid


@dataclass
class _Missed:
    count: int = 0
    first: datetime.datetime | None = None
    last: datetime.datetime | None = None
    reasons: set[str] = field(default_factory=set)


def state_path_for(config_path: Path) -> Path:
    """The heartbeat file for the daemon reading ``config_path``."""
    return config_path.parent / "logs" / STATE_NAME


def lock_path_for(config_path: Path) -> Path:
    """The single-instance lock for the daemon reading ``config_path``."""
    return config_path.with_name(LOCK_NAME)


def read_state(config_path: Path) -> dict[str, Any] | None:
    """The last heartbeat the daemon wrote, or ``None``."""
    try:
        data = json.loads(state_path_for(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class Daemon:
    """Owns the APScheduler instance, the fires in flight and the main loop."""

    def __init__(
        self,
        config_path: Path,
        reload_interval: int = 5,
        *,
        runspec_bin: str | None = None,
        tick: float = 1.0,
        backstop: float = BACKSTOP_SECONDS,
        kill_grace: float = KILL_GRACE_SECONDS,
    ) -> None:
        self.config_path = config_path
        self.interval = max(1, int(reload_interval))  # heartbeat interval
        self.tick = max(0.05, float(tick))
        self.backstop = backstop
        self.kill_grace = kill_grace
        self.runspec_bin = runspec_bin or str(scripts_dir() / "runspec")
        self.home = _home_of("")
        self.lock_path = lock_path_for(config_path)
        self.state_path = state_path_for(config_path)
        self.started = _now()
        self.code_version = installed_version()

        self._scheduler: Any = None
        self._settings = SchedulerSettings()
        self._entries: dict[str, ScheduleEntry] = {}
        self._job_keys: dict[str, str] = {}
        self._sig: tuple[int, int, int] | None = None
        self._config_error: str | None = None
        self._last_reload: datetime.datetime | None = None
        self._lock_fh: IO[str] | None = None
        self._lock = threading.RLock()
        self._running: dict[int, Fire] = {}
        self._missed: dict[str, _Missed] = {}
        self._triggers: dict[str, Any] = {}
        self._last_sched: dict[str, datetime.datetime] = {}
        self._last_fire: dict[str, str] = {}
        self._fires = 0
        self._stop = threading.Event()
        self._reload = threading.Event()
        self._last_heartbeat = 0.0
        self._state_warned = False

    # ── single instance ───────────────────────────────────────────────────
    def acquire_lock(self) -> int | None:
        """Take the venv's scheduler lock. ``None`` on success, else the pid
        holding it (``0`` when unknown). Raises ``OSError`` if the lock file
        can't be created at all."""
        fh, holder = take_lock(self.lock_path)
        self._lock_fh = fh
        return holder

    def release_lock(self) -> None:
        fh = self._lock_fh
        self._lock_fh = None
        if fh is not None:
            with contextlib.suppress(OSError):
                fh.close()

    # ── config + jobs ─────────────────────────────────────────────────────
    def load(self, *, initial: bool = False) -> None:
        """(Re)load the config and bring the jobs in line with it.

        At start a config that won't parse raises (the caller exits non-zero); on
        a later reload it is logged, reported in the heartbeat, and the last good
        jobs keep firing."""
        sig = _signature(self.config_path)  # before the read: an edit during it reloads again
        try:
            settings, entries = load_config(self.config_path)
        except Exception as exc:
            self._sig = sig
            if initial:
                raise
            self._config_error = f"{type(exc).__name__}: {exc}"
            logger.error("config %s is invalid — keeping the last good schedules: %s", self.config_path, exc)
            return
        self._sig = sig
        self._config_error = None
        self._last_reload = _now()
        with self._lock:
            self._settings = settings
            self._entries = {e.id: e for e in entries}
        self._sync_jobs(initial=initial)

    def _scheduler_or_start(self) -> Any:
        if self._scheduler is None:
            from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED, EVENT_JOB_SUBMITTED
            from apscheduler.schedulers.background import BackgroundScheduler

            sched = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": self._settings.misfire_grace})
            sched.add_listener(self._on_scheduled, EVENT_JOB_SUBMITTED | EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES)
            sched.add_listener(self._on_error, EVENT_JOB_ERROR)
            sched.start()
            self._scheduler = sched
        return self._scheduler

    def _job_key(self, entry: ScheduleEntry, settings: SchedulerSettings) -> str:
        """Only what changes *when* a schedule fires: an edit to its args or
        timeout must not reset an ``every`` timer."""
        when = entry.to_dict()["when"]
        return json.dumps({"when": when, "catch_up": entry.catch_up, "grace": settings.misfire_grace, "window": settings.catch_up_window}, sort_keys=True)

    def _sync_jobs(self, *, initial: bool = False) -> None:
        sched = self._scheduler_or_start()
        with self._lock:
            settings = self._settings
            wanted = {sid: e for sid, e in self._entries.items() if e.enabled} if settings.backend == "daemon" else {}
        added = changed = removed = 0
        for sid in list(self._job_keys):
            if sid not in wanted:
                with contextlib.suppress(Exception):
                    sched.remove_job(sid)
                del self._job_keys[sid]
                self._forget(sid)
                removed += 1
        for sid, entry in wanted.items():
            key = self._job_key(entry, settings)
            if self._job_keys.get(sid) == key:
                continue
            is_new = sid not in self._job_keys
            try:
                trigger = when_to_trigger(entry.when)
            except Exception as exc:  # noqa: BLE001 — one bad schedule never stops the rest
                logger.error("schedule %r: invalid timing — not scheduled: %s", sid, exc)
                with contextlib.suppress(Exception):
                    sched.remove_job(sid)
                self._job_keys.pop(sid, None)
                self._forget(sid)
                continue
            kwargs: dict[str, Any] = {
                "id": sid,
                "args": [sid],
                "replace_existing": True,
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": max(settings.misfire_grace, settings.catch_up_window) if entry.catch_up else settings.misfire_grace,
            }
            if initial and entry.catch_up and self._missed_while_down(entry, trigger, settings):
                kwargs["next_run_time"] = _now()  # one coalesced catch-up fire now
                logger.info("schedule %r: fires were missed while the scheduler was down — catching up once", sid)
            with self._lock:
                self._triggers[sid] = trigger
                self._last_sched.pop(sid, None)
            sched.add_job(self._fire, trigger, **kwargs)
            self._job_keys[sid] = key
            if is_new:
                added += 1
            else:
                changed += 1
        if settings.backend != "daemon":
            logger.info("backend is %r — the daemon fires nothing (switch with: runspec-scheduler use daemon)", settings.backend)
        elif added or changed or removed or initial:
            logger.info("schedules: %d active (%d added, %d rescheduled, %d removed)", len(self._job_keys), added, changed, removed)

    def _missed_while_down(self, entry: ScheduleEntry, trigger: Any, settings: SchedulerSettings) -> bool:
        """Whether ``entry`` had a fire due since its last recorded fire that is
        still inside the catch-up window. A schedule that never fired doesn't
        catch up (a new schedule must not fire at once)."""
        last = self._last_known_fire(entry.id)
        if last is None:
            return False
        now = _now()
        floor = now - datetime.timedelta(seconds=settings.catch_up_window)
        latest = None
        t = trigger.get_next_fire_time(None, last + datetime.timedelta(seconds=1))
        for _ in range(100_000):  # every 1s for a day fits; never loop forever
            if t is None or t > now:
                break
            latest = t
            t = trigger.get_next_fire_time(t, t + datetime.timedelta(seconds=1))
        return latest is not None and latest >= floor

    def _last_known_fire(self, sched_id: str) -> datetime.datetime | None:
        """The newest fire we know of: our own heartbeat, else the runs index."""
        stamps: list[datetime.datetime] = []
        raw = self._last_fire.get(sched_id)
        if raw:
            with contextlib.suppress(ValueError):
                stamps.append(datetime.datetime.fromisoformat(raw))
        with contextlib.suppress(Exception):
            from .status import _runs_for

            rows = _runs_for(f"{LABEL_PREFIX}{sched_id}", 1)
            if rows and rows[0].get("start_ts"):
                stamps.append(datetime.datetime.fromisoformat(str(rows[0]["start_ts"])))
        stamps = [s if s.tzinfo else s.astimezone() for s in stamps]
        return max(stamps) if stamps else None

    # ── APScheduler events ────────────────────────────────────────────────
    def _forget(self, sid: str) -> None:
        with self._lock:
            self._triggers.pop(sid, None)
            self._last_sched.pop(sid, None)

    def _on_scheduled(self, event: Any) -> None:
        """Every fire APScheduler processes (run, missed or refused as busy).

        With ``coalesce`` on, APScheduler silently drops all but the latest of
        several due fire times (a pause, a clock step). Each event therefore also
        counts the trigger's fire times between the previous one we saw and this
        one: those were dropped, and are recorded as missed."""
        from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED

        times = list(getattr(event, "scheduled_run_times", None) or [])
        one = getattr(event, "scheduled_run_time", None)
        if one is not None:
            times.append(one)
        if not times:
            return
        latest = max(times)
        with self._lock:
            prev = self._last_sched.get(event.job_id)
            self._last_sched[event.job_id] = latest
            trigger = self._triggers.get(event.job_id)
        dropped: list[datetime.datetime] = []
        if prev is not None and trigger is not None and latest > prev:
            with contextlib.suppress(Exception):
                t = trigger.get_next_fire_time(prev, prev + datetime.timedelta(microseconds=1))
                for _ in range(100_000):
                    if t is None or t >= latest:
                        break
                    dropped.append(t)
                    t = trigger.get_next_fire_time(t, t + datetime.timedelta(microseconds=1))
        if event.code == EVENT_JOB_MISSED:
            dropped.append(latest)
            reason = "missed"
        elif event.code == EVENT_JOB_MAX_INSTANCES:
            dropped.append(latest)
            reason = "busy"
        else:
            reason = "missed"
        if not dropped:
            return
        with self._lock:
            m = self._missed.setdefault(event.job_id, _Missed())
            m.count += len(dropped)
            lo, hi = min(dropped), max(dropped)
            m.first = lo if m.first is None else min(m.first, lo)
            m.last = hi if m.last is None else max(m.last, hi)
            m.reasons.add(reason)

    def _on_error(self, event: Any) -> None:
        logger.error("schedule %r: unexpected error in the fire callback: %s", event.job_id, event.exception)

    def _flush_missed(self) -> None:
        """One ``skipped`` run per schedule for the fires missed since the last
        tick (a long pause with an ``every 10s`` schedule is one row, not 360)."""
        with self._lock:
            pending, self._missed = self._missed, {}
            entries = dict(self._entries)
            grace = self._settings.misfire_grace
        for sid, m in pending.items():
            entry = entries.get(sid)
            runnable = entry.runnable if entry else sid
            n = "1 fire" if m.count == 1 else f"{m.count} fires"
            if "busy" in m.reasons:
                msg = f"skipped {n} of '{runnable}' — the scheduler was still starting the previous one"
            else:
                span = _iso(m.first) if m.first == m.last else f"{_iso(m.first)} to {_iso(m.last)}"
                msg = f"missed {n} of '{runnable}' due {span} — the scheduler was paused or late (host suspended, clock step, or overloaded; grace {grace}s)"
            logger.warning("schedule %r: %s", sid, msg)
            record_run(sid, runnable, status="skipped", exit_code=0, reason="missed", message=msg, when=m.last)
            audit("scheduler.fire_missed", "skipped", target={"schedule": sid, "runnable": runnable}, detail={"count": m.count, "first": _iso(m.first), "last": _iso(m.last)})

    # ── firing ────────────────────────────────────────────────────────────
    def _fire(self, sched_id: str) -> None:
        """APScheduler callback: start the fire and return at once."""
        entry: ScheduleEntry | None = None
        try:
            with self._lock:
                entry = self._entries.get(sched_id)
                settings = self._settings
            if entry is None or settings.backend != "daemon":
                return
            self.fire_entry(entry, settings)
        except Exception as exc:  # noqa: BLE001 — every failure leaves a record
            logger.exception("schedule %r: could not start the fire", sched_id)
            runnable = entry.runnable if entry else sched_id
            record_run(sched_id, runnable, status="failed", exit_code=70, reason="scheduler_error", message=f"the scheduler could not start '{runnable}': {exc}")
            audit("scheduler.fire_failed", "failed", target={"schedule": sched_id, "runnable": runnable}, detail={"error": str(exc)})

    def fire_entry(self, entry: ScheduleEntry, settings: SchedulerSettings | None = None) -> Fire | None:
        """Start one schedule's fire now (``runspec exec`` in its own session).
        Returns the :class:`Fire`, or ``None`` when it couldn't start (recorded)."""
        settings = settings or self._settings
        target, reason = resolve_entry(entry)
        if target is None:
            code = 127 if "not installed" in reason else 2
            msg = f"schedule '{entry.id}' can't run: {reason}"
            logger.error(msg)
            record_run(entry.id, entry.runnable, status="failed", exit_code=code, reason="not_installed" if code == 127 else "invalid", message=msg)
            audit("scheduler.fire_failed", "failed", target={"schedule": entry.id, "runnable": entry.runnable}, detail={"error": reason})
            return None
        timeout = entry.effective_timeout(settings)
        env, argv = exec_invocation(
            self.runspec_bin,
            entry.runnable,
            dict(entry.args),
            entry.env_file,
            home=self.home,
            sched_id=entry.id,
            command=entry.command,
            spec=target.spec,
            timeout=timeout or None,
        )
        child_env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
        child_env.update(env)
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,  # runspec exec captures the output into the run's log
                stderr=subprocess.DEVNULL,
                env=child_env,
                cwd=self.home if self.home and os.path.isdir(self.home) else None,
                start_new_session=True,  # survives a daemon stop; its group is killable as one
                close_fds=True,
            )
        except OSError as exc:
            msg = f"could not start {argv[0]}: {exc}"
            logger.error("schedule %r: %s", entry.id, msg)
            record_run(entry.id, entry.runnable, status="failed", exit_code=126, reason="launch_error", message=msg)
            audit("scheduler.fire_failed", "failed", target={"schedule": entry.id, "runnable": entry.runnable}, detail={"error": msg})
            return None
        now_mono = time.monotonic()
        fire = Fire(
            sched_id=entry.id,
            runnable=entry.runnable,
            proc=proc,
            started=_now(),
            started_mono=now_mono,
            timeout=timeout,
            deadline=(now_mono + timeout + self.backstop) if timeout else None,
        )
        with self._lock:
            self._running[proc.pid] = fire
            self._last_fire[entry.id] = _iso(fire.started) or ""
            self._fires += 1
        logger.info("schedule %r fired: %s (pid %d)", entry.id, entry.runnable, proc.pid)
        return fire

    def reap(self) -> None:
        """Collect finished fires and enforce the backstop (main thread)."""
        with self._lock:
            fires = list(self._running.values())
        now = time.monotonic()
        for fire in fires:
            rc = fire.proc.poll()
            if rc is None:
                if fire.deadline is not None and now >= fire.deadline:
                    if fire.killed_at is None:
                        logger.error("schedule %r: run still going %ds past its %ds timeout — killing its process group", fire.sched_id, int(self.backstop), fire.timeout)
                        _killpg(fire.pid, signal.SIGTERM)
                        fire.killed_at = now
                        audit("scheduler.fire_killed", "killed", target={"schedule": fire.sched_id, "runnable": fire.runnable}, detail={"timeout_s": fire.timeout, "pid": fire.pid})
                    elif now - fire.killed_at >= self.kill_grace:
                        _killpg(fire.pid, signal.SIGKILL)
                continue
            with self._lock:
                self._running.pop(fire.pid, None)
            took = time.monotonic() - fire.started_mono
            if rc == EXIT_TIMEOUT and fire.timeout:
                # exec stopped the runnable; anything it left behind in the run's
                # process group (background children) goes too.
                left = _killpg(fire.pid, signal.SIGKILL)
                logger.warning("schedule %r: timed out after %ds%s", fire.sched_id, fire.timeout, " — killed what it left running" if left else "")
            elif rc < 0:
                logger.warning("schedule %r: runspec exec ended by signal %d after %.1fs", fire.sched_id, -rc, took)
            else:
                logger.info("schedule %r finished: exit %d", fire.sched_id, rc)

    # ── heartbeat ─────────────────────────────────────────────────────────
    def state(self) -> dict[str, Any]:
        jobs: dict[str, str | None] = {}
        sched = self._scheduler
        if sched is not None:
            with contextlib.suppress(Exception):
                for job in sched.get_jobs():
                    jobs[job.id] = _iso(getattr(job, "next_run_time", None))
        with self._lock:
            running = [{"id": f.sched_id, "pid": f.pid, "started": _iso(f.started)} for f in self._running.values()]
            settings = self._settings
            last_fire = dict(self._last_fire)
        return {
            "version": 1,
            "pid": os.getpid(),
            "venv": sys.prefix,
            "config": str(self.config_path),
            "started": _iso(self.started),
            "scheduler_version": self.code_version,
            "heartbeat": _iso(_now()),
            "interval_s": self.interval,
            "backend": settings.backend,
            "firing": settings.backend == "daemon",
            "config_error": self._config_error,
            "last_reload": _iso(self._last_reload),
            "jobs": jobs,
            "running": running,
            "last_fire": last_fire,
            "fires": self._fires,
        }

    def write_state(self, extra: dict[str, Any] | None = None) -> None:
        payload = {**self.state(), **(extra or {})}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(f".{STATE_NAME}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            os.replace(tmp, self.state_path)
            self._state_warned = False
        except OSError as exc:
            if not self._state_warned:
                logger.warning("could not write heartbeat %s: %s", self.state_path, exc)
                self._state_warned = True

    def _restore_last_fires(self) -> None:
        prev = read_state(self.config_path) or {}
        last = prev.get("last_fire")
        if isinstance(last, dict):
            self._last_fire = {str(k): str(v) for k, v in last.items() if v}

    # ── run loop ──────────────────────────────────────────────────────────
    def run(self) -> int:
        """Run until SIGTERM/SIGINT. Returns the process exit code."""
        try:
            holder = None if self._lock_fh is not None else self.acquire_lock()
        except OSError as exc:
            logger.error("cannot create the scheduler lock %s: %s", self.lock_path, exc)
            return 1
        if holder is not None:
            who = f" (pid {holder})" if holder else ""
            print(f"runspec-scheduler: already running for this venv{who}", flush=True)  # noqa: T201
            return 0
        try:
            return self._run_locked()
        finally:
            self.release_lock()

    def _run_locked(self) -> int:
        if is_generic_venv_name(venv_name()):
            logger.warning("venv name %r is generic — the console identifies runnables by venv name; consider a descriptive venv.", venv_name())
        ensure_environment()
        self._install_signal_handlers()
        self._restore_last_fires()
        try:
            self.load(initial=True)
        except Exception as exc:  # noqa: BLE001 — fatal at start, so the keeper restarts us
            logger.error("cannot start: config %s is invalid: %s", self.config_path, exc)
            self.write_state({"stopped": _iso(_now()), "config_error": f"{type(exc).__name__}: {exc}", "firing": False})
            audit("scheduler.daemon", "failed", target={"venv": sys.prefix}, detail={"error": str(exc)})
            self._shutdown_scheduler()
            return 1
        logger.info("runspec-scheduler daemon started (pid %d, config %s, backend %s)", os.getpid(), self.config_path, self._settings.backend)
        audit("scheduler.daemon", "started", target={"venv": sys.prefix}, detail={"pid": os.getpid(), "backend": self._settings.backend, "schedules": len(self._job_keys)})
        self.write_state()
        self._last_heartbeat = time.monotonic()
        code = 0
        try:
            while not self._stop.is_set():
                self._stop.wait(self.tick)
                if self._stop.is_set():
                    break
                if self._reload.is_set():
                    self._reload.clear()
                    logger.info("SIGHUP — reloading")
                    self.load()
                elif _signature(self.config_path) != self._sig:
                    logger.info("config changed — reloading")
                    self.load()
                self._flush_missed()
                self.reap()
                if time.monotonic() - self._last_heartbeat >= self.interval:
                    self.write_state()
                    self._last_heartbeat = time.monotonic()
        except Exception:  # noqa: BLE001 — log it and exit non-zero so the keeper restarts us
            logger.exception("scheduler loop failed")
            code = 1
        finally:
            self._shutdown(code)
        return code

    def _install_signal_handlers(self) -> None:
        def _stop(signum: int, _frame: object) -> None:
            logger.info("received signal %s — stopping", signum)
            self._stop.set()

        def _hup(_signum: int, _frame: object) -> None:
            self._reload.set()

        for sig, handler in ((signal.SIGTERM, _stop), (signal.SIGINT, _stop), (signal.SIGHUP, _hup)):
            with contextlib.suppress(ValueError):  # not on the main thread (tests)
                signal.signal(sig, handler)

    def _shutdown_scheduler(self) -> None:
        sched = self._scheduler
        self._scheduler = None
        if sched is not None:
            try:
                sched.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                logger.exception("error shutting down the scheduler")

    def _shutdown(self, code: int = 0) -> None:
        self._shutdown_scheduler()
        with contextlib.suppress(Exception):
            self._flush_missed()
        self.reap()
        with self._lock:
            left = len(self._running)
        if left:
            logger.info("%d fire(s) still running — they finish on their own and runspec exec records them", left)
        self.write_state({"stopped": _iso(_now()), "firing": False})
        audit("scheduler.daemon", "stopped" if code == 0 else "failed", target={"venv": sys.prefix}, detail={"pid": os.getpid(), "fires": self._fires, "running": left})
        logger.info("runspec-scheduler daemon stopped")

    def stop(self) -> None:
        self._stop.set()


def run_daemon(config_path: Path, reload_interval: int = 5, *, lock_fh: IO[str] | None = None) -> int:
    """Entry point used by the ``run`` verb. Returns the exit code. ``lock_fh`` is
    the venv lock when the caller already took it (see ``cli.main``)."""
    daemon = Daemon(config_path, reload_interval)
    daemon._lock_fh = lock_fh
    return daemon.run()


def take_lock(path: Path) -> tuple[IO[str] | None, int | None]:
    """Take the exclusive single-instance lock at ``path``. ``(handle, None)``
    when we hold it (closing the handle releases it), ``(None, pid)`` when
    another process does (pid ``0`` when unknown). Raises ``OSError`` when the
    file can't be created."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+", encoding="utf-8")  # noqa: SIM115 — held for the daemon's life
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        text = fh.read().strip()
        fh.close()
        try:
            return None, int(text.split()[0])
        except (ValueError, IndexError):
            return None, 0
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return fh, None
