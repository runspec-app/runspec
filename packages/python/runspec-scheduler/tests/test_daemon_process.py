"""The daemon as a real background process, firing a real runnable through the
real ``runspec exec`` — one test per failure in the robustness review
(docs/design/venv-scheduler.md). Each test starts ``runspec-scheduler run`` in a
throwaway venv (tests/procenv.py) and checks the runs index, the heartbeat and
the processes left behind.

Timings are kept short for CI (``every = "1s"``, a 1s heartbeat); the 48-hour
soak on a real host covers the long-running side.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import procenv as pe  # noqa: E402

pytestmark = [pytest.mark.process, pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon")]


_PROCS: list[pe.Proc] = []


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    return pe.make_venv(tmp_path_factory.mktemp("procvenv"))


@pytest.fixture
def venv(base):
    """The module's venv with a clean slate: no config, logs, heartbeat or lock."""
    pe.kill_leftovers(base)
    for name in ("runspec_scheduler.toml", "runspec_scheduler.lock"):
        (base / name).unlink(missing_ok=True)
    shutil.rmtree(base / "logs", ignore_errors=True)
    shutil.rmtree(base / "supervisor", ignore_errors=True)
    for p in base.glob("daemon-*.out"):
        p.unlink()
    yield base
    while _PROCS:
        _PROCS.pop().stop(5)
    pe.kill_leftovers(base)


def start(venv: Path, *args: str) -> pe.Proc:
    p = pe.Proc(venv, *args)
    _PROCS.append(p)
    pe.wait_for(lambda: (pe.state(venv) or {}).get("pid") == p.pid or p.p.poll() is not None, 20, what="the daemon to start")
    return p


DAEMON = """
[scheduler]
backend = "daemon"
"""


def sched(sid: str, mode: str = "ok", every: str = "1s", **extra: object) -> str:
    args = {"mode": mode, **{k: v for k, v in extra.items() if k in ("seconds", "marker")}}
    arg_text = ", ".join(f'{k} = "{v}"' if isinstance(v, str) else f"{k} = {v}" for k, v in args.items())
    lines = [f'[[schedule]]\nid = "{sid}"\nrunnable = "demo"\nwhen = {{ every = "{every}" }}\nargs = {{ {arg_text} }}']
    for k in ("timeout", "catch_up"):
        if k in extra:
            v = extra[k]
            lines.append(f"{k} = {str(v).lower() if isinstance(v, bool) else v}")
    return "\n".join(lines) + "\n"


# ── basics ──────────────────────────────────────────────────────────────────────


def test_fires_are_recorded_by_exec_under_the_schedule_label(venv):
    pe.write_config(venv, DAEMON + sched("a"))
    d = start(venv)
    rows = pe.wait_for(lambda: [r for r in pe.finished(venv, "a") if r["status"] == "completed"][:2] if len(pe.finished(venv, "a")) >= 2 else None, 20, what="two fires")
    assert all(r["exit_code"] == 0 and r["trigger"] == "schedule:daemon" for r in rows)
    st = pe.state(venv)
    assert st["firing"] is True and "a" in st["jobs"]
    # status (as the CLI reports it) sees the daemon
    out = subprocess.run([str(venv / "bin" / "runspec-scheduler"), "status"], capture_output=True, text=True, cwd=venv, timeout=60)
    import json

    payload = json.loads(out.stdout)
    assert payload["backend"] == "daemon" and payload["daemon"]["state"] == "running"
    [a] = payload["schedules"]
    assert a["installed"] is True and a["line"] == "daemon" and a["runs"]
    # the daemon's own run record was closed at start (review #10)
    own = [r for r in pe.runs(venv) if r["runnable"] == "runspec-scheduler"]
    assert own and all(r["status"] != "running" for r in own)
    assert d.stop() == 0
    assert "already running" not in d.output()


def test_a_second_start_exits_at_once(venv):
    """A watchdog that starts the daemon again must not run a second scheduler."""
    pe.write_config(venv, DAEMON + sched("a", every="1h"))
    first = start(venv)
    t0 = time.monotonic()
    second = subprocess.run([str(venv / "bin" / "runspec-scheduler"), "run"], capture_output=True, text=True, cwd=venv, timeout=30)
    assert second.returncode == 0
    assert f"already running for this venv (pid {first.pid})" in second.stdout
    assert time.monotonic() - t0 < 10
    # and it left no run record of its own
    assert len([r for r in pe.runs(venv) if r["runnable"] == "runspec-scheduler"]) == 1


def test_cron_backend_fires_nothing(venv):
    pe.write_config(venv, sched("a"))  # no [scheduler] → cron
    start(venv)
    time.sleep(3)
    assert pe.runs(venv, "a") == []
    assert pe.state(venv)["firing"] is False


# ── fires that never finish or misbehave ─────────────────────────────────────────


def test_hung_run_is_killed_at_its_timeout_with_its_children(venv, tmp_path):
    """Review #6: a hang is stopped, recorded as timed out, and its process group goes."""
    marker = tmp_path / "hang"
    pe.write_config(venv, DAEMON + sched("h", "hang", every="2s", marker=str(marker), timeout=2, catch_up=False))
    start(venv)
    row = pe.wait_for(lambda: next((r for r in pe.finished(venv, "h") if r["status"] != "skipped"), None), 30, what="the timed-out run")
    assert row["status"] == "failed" and row["exit_code"] == 124
    # the overlapping fires while it hung were recorded skips, not lost
    assert any(r["status"] == "skipped" for r in pe.runs(venv, "h"))
    child = int(pe.wait_for(lambda: Path(str(marker) + ".child").read_text() if Path(str(marker) + ".child").exists() else None, 5, what="child pid"))
    pe.wait_for(lambda: not pe.pid_alive(child), 10, what="the run's background child to be killed")


def test_huge_output_does_not_reach_the_daemon(venv):
    """Review #7: 1 GiB of output; the daemon holds none of it."""
    pe.write_config(venv, DAEMON + sched("big", "bigout", every="2s", seconds=1024, catch_up=False))
    d = start(venv)
    row = pe.wait_for(lambda: next((r for r in pe.finished(venv, "big") if r["status"] != "skipped"), None), 120, what="the 1 GiB run")
    assert row["status"] == "completed" and row["exit_code"] == 0
    rss_kb = int(next(line.split()[1] for line in Path(f"/proc/{d.pid}/status").read_text().splitlines() if line.startswith("VmRSS")))
    assert rss_kb < 200 * 1024


def test_invalid_utf8_output_keeps_the_exit_code(venv):
    """Review #8."""
    pe.write_config(venv, DAEMON + sched("u", "badutf8"))
    start(venv)
    row = pe.wait_for(lambda: next(iter(pe.finished(venv, "u")), None), 20, what="the run")
    assert row["status"] == "completed" and row["exit_code"] == 0


def test_background_child_holding_stdout_does_not_block(venv):
    """Review #7: the run ends when the runnable does; other schedules keep firing."""
    pe.write_config(venv, DAEMON + sched("bg", "bgchild", every="2s", seconds=60) + sched("tick"))
    start(venv)
    pe.wait_for(lambda: next((r for r in pe.finished(venv, "bg") if r["status"] == "completed"), None), 20, what="the bgchild run to finish")
    n = len(pe.finished(venv, "tick"))
    pe.wait_for(lambda: len(pe.finished(venv, "tick")) >= n + 3, 20, what="tick to keep firing")


def test_saturated_pool_still_fires_on_time(venv):
    """Review #6: twenty long fires at once don't stop a short schedule."""
    long = "".join(sched(f"long{i}", "sleep", every="1s", seconds=30) for i in range(20))
    pe.write_config(venv, DAEMON + long + sched("tick"))
    start(venv)
    time.sleep(2)
    n = len(pe.finished(venv, "tick"))
    pe.wait_for(lambda: len(pe.finished(venv, "tick")) >= n + 4, 20, what="tick to keep firing with 20 long runs in flight")
    # overlapping long fires are recorded skips (exec --lock), not silent drops
    pe.wait_for(lambda: any(r["status"] == "skipped" for r in pe.runs(venv, "long0")), 20, what="a recorded overlap skip")


def test_failing_runnable_is_recorded_as_failed(venv):
    pe.write_config(venv, DAEMON + sched("f", "fail"))
    start(venv)
    row = pe.wait_for(lambda: next(iter(pe.finished(venv, "f")), None), 20, what="the run")
    assert row["status"] == "failed" and row["exit_code"] == 3


# ── config ──────────────────────────────────────────────────────────────────────


def test_invalid_config_at_start_exits_non_zero(venv):
    """Review #9."""
    pe.write_config(venv, DAEMON + '[[schedule]]\nid = "x"\nrunnable = "demo"\nwhen = { every = "soon" }\n')
    p = pe.Proc(venv)
    _PROCS.append(p)
    assert p.p.wait(30) == 1
    assert "invalid" in p.output()
    assert pe.state(venv)["config_error"]


def test_config_broken_mid_run_keeps_firing(venv):
    pe.write_config(venv, DAEMON + sched("a"))
    start(venv)
    pe.wait_for(lambda: len(pe.finished(venv, "a")) >= 1, 20, what="first fire")
    pe.write_config(venv, "[[schedule]\nthis is not toml")
    pe.wait_for(lambda: (pe.state(venv) or {}).get("config_error"), 10, what="config_error in the heartbeat")
    n = len(pe.finished(venv, "a"))
    pe.wait_for(lambda: len(pe.finished(venv, "a")) >= n + 2, 20, what="fires from the last good config")
    pe.write_config(venv, DAEMON + sched("a"))
    pe.wait_for(lambda: (pe.state(venv) or {}).get("config_error") is None, 10, what="the error to clear")


def test_fifty_edits_in_a_second(venv):
    """Review #3/#11: the final config wins and the job survives the churn."""
    pe.write_config(venv, DAEMON + sched("a"))
    d = start(venv)
    for i in range(50):
        pe.write_config(venv, DAEMON + sched("a", seconds=i) + (sched("b") if i == 49 else ""))
        time.sleep(0.02)
    pe.wait_for(lambda: len(pe.finished(venv, "b")) >= 1, 20, what="the schedule added by the last edit")
    n = len(pe.finished(venv, "a"))
    pe.wait_for(lambda: len(pe.finished(venv, "a")) >= n + 2, 20, what="a to keep firing")
    assert d.p.poll() is None


def test_sighup_reloads_instead_of_killing(venv):
    """Review #12."""
    pe.write_config(venv, DAEMON + sched("a", every="1h"))
    d = start(venv)
    d.signal(signal.SIGHUP)
    time.sleep(1.5)
    assert d.p.poll() is None
    assert "SIGHUP" in d.output()


# ── stopping ────────────────────────────────────────────────────────────────────


def test_sigterm_during_a_fire_stops_fast_and_the_fire_finishes(venv):
    """Review #13: the stop doesn't wait for the run; exec still records it."""
    pe.write_config(venv, DAEMON + sched("s", "sleep", every="1s", seconds=4))
    d = start(venv)
    pe.wait_for(lambda: any(r["status"] == "running" for r in pe.runs(venv, "s")), 20, what="a fire in flight")
    t0 = time.monotonic()
    d.signal(signal.SIGTERM)
    assert d.p.wait(10) == 0
    assert time.monotonic() - t0 < 3
    pe.wait_for(lambda: any(r["status"] == "completed" for r in pe.runs(venv, "s")), 20, what="the fire to finish after the stop")


def test_kill_9_during_a_fire(venv):
    """The fire outlives a crashed daemon, and status reports it stopped."""
    pe.write_config(venv, DAEMON + sched("s", "sleep", every="1s", seconds=3))
    d = start(venv)
    pe.wait_for(lambda: any(r["status"] == "running" for r in pe.runs(venv, "s")), 20, what="a fire in flight")
    d.signal(signal.SIGKILL)
    d.p.wait(10)
    pe.wait_for(lambda: any(r["status"] == "completed" for r in pe.runs(venv, "s")), 20, what="the fire to finish")
    sys.path.insert(0, str(Path(__file__).parents[1]))
    from runspec_scheduler.status import daemon_status

    assert daemon_status(venv / "runspec_scheduler.toml")["state"] == "stopped"
    # and a fresh start takes over the lock
    start(venv)


def test_paused_daemon_runs_the_due_fire_after_sigcont(venv):
    """Review #1: a pause shorter than the misfire grace still fires (once)."""
    pe.write_config(venv, DAEMON + sched("a", every="1s"))
    d = start(venv)
    pe.wait_for(lambda: len(pe.finished(venv, "a")) >= 1, 20, what="first fire")
    d.signal(signal.SIGSTOP)
    time.sleep(8)
    n = len(pe.runs(venv, "a"))
    d.signal(signal.SIGCONT)
    pe.wait_for(lambda: len(pe.finished(venv, "a")) > n, 20, what="a fire after SIGCONT")
    # coalesced: the eight missed seconds are not eight runs
    time.sleep(1.5)
    assert len(pe.runs(venv, "a")) <= n + 4


def test_pause_past_the_grace_is_a_recorded_miss(venv):
    """Review #1: a fire later than the grace is a recorded skipped run."""
    pe.write_config(venv, '[scheduler]\nbackend = "daemon"\nmisfire_grace = 1\n' + sched("a", every="2s"))
    d = start(venv)
    pe.wait_for(lambda: len(pe.finished(venv, "a")) >= 1, 20, what="first fire")
    d.signal(signal.SIGSTOP)
    time.sleep(6)
    d.signal(signal.SIGCONT)
    row = pe.wait_for(lambda: next((r for r in pe.runs(venv, "a") if r["status"] == "skipped"), None), 20, what="a recorded missed fire")
    assert row["exit_code"] == 0


def test_catch_up_fires_once_on_start(venv):
    """A catch_up schedule whose fire was due while the daemon was down runs on start."""
    import datetime
    import json

    cfg = DAEMON + '[[schedule]]\nid = "c"\nrunnable = "demo"\ncatch_up = true\nwhen = { cron = "0 3 * * *" }\n'
    pe.write_config(venv, cfg + cfg.replace('id = "c"', 'id = "n"').replace("catch_up = true", "catch_up = false").replace(DAEMON, ""))
    yesterday = (datetime.datetime.now().astimezone() - datetime.timedelta(days=1, hours=1)).isoformat()
    (venv / "logs").mkdir(exist_ok=True)
    (venv / "logs" / "scheduler_state.json").write_text(json.dumps({"pid": 0, "last_fire": {"c": yesterday, "n": yesterday}}))
    start(venv)
    pe.wait_for(lambda: len(pe.finished(venv, "c")) == 1, 20, what="the catch-up fire")
    time.sleep(2)
    assert pe.runs(venv, "n") == []  # no catch_up → skipped, as cron does


# ── switching backends against a real crontab ───────────────────────────────────

real_crontab = pytest.mark.skipif(
    not (shutil.which("crontab") and os.environ.get("RUNSPEC_SCHEDULER_REAL_CRONTAB") == "1"),
    reason="needs crontab and RUNSPEC_SCHEDULER_REAL_CRONTAB=1 (it rewrites this account's crontab; CI sets it)",
)


@pytest.fixture
def crontab():
    """This account's crontab, restored afterwards."""
    before = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    saved = before.stdout if before.returncode == 0 else None
    subprocess.run(["crontab", "-"], input="# operator's own line\n17 4 * * * /bin/true\n", text=True, check=True)
    yield lambda: subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    if saved is None:
        subprocess.run(["crontab", "-r"], capture_output=True)
    else:
        subprocess.run(["crontab", "-"], input=saved, text=True)


def _cli(venv: Path, *args: str) -> tuple[int, dict]:
    import json

    p = subprocess.run([str(venv / "bin" / "runspec-scheduler"), *args], capture_output=True, text=True, cwd=venv, timeout=60)
    try:
        return p.returncode, json.loads(p.stdout)
    except ValueError:
        raise AssertionError(f"not JSON: {p.stdout!r} {p.stderr!r}") from None


@real_crontab
def test_use_switches_backends_without_ever_running_both(venv, crontab):
    pe.write_config(venv, '[[schedule]]\nid = "n"\nrunnable = "demo"\nwhen = { cron = "0 2 * * *" }\n')
    rc, out = _cli(venv, "sync")
    assert rc == 0, out
    assert "# runspec:sched:n" in crontab() and "/bin/true" in crontab()

    rc, out = _cli(venv, "use", "daemon")
    assert rc == 0 and out["backend"] == "daemon", out
    assert "# runspec:sched:n" not in crontab() and "/bin/true" in crontab()
    assert out["daemon"]["state"] == "stopped" and "hint" in out

    rc, out = _cli(venv, "sync")  # the daemon owns this venv now: no lines written
    assert rc == 0 and out["backend"] == "daemon" and out["installed"] == [], out
    assert "# runspec:sched:n" not in crontab()

    start(venv)
    rc, out = _cli(venv, "status")
    assert out["backend"] == "daemon" and out["daemon"]["firing"] is True and out["stale"] == []

    rc, out = _cli(venv, "use", "cron")
    assert rc == 0 and out["backend"] == "cron", out
    assert "# runspec:sched:n" in crontab()
    pe.wait_for(lambda: (pe.state(venv) or {}).get("firing") is False, 10, what="the daemon to stop firing")


@real_crontab
def test_status_flags_a_crontab_line_left_while_on_the_daemon(venv, crontab):
    pe.write_config(venv, '[[schedule]]\nid = "n"\nrunnable = "demo"\nwhen = { cron = "0 2 * * *" }\n')
    _cli(venv, "sync")
    pe.write_config(venv, DAEMON + '[[schedule]]\nid = "n"\nrunnable = "demo"\nwhen = { cron = "0 2 * * *" }\n')  # hand-edited, no `use`
    _, out = _cli(venv, "status")
    assert out["stale"] == [{"id": "n", "user": "(self)", "reason": "backend is daemon"}]

    # sync on a daemon venv strips the leftover line, as the console's tip says
    rc, out = _cli(venv, "sync")
    assert rc == 0 and out["backend"] == "daemon", out
    assert out["installed"] == [{"id": "n", "user": "(self)", "removed": "true"}]
    assert "# runspec:sched:n" not in crontab()
    _, out = _cli(venv, "status")
    assert out["stale"] == []


def test_add_refuses_every_on_cron(venv):
    rc, out = _cli(venv, "add", "--id", "p", "--runnable", "demo", "--every", "30s")
    assert rc == 2 and "daemon backend" in out["error"]
    pe.write_config(venv, DAEMON)
    rc, out = _cli(venv, "add", "--id", "p", "--runnable", "demo", "--every", "30s", "--timeout", "90m", "--catch-up")
    assert rc == 0, out
    _, listed = _cli(venv, "list")
    [p] = listed["schedules"]
    assert p["timeout"] == 5400 and p["catch_up"] is True and listed["backend"] == "daemon"


# ── keepers: run --detach, the cron watchdog, supervisord in the venv ───────────


def _plain(venv: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(venv / "bin" / "runspec-scheduler"), *args], capture_output=True, text=True, cwd=venv, timeout=60)


def _daemon_pid(venv: Path) -> int | None:
    st = pe.state(venv) or {}
    pid = st.get("pid")
    return pid if isinstance(pid, int) and pe.pid_alive(pid) else None


def test_run_detach_starts_once_and_hands_the_lock_over(venv):
    pe.write_config(venv, DAEMON + sched("d"))
    p = _plain(venv, "run", "--detach", "--reload-interval", "1")
    assert p.returncode == 0 and "started" in p.stdout, (p.stdout, p.stderr)
    pid = pe.wait_for(lambda: _daemon_pid(venv), 20, what="the detached daemon")
    assert (venv / "runspec_scheduler.lock").read_text().split()[0] == str(pid)
    again = _plain(venv, "run", "--detach")
    assert again.returncode == 0 and f"already running for this venv (pid {pid})" in again.stdout
    pe.wait_for(lambda: len(pe.finished(venv, "d")) >= 1, 20, what="a fire from the detached daemon")
    assert os.getsid(pid) == pid  # its own session: a cron job's exit can't take it down
    os.kill(pid, signal.SIGTERM)
    pe.wait_for(lambda: not pe.pid_alive(pid), 20, what="the daemon to stop")
    p = _plain(venv, "run", "--detach", "--reload-interval", "1")  # the lock went with it
    assert "started" in p.stdout
    pe.wait_for(lambda: _daemon_pid(venv) not in (None, pid), 20, what="a fresh daemon")


@real_crontab
def test_cron_keeper_installs_starts_and_removes(venv, crontab):
    pe.write_config(venv, DAEMON)
    rc, out = _cli(venv, "install-service", "--manager", "cron")
    assert rc == 0 and out["written"] is False and "scheduler-keeper" not in crontab(), out

    rc, out = _cli(venv, "install-service", "--manager", "cron", "--write")
    assert rc == 0 and out["written"] is True, out
    tab = crontab()
    assert tab.count(f"# runspec:scheduler-keeper:{venv}") == 2 and "/bin/true" in tab
    pid = pe.wait_for(lambda: _daemon_pid(venv), 20, what="the daemon the keeper started")
    _, st = _cli(venv, "status")
    assert [k["manager"] for k in st["keeper"]] == ["cron"]

    # what the watchdog line runs every 5 minutes: a no-op while it's up
    watchdog = next(line for line in tab.splitlines() if line.startswith("*/5"))
    subprocess.run(watchdog.split(" ", 5)[5], shell=True, check=True, timeout=30)
    assert _daemon_pid(venv) == pid

    rc, out = _cli(venv, "install-service", "--remove", "--write")
    assert rc == 0 and out["ok"], out
    assert "scheduler-keeper" not in crontab() and "/bin/true" in crontab()
    assert not pe.pid_alive(pid)
    _, st = _cli(venv, "status")
    assert st["keeper"] == [] and st["daemon"]["state"] == "stopped"


@real_crontab
@pytest.mark.skipif(importlib.util.find_spec("supervisor") is None, reason="needs supervisor (the dev extra)")
def test_supervisor_keeper_restarts_a_killed_daemon(venv, crontab):
    pe.write_config(venv, DAEMON)
    rc, out = _cli(venv, "install-service", "--manager", "supervisor", "--write")
    assert rc == 0 and out["ok"], out
    assert crontab().count(f"# runspec:scheduler-keeper:{venv}") == 2
    pid = pe.wait_for(lambda: _daemon_pid(venv), 30, what="the daemon under supervisord")
    os.kill(pid, signal.SIGKILL)
    pe.wait_for(lambda: _daemon_pid(venv) not in (None, pid), 30, what="supervisord to restart the daemon")
    new = _daemon_pid(venv)
    _, st = _cli(venv, "status")
    [k] = st["keeper"]
    assert k["manager"] == "supervisor" and k["supervisord_pid"]

    rc, out = _cli(venv, "install-service", "--remove", "--write")
    assert rc == 0 and out["ok"], out
    pe.wait_for(lambda: not pe.pid_alive(new), 30, what="the daemon to stop")
    assert not pe.pid_alive(k["supervisord_pid"])
    assert "scheduler-keeper" not in crontab() and not (venv / "supervisor").exists()
