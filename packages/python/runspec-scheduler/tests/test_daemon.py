"""Daemon unit tests: job diffing, config errors, missed fires, catch-up, the
fire command, the backstop, the lock and the heartbeat. The process-level
failure tests (signals, hangs, huge output, …) are in test_daemon_process.py."""

from __future__ import annotations

import datetime
import json
import os
import signal
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from runspec_scheduler import cronsync, daemon, discovery, status
from runspec_scheduler.config import ScheduleEntry, SchedulerSettings, write_schedules
from runspec_scheduler.discovery import Target
from runspec_scheduler.timing import When, when_to_trigger

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon")


def _target(name: str = "backup") -> Target:
    return Target(runnable=name, source="/pkg/runspec.toml", binary=Path(f"/venv/bin/{name}"), run_as="", become_method="sudo", become_flags=None, spec={"args": {"level": {}}, "commands": {}})


@pytest.fixture
def recorded(monkeypatch):
    """Capture record_run / audit calls instead of writing them."""
    calls: dict[str, list] = {"runs": [], "audit": []}
    monkeypatch.setattr(daemon, "record_run", lambda *a, **k: calls["runs"].append((a, k)))
    monkeypatch.setattr(daemon, "audit", lambda *a, **k: calls["audit"].append((a, k)))
    return calls


def _cfg(path: Path, entries, backend: str = "daemon", **settings) -> Path:
    write_schedules(path, entries, scheduler={"backend": backend, **settings})
    return path


def _fake_runspec(tmp_path: Path, body: str = "") -> Path:
    """A stand-in for {venv}/bin/runspec that records its argv and pgid."""
    script = tmp_path / "fake-runspec"
    out = tmp_path / "fake-runspec.out"
    script.write_text(
        textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, sys, time
        with open({str(out)!r}, "a") as f:
            f.write(json.dumps({{"argv": sys.argv[1:], "pgid": os.getpgid(0), "pid": os.getpid(), "env": {{k: v for k, v in os.environ.items() if k.startswith("RUNSPEC_")}}}}) + "\\n")
        """)
        + textwrap.dedent(body)
    )
    script.chmod(0o755)
    return script


def _calls(tmp_path: Path) -> list[dict]:
    out = tmp_path / "fake-runspec.out"
    return [json.loads(line) for line in out.read_text().splitlines()] if out.exists() else []


class TestEnvironment:
    def test_thin_cron_environment_is_filled_in(self):
        env = daemon.ensure_environment({"PATH": "/usr/bin:/bin"})
        parts = env["PATH"].split(os.pathsep)
        assert parts[:2] == ["/usr/bin", "/bin"]  # existing order kept
        assert "/usr/sbin" in parts and "/usr/local/bin" in parts and "/sbin" in parts
        assert env["LANG"] == "C.UTF-8"

    def test_existing_locale_is_kept(self):
        assert daemon.ensure_environment({"PATH": "", "LC_ALL": "en_GB.UTF-8"}).get("LANG") is None
        assert daemon.ensure_environment({"LANG": "de_DE.UTF-8"})["LANG"] == "de_DE.UTF-8"


class TestJobs:
    def test_only_enabled_daemon_schedules_get_jobs(self, tmp_path):
        cfg = _cfg(tmp_path / "c.toml", [ScheduleEntry(id="on", when=When(every="15m"), runnable="a"), ScheduleEntry(id="off", when=When(every="15m"), runnable="b", enabled=False)])
        d = daemon.Daemon(cfg)
        try:
            d.load(initial=True)
            assert {j.id for j in d._scheduler.get_jobs()} == {"on"}
        finally:
            d._shutdown_scheduler()

    def test_cron_backend_schedules_nothing(self, tmp_path):
        cfg = _cfg(tmp_path / "c.toml", [ScheduleEntry(id="on", when=When(cron="* * * * *"), runnable="a")], backend="cron")
        d = daemon.Daemon(cfg)
        try:
            d.load(initial=True)
            assert d._scheduler.get_jobs() == []
            assert d.state()["firing"] is False
        finally:
            d._shutdown_scheduler()

    def test_edit_that_doesnt_change_timing_keeps_the_next_fire(self, tmp_path):
        """Review #3: an args edit must not restart an `every` timer."""
        cfg = tmp_path / "c.toml"
        _cfg(cfg, [ScheduleEntry(id="six", when=When(every="6h"), runnable="a", args={"level": "1"}), ScheduleEntry(id="gone", when=When(every="1h"), runnable="a")])
        d = daemon.Daemon(cfg)
        try:
            d.load(initial=True)
            before = d._scheduler.get_job("six").next_run_time
            time.sleep(0.05)
            _cfg(cfg, [ScheduleEntry(id="six", when=When(every="6h"), runnable="a", args={"level": "2"}, timeout=60), ScheduleEntry(id="new", when=When(every="1h"), runnable="a")])
            d.load()
            assert d._scheduler.get_job("six").next_run_time == before
            assert d._entries["six"].args == {"level": "2"}  # the next fire uses the new args
            assert {j.id for j in d._scheduler.get_jobs()} == {"six", "new"}
            # a timing change does reschedule
            _cfg(cfg, [ScheduleEntry(id="six", when=When(every="5h"), runnable="a")])
            d.load()
            assert d._scheduler.get_job("six").next_run_time != before
        finally:
            d._shutdown_scheduler()

    def test_invalid_config_at_start_raises(self, tmp_path):
        """Review #9: no daemon running with zero jobs."""
        cfg = tmp_path / "c.toml"
        cfg.write_text("[[schedule]]\nid = 'x'\nrunnable = 'a'\nwhen = { every = 'soon' }\n")
        d = daemon.Daemon(cfg)
        try:
            with pytest.raises(ValueError):
                d.load(initial=True)
        finally:
            d._shutdown_scheduler()

    def test_invalid_config_later_keeps_last_good_jobs(self, tmp_path):
        cfg = _cfg(tmp_path / "c.toml", [ScheduleEntry(id="a", when=When(every="1h"), runnable="a")])
        d = daemon.Daemon(cfg)
        try:
            d.load(initial=True)
            cfg.write_text("[[schedule]\nbroken")
            d.load()
            assert [j.id for j in d._scheduler.get_jobs()] == ["a"]
            assert d._config_error
            assert d.state()["config_error"]
            _cfg(cfg, [ScheduleEntry(id="a", when=When(every="1h"), runnable="a")])
            d.load()
            assert d._config_error is None
        finally:
            d._shutdown_scheduler()

    def test_duplicate_ids_are_a_config_error(self, tmp_path):
        cfg = tmp_path / "c.toml"
        cfg.write_text("[[schedule]]\nid='a'\nrunnable='x'\nwhen={every='1h'}\n\n[[schedule]]\nid='a'\nrunnable='y'\nwhen={every='2h'}\n")
        with pytest.raises(ValueError, match="more than once"):
            daemon.Daemon(cfg).load(initial=True)

    def test_signature_sees_same_mtime_edits(self, tmp_path):
        """Review #11: an edit inside one mtime tick still reads as a change."""
        p = tmp_path / "c.toml"
        p.write_text("a")
        st = p.stat()
        sig = daemon._signature(p)
        tmp = tmp_path / "t"
        tmp.write_text("ab")
        os.replace(tmp, p)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
        assert daemon._signature(p) != sig


class TestMissed:
    def test_missed_fires_collapse_into_one_skipped_run(self, tmp_path, recorded):
        """Review #1: a late fire is recorded, once per schedule per tick."""
        from apscheduler.events import EVENT_JOB_MISSED

        cfg = _cfg(tmp_path / "c.toml", [ScheduleEntry(id="tick", when=When(every="10s"), runnable="demo")])
        d = daemon.Daemon(cfg)
        d.load(initial=True)
        d._shutdown_scheduler()
        t0 = datetime.datetime(2026, 11, 7, 6, 0, tzinfo=datetime.timezone.utc)
        for i in range(360):
            d._on_scheduled(SimpleNamespace(code=EVENT_JOB_MISSED, job_id="tick", scheduled_run_time=t0 + datetime.timedelta(seconds=10 * i)))
        d._flush_missed()
        assert len(recorded["runs"]) == 1
        (args, kw) = recorded["runs"][0]
        assert args == ("tick", "demo") and kw["status"] == "skipped" and kw["reason"] == "missed"
        assert "360 fires" in kw["message"]
        assert recorded["audit"][0][0] == ("scheduler.fire_missed", "skipped")
        d._flush_missed()
        assert len(recorded["runs"]) == 1  # nothing new

    def test_fires_dropped_by_coalescing_are_counted(self, tmp_path, recorded):
        """A pause makes APScheduler run only the latest due fire; the ones it
        dropped are recorded as missed."""
        from apscheduler.events import EVENT_JOB_SUBMITTED

        cfg = _cfg(tmp_path / "c.toml", [ScheduleEntry(id="tick", when=When(every="10s"), runnable="demo")])
        d = daemon.Daemon(cfg)
        d.load(initial=True)
        d._shutdown_scheduler()
        t0 = d._triggers["tick"].get_next_fire_time(None, datetime.datetime.now().astimezone())
        d._on_scheduled(SimpleNamespace(code=EVENT_JOB_SUBMITTED, job_id="tick", scheduled_run_times=[t0]))
        d._on_scheduled(SimpleNamespace(code=EVENT_JOB_SUBMITTED, job_id="tick", scheduled_run_times=[t0 + datetime.timedelta(seconds=10)]))
        d._flush_missed()
        assert recorded["runs"] == []  # back-to-back fires: nothing dropped
        d._on_scheduled(SimpleNamespace(code=EVENT_JOB_SUBMITTED, job_id="tick", scheduled_run_times=[t0 + datetime.timedelta(seconds=60)]))
        d._flush_missed()
        [(args, kw)] = recorded["runs"]
        assert "4 fires" in kw["message"] and kw["status"] == "skipped"

    def test_scheduler_sets_its_own_defaults(self, tmp_path):
        entries = [ScheduleEntry(id="a", when=When(every="1h"), runnable="a"), ScheduleEntry(id="b", when=When(every="1h"), runnable="a", catch_up=True)]
        cfg = _cfg(tmp_path / "c.toml", entries, misfire_grace=120, catch_up_window="2h")
        d = daemon.Daemon(cfg)
        try:
            d.load(initial=True)
            a, b = d._scheduler.get_job("a"), d._scheduler.get_job("b")
            assert a.misfire_grace_time == 120 and a.coalesce is True
            assert b.misfire_grace_time == 7200
        finally:
            d._shutdown_scheduler()


class TestCatchUp:
    def _daemon(self, tmp_path, last_fire, catch_up=True, window="1d"):
        cfg = _cfg(tmp_path / "c.toml", [ScheduleEntry(id="n", when=When(cron="0 2 * * *"), runnable="a", catch_up=catch_up)], catch_up_window=window)
        d = daemon.Daemon(cfg)
        if last_fire:
            d._last_fire = {"n": last_fire.isoformat()}
        return d

    def test_missed_while_down_inside_the_window(self, tmp_path, monkeypatch):
        monkeypatch.setattr(status, "_runs_for", lambda *a, **k: [])
        now = datetime.datetime.now().astimezone()
        d = self._daemon(tmp_path, now - datetime.timedelta(days=1, hours=1))
        entry = ScheduleEntry(id="n", when=When(cron="0 2 * * *"), runnable="a", catch_up=True)
        assert d._missed_while_down(entry, when_to_trigger(entry.when), SchedulerSettings(catch_up_window=86400)) is True

    def test_too_old_or_never_fired_does_not_catch_up(self, tmp_path, monkeypatch):
        monkeypatch.setattr(status, "_runs_for", lambda *a, **k: [])
        now = datetime.datetime.now().astimezone()
        entry = ScheduleEntry(id="n", when=When(cron="0 2 * * *"), runnable="a", catch_up=True)
        trig = when_to_trigger(entry.when)
        # last fire 10 days ago, window 1h: the latest missed 02:00 is older than the window
        d = self._daemon(tmp_path, now - datetime.timedelta(days=10))
        latest_due = trig.get_next_fire_time(None, now - datetime.timedelta(days=1))
        window = int((now - latest_due).total_seconds()) - 60
        if window > 0:
            assert d._missed_while_down(entry, trig, SchedulerSettings(catch_up_window=window)) is False
        # a schedule that never fired doesn't fire at once
        assert self._daemon(tmp_path, None)._missed_while_down(entry, trig, SchedulerSettings()) is False

    def test_catch_up_schedules_one_fire_now(self, tmp_path, monkeypatch):
        monkeypatch.setattr(status, "_runs_for", lambda *a, **k: [])
        now = datetime.datetime.now().astimezone()
        d = self._daemon(tmp_path, now - datetime.timedelta(days=1, hours=1))
        try:
            d.load(initial=True)
            nxt = d._scheduler.get_job("n").next_run_time
            assert nxt <= datetime.datetime.now().astimezone() + datetime.timedelta(seconds=5)
        finally:
            d._shutdown_scheduler()

    def test_without_catch_up_the_next_fire_is_the_regular_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr(status, "_runs_for", lambda *a, **k: [])
        now = datetime.datetime.now().astimezone()
        d = self._daemon(tmp_path, now - datetime.timedelta(days=1, hours=1), catch_up=False)
        try:
            d.load(initial=True)
            nxt = d._scheduler.get_job("n").next_run_time
            assert (nxt.hour, nxt.minute) == (2, 0)
        finally:
            d._shutdown_scheduler()


class TestFire:
    def test_fire_runs_exec_in_its_own_session(self, tmp_path, monkeypatch, recorded):
        monkeypatch.setattr(daemon, "resolve_entry", lambda e: (_target(), ""))
        monkeypatch.setenv("RUNSPEC_RUN_ID", "daemon-own-run")
        monkeypatch.setenv("RUNSPEC_CONFIG", "/scheduler/runspec.toml")
        d = daemon.Daemon(tmp_path / "c.toml", runspec_bin=str(_fake_runspec(tmp_path)))
        d.home = str(tmp_path)
        entry = ScheduleEntry(id="nightly", when=When(cron="0 2 * * *"), runnable="backup", args={"level": "warn"}, env_file="/home/svc/n.env")
        fire = d.fire_entry(entry, SchedulerSettings())
        assert fire is not None
        fire.proc.wait(10)
        [call] = _calls(tmp_path)
        lock = f"{tmp_path}/.cache/runspec-scheduler/{cronsync.venv_tag(tmp_path.parent)}/nightly.lock"
        sched_env = str(tmp_path.parent / "runspec_schedules.env")  # {venv}/runspec_schedules.env of {venv}/bin/runspec
        assert call["argv"] == [
            "exec",
            "--label",
            "sched:nightly",
            "--trigger",
            "schedule:daemon",
            "--lock",
            lock,
            "--env-file",
            sched_env,
            "--timeout",
            "21600",
            "backup",
            "--",
            "--level",
            "warn",
        ]
        assert call["pgid"] == call["pid"]  # its own session / process group
        assert call["env"]["RUNSPEC_ENV_FILE"] == "/home/svc/n.env" and call["env"]["RUNSPEC_AGENT"] == "1"
        assert "RUNSPEC_RUN_ID" not in call["env"] and "RUNSPEC_CONFIG" not in call["env"]
        d.reap()
        assert d._running == {} and recorded["runs"] == []

    def test_timeout_zero_means_no_limit(self, tmp_path, monkeypatch, recorded):
        monkeypatch.setattr(daemon, "resolve_entry", lambda e: (_target(), ""))
        d = daemon.Daemon(tmp_path / "c.toml", runspec_bin=str(_fake_runspec(tmp_path)))
        fire = d.fire_entry(ScheduleEntry(id="s", when=When(every="1h"), runnable="backup", timeout=0), SchedulerSettings())
        fire.proc.wait(10)
        assert "--timeout" not in _calls(tmp_path)[0]["argv"]
        assert fire.deadline is None

    def test_missing_runnable_is_a_recorded_failure(self, tmp_path, monkeypatch, recorded):
        """Review #5: a fire that can't start leaves a record."""
        monkeypatch.setattr(daemon, "resolve_entry", lambda e: (None, "runnable 'gone' not installed in this venv"))
        d = daemon.Daemon(tmp_path / "c.toml", runspec_bin=str(_fake_runspec(tmp_path)))
        assert d.fire_entry(ScheduleEntry(id="s", when=When(every="1h"), runnable="gone"), SchedulerSettings()) is None
        (args, kw) = recorded["runs"][0]
        assert args == ("s", "gone") and kw["status"] == "failed" and kw["exit_code"] == 127
        assert recorded["audit"][0][0] == ("scheduler.fire_failed", "failed")

    def test_missing_runspec_binary_is_a_recorded_failure(self, tmp_path, monkeypatch, recorded):
        monkeypatch.setattr(daemon, "resolve_entry", lambda e: (_target(), ""))
        d = daemon.Daemon(tmp_path / "c.toml", runspec_bin=str(tmp_path / "nope"))
        assert d.fire_entry(ScheduleEntry(id="s", when=When(every="1h"), runnable="backup"), SchedulerSettings()) is None
        assert recorded["runs"][0][1]["exit_code"] == 126

    def test_any_error_in_the_callback_is_recorded(self, tmp_path, monkeypatch, recorded):
        def boom(entry):
            raise RuntimeError("half-installed package")

        monkeypatch.setattr(daemon, "resolve_entry", boom)
        cfg = _cfg(tmp_path / "c.toml", [ScheduleEntry(id="s", when=When(every="1h"), runnable="backup")])
        d = daemon.Daemon(cfg)
        d.load(initial=True)
        d._shutdown_scheduler()
        d._fire("s")
        assert recorded["runs"][0][1]["reason"] == "scheduler_error"

    def test_fire_uses_the_last_good_config(self, tmp_path, monkeypatch, recorded):
        """Review #4: a fire never re-reads (a half-written) config."""
        monkeypatch.setattr(daemon, "resolve_entry", lambda e: (_target(), ""))
        cfg = _cfg(tmp_path / "c.toml", [ScheduleEntry(id="s", when=When(every="1h"), runnable="backup", args={"level": "x"})])
        d = daemon.Daemon(cfg, runspec_bin=str(_fake_runspec(tmp_path)))
        d.load(initial=True)
        d._shutdown_scheduler()
        cfg.write_text("garbage [[[")
        d._fire("s")
        for fire in list(d._running.values()):
            fire.proc.wait(10)
        assert _calls(tmp_path)[0]["argv"][-2:] == ["--level", "x"]

    def test_backstop_kills_the_whole_group(self, tmp_path, monkeypatch, recorded):
        """Review #6: past timeout + backstop, SIGTERM then SIGKILL the group."""
        monkeypatch.setattr(daemon, "resolve_entry", lambda e: (_target(), ""))
        body = """
        import signal, subprocess
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"])
        open(PIDFILE, "w").write(str(child.pid))
        time.sleep(600)
        """.replace("PIDFILE", repr(str(tmp_path / "child.pid")))
        d = daemon.Daemon(tmp_path / "c.toml", runspec_bin=str(_fake_runspec(tmp_path, body)), backstop=0, kill_grace=0.2)
        fire = d.fire_entry(ScheduleEntry(id="s", when=When(every="1h"), runnable="backup", timeout=1), SchedulerSettings())
        deadline = time.monotonic() + 10
        while not (tmp_path / "child.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        child = int((tmp_path / "child.pid").read_text())
        fire.deadline = time.monotonic() - 1
        d.reap()  # SIGTERM (ignored)
        assert fire.killed_at is not None
        time.sleep(0.3)
        d.reap()  # SIGKILL
        fire.proc.wait(10)
        d.reap()
        assert d._running == {}
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(child, signal.SIGKILL)
            pytest.fail("the run's child survived the backstop")
        assert recorded["audit"][0][0] == ("scheduler.fire_killed", "killed")


class TestLockAndState:
    def test_second_lock_reports_the_holder(self, tmp_path):
        fh, holder = daemon.take_lock(tmp_path / "x.lock")
        try:
            assert fh is not None and holder is None
            fh2, holder2 = daemon.take_lock(tmp_path / "x.lock")
            assert fh2 is None and holder2 == os.getpid()
        finally:
            fh.close()
        fh3, holder3 = daemon.take_lock(tmp_path / "x.lock")
        assert fh3 is not None and holder3 is None
        fh3.close()

    def _write_state(self, cfg: Path, **over):
        now = datetime.datetime.now().astimezone()
        started = (now - datetime.timedelta(minutes=5)).isoformat()
        state = {"pid": os.getpid(), "started": started, "heartbeat": now.isoformat(), "interval_s": 5, "firing": True, "jobs": {"a": now.isoformat()}, "running": []}
        state.update(over)
        p = daemon.state_path_for(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state))

    def test_daemon_status_states(self, tmp_path, monkeypatch):
        cfg = tmp_path / "runspec_scheduler.toml"
        monkeypatch.setattr(status, "_pid_is_scheduler", lambda pid: pid == os.getpid())
        assert status.daemon_status(cfg)["state"] == "stopped"  # no heartbeat file
        self._write_state(cfg)
        got = status.daemon_status(cfg)
        assert got["state"] == "running" and got["firing"] is True and got["uptime_s"] >= 299
        old = (datetime.datetime.now().astimezone() - datetime.timedelta(seconds=60)).isoformat()
        self._write_state(cfg, heartbeat=old)
        assert status.daemon_status(cfg)["state"] == "stale"
        self._write_state(cfg, pid=999999999)
        assert status.daemon_status(cfg)["state"] == "stopped"
        self._write_state(cfg, stopped=datetime.datetime.now().astimezone().isoformat())
        assert status.daemon_status(cfg)["state"] == "stopped"

    def test_restart_needed_after_an_upgrade(self, tmp_path, monkeypatch):
        cfg = tmp_path / "runspec_scheduler.toml"
        monkeypatch.setattr(status, "_pid_is_scheduler", lambda pid: pid == os.getpid())
        monkeypatch.setattr(daemon, "installed_version", lambda: "0.7.0")
        self._write_state(cfg, scheduler_version="0.7.0")
        got = status.daemon_status(cfg)
        assert got["version"] == "0.7.0" and got["restart_needed"] is False
        self._write_state(cfg, scheduler_version="0.6.2")  # started before pip install -U
        assert status.daemon_status(cfg)["restart_needed"] is True
        self._write_state(cfg)  # a daemon older than 0.7.0 records no version
        got = status.daemon_status(cfg)
        assert got["version"] is None and got["restart_needed"] is False

    def test_heartbeat_contents(self, tmp_path):
        cfg = _cfg(tmp_path / "runspec_scheduler.toml", [ScheduleEntry(id="a", when=When(every="1h"), runnable="a")])
        d = daemon.Daemon(cfg)
        try:
            d.load(initial=True)
            d.write_state()
        finally:
            d._shutdown_scheduler()
        st = daemon.read_state(cfg)
        assert st["pid"] == os.getpid() and st["backend"] == "daemon" and st["firing"] is True
        assert set(st["jobs"]) == {"a"} and st["config_error"] is None
        assert st["scheduler_version"] == daemon.installed_version()
        assert (tmp_path / "logs" / "scheduler_state.json").is_file()


def test_discover_excludes_self_and_runspec(monkeypatch):
    fake = [
        {"runnable": "runspec-scheduler", "source": "/a", "spec": {}},
        {"runnable": "runspec", "source": "/b", "spec": {}},
        {"runnable": "backup", "source": "/c", "spec": {"run_as": "svc"}},
    ]
    monkeypatch.setattr(discovery, "_discover_installed", lambda: fake)
    monkeypatch.setattr(discovery, "_deduplicate", lambda x: x)
    assert set(discovery.discover()) == {"backup"}
