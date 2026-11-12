"""Tests for install-service's keeper choice, plans and apply (no real systemd)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from runspec_scheduler import keeper


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def systemctl(calls, units):
    """A stand-in subprocess.run: records each argv and, like the real
    ``systemctl --user enable``, links the unit into default.target.wants."""

    def run(argv, **k):
        calls.append(argv)
        if argv[:3] == ["systemctl", "--user", "enable"]:
            wants = units / "default.target.wants"
            wants.mkdir(parents=True, exist_ok=True)
            (wants / argv[-1]).symlink_to(units / argv[-1])
        if argv[:3] == ["systemctl", "--user", "disable"]:
            (units / "default.target.wants" / argv[-1]).unlink(missing_ok=True)
        return _Proc(0)

    return run


def facts(**kw):
    base = {"user": "svc", "venv_owner": "svc", "linger": True, "systemd_version": 252, "user_bus": True, "supervisord": None, "crontab": True}
    base.update(kw)
    return keeper.Facts(**base)


@pytest.fixture
def venv(monkeypatch, tmp_path):
    """A fake venv layout: scripts dir, unit dir and crontab all in tmp_path."""
    root = tmp_path / "venvs" / "ops"
    (root / "bin").mkdir(parents=True)
    monkeypatch.setattr(keeper, "venv_root", lambda: root)
    monkeypatch.setattr(keeper, "scripts_dir", lambda: root / "bin")
    units = tmp_path / "systemd" / "user"
    monkeypatch.setattr(keeper, "unit_dir", lambda: units)
    monkeypatch.setattr(keeper, "system_unit_path", lambda: tmp_path / "etc" / "runspec-scheduler-ops.service")
    monkeypatch.setattr(keeper, "unit_name", lambda: "runspec-scheduler-ops.service")
    tab: list[str] = ["# mine", "17 4 * * * /bin/true"]
    monkeypatch.setattr(keeper, "_read_own_crontab", lambda: list(tab))

    def write(lines, existing=None):
        tab[:] = lines

    monkeypatch.setattr(keeper, "_write_own_crontab", write)
    monkeypatch.setattr(keeper.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(keeper, "_daemon_pid", lambda config: None)
    return SimpleNamespace(root=root, tab=tab, units=units, etc=tmp_path / "etc")


# ── auto ────────────────────────────────────────────────────────────────────────


def test_auto_takes_systemd_user_when_linger_is_on():
    assert keeper.choose(facts())[0] == "systemd-user"


@pytest.mark.parametrize(
    "kw, why",
    [
        ({"linger": False}, "lingering is off"),
        ({"systemd_version": 219}, "systemd 219 is too old"),
        ({"systemd_version": None, "linger": None}, "no systemd"),
        ({"user_bus": False}, "bus"),
    ],
)
def test_auto_falls_back_to_supervisor_then_cron(kw, why):
    manager, reason = keeper.choose(facts(**kw))
    assert manager == "cron" and why in reason and "runspec-scheduler[supervisor]" in reason
    manager, reason = keeper.choose(facts(supervisord=Path("/v/bin/supervisord"), **kw))
    assert manager == "supervisor" and why in reason


# ── plans ───────────────────────────────────────────────────────────────────────


def test_nothing_changes_without_write(venv):
    out = keeper.plan_install("cron", facts=facts()).to_dict(written=False)
    assert out["ok"] and out["written"] is False and "--write" in out["hint"]
    assert out["crontab"][0].startswith("@reboot ") and "run --detach" in out["crontab"][0]
    assert venv.tab == ["# mine", "17 4 * * * /bin/true"]


def test_someone_else_than_the_owner_is_refused(venv):
    out = keeper.plan_install("cron", facts=facts(user="jason")).to_dict(written=False)
    assert out["ok"] is False and "venv owner (svc)" in out["error"] and "sudo -iu svc" in out["hint"]


def test_system_unit_is_only_printed(venv):
    out = keeper.plan_install("systemd", facts=facts(user="jason")).to_dict(written=False)
    assert out["ok"]
    [f] = out["files"]
    assert f["by"] == "root" and "User=svc" in f["content"]
    assert out["root_steps"][-1] == "sudo systemctl enable --now runspec-scheduler-ops.service"
    keeper.apply(keeper.plan_install("systemd", facts=facts()))
    assert (venv.root / "runspec-scheduler-ops.service").is_file()  # staged for root
    assert not venv.etc.exists()


def test_supervisor_system_prints_a_program_block(venv):
    out = keeper.plan_install("supervisor", facts=facts(user="jason"), system=True).to_dict(written=False)
    assert out["ok"] and out["steps"] == []
    [f] = out["files"]
    assert "user=svc" in f["content"] and "[program:runspec-scheduler-ops]" in f["content"]


def test_supervisor_needs_supervisord_in_the_venv(venv):
    out = keeper.plan_install("supervisor", facts=facts()).to_dict(written=False)
    assert out["ok"] is False and "runspec-scheduler[supervisor]" in out["hint"]


def test_root_owned_venv_warns(venv):
    out = keeper.plan_install("cron", facts=facts(user="root", venv_owner="root")).to_dict(written=False)
    assert out["ok"] and any("root" in w for w in out["warnings"])


def test_config_is_passed_on(venv, tmp_path):
    cfg = tmp_path / "other.toml"
    out = keeper.plan_install("cron", facts=facts(), config=str(cfg)).to_dict(written=False)
    assert f"--config {cfg}" in out["crontab"][1]


# ── apply ───────────────────────────────────────────────────────────────────────


def test_cron_keeper_lines_are_tagged_and_replace_old_ones(venv, monkeypatch):
    monkeypatch.setattr(keeper.subprocess, "run", lambda *a, **k: _Proc(0))
    keeper.apply(keeper.plan_install("cron", facts=facts()))
    keeper.apply(keeper.plan_install("cron", facts=facts()))  # twice: still one pair
    tag = f"# runspec:scheduler-keeper:{venv.root}"
    assert venv.tab.count(tag) == 2 and venv.tab[:2] == ["# mine", "17 4 * * * /bin/true"]
    assert keeper.installed() == [{"manager": "cron", "where": "crontab", "crontab": keeper.keeper_lines(venv.tab)}]


def test_keeper_lines_of_another_venv_are_left_alone(venv, monkeypatch):
    monkeypatch.setattr(keeper.subprocess, "run", lambda *a, **k: _Proc(0))
    others = ["# runspec:scheduler-keeper:/opt/venvs/other", "@reboot /opt/venvs/other/bin/runspec-scheduler run --detach", "# runspec:sched:n", "0 2 * * * x"]
    venv.tab += others
    keeper.apply(keeper.plan_install("cron", facts=facts()))
    keeper.apply(keeper.plan_remove("auto"))
    assert venv.tab == ["# mine", "17 4 * * * /bin/true", *others]


def test_systemd_user_writes_and_enables(venv, monkeypatch):
    calls = []
    monkeypatch.setattr(keeper.subprocess, "run", systemctl(calls, venv.units))
    out = keeper.apply(keeper.plan_install("systemd-user", facts=facts()))
    assert out["ok"] and out["written"]
    assert (venv.units / "runspec-scheduler-ops.service").is_file()
    assert calls == [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", "runspec-scheduler-ops.service"]]
    assert [k["manager"] for k in keeper.installed()] == ["systemd-user"]


def test_systemd_user_warns_without_linger_when_a_session_bus_answers(venv):
    out = keeper.plan_install("systemd-user", facts=facts(linger=False)).to_dict(written=False)
    assert out["ok"] and any("enable-linger svc" in w for w in out["warnings"])


def test_systemd_user_without_linger_or_bus_is_refused_before_writing(venv):
    out = keeper.apply(keeper.plan_install("systemd-user", facts=facts(linger=False, user_bus=False)))
    assert out["ok"] is False and "lingering is off for svc" in out["error"]
    assert "sudo loginctl enable-linger svc" in out["hint"] and "--manager cron" in out["hint"]
    assert out["written"] is False and not (venv.units / "runspec-scheduler-ops.service").exists()
    out = keeper.plan_install("systemd-user", facts=facts(user_bus=False)).to_dict(written=False)
    assert out["ok"] is False and "doesn't answer" in out["error"] and "-iu" in out["hint"]


@pytest.mark.parametrize(
    "stderr",
    [
        "Failed to connect to bus: No medium found",
        "Failed to connect to user scope bus via local transport: $DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined",
    ],
)
def test_systemd_user_bus_error_undoes_the_unit_and_gives_login_shell_hint(venv, monkeypatch, stderr):
    monkeypatch.setattr(keeper.subprocess, "run", lambda *a, **k: _Proc(1, stderr=stderr))
    unit = venv.units / "runspec-scheduler-ops.service"
    out = keeper.apply(keeper.plan_install("systemd-user", facts=facts()))
    assert out["ok"] is False and "-iu" in out["hint"] and "enable-linger" in out["hint"]
    assert out["done"] == [f"write {unit}"] and out["undone"] == [str(unit)]
    assert out["written"] is False and not unit.exists()


def test_a_failed_reinstall_puts_the_old_unit_back(venv, monkeypatch):
    unit = venv.units / "runspec-scheduler-ops.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("old unit\n")
    monkeypatch.setattr(keeper.subprocess, "run", lambda *a, **k: _Proc(1, stderr="boom"))
    out = keeper.apply(keeper.plan_install("systemd-user", facts=facts()))
    assert out["ok"] is False and unit.read_text() == "old unit\n"


def test_a_unit_that_was_never_enabled_is_not_the_keeper(venv, monkeypatch):
    # what an install that failed before 0.10.3 left behind, next to the cron
    # watchdog it fell back to
    unit = venv.units / "runspec-scheduler-ops.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(f"ExecStart={venv.root}/bin/runspec-scheduler run\n")
    monkeypatch.setattr(keeper.subprocess, "run", lambda argv, **k: _Proc(0))
    keeper.apply(keeper.plan_install("cron", facts=facts()))
    assert [k["manager"] for k in keeper.installed()] == ["cron"]
    monkeypatch.setattr(keeper, "_daemon_pid", lambda config: 999999)
    plan = keeper.plan_restart(facts=facts())
    assert plan.manager == "cron" and [a.kind for a in plan.actions] == ["stop", "run"]
    # remove deletes the stray file without asking systemctl (no bus needed)
    plan = keeper.plan_remove("auto")
    assert [(a.kind, a.path) for a in plan.actions if a.kind == "unlink"] == [("unlink", unit)]
    assert not any(a.argv and a.argv[0] == "systemctl" for a in plan.actions)


def test_remove_undoes_systemd_user_and_stops_the_daemon(venv, monkeypatch):
    calls = []
    monkeypatch.setattr(keeper.subprocess, "run", systemctl(calls, venv.units))
    keeper.apply(keeper.plan_install("systemd-user", facts=facts()))
    monkeypatch.setattr(keeper, "_daemon_pid", lambda config: 999999)
    stopped = []
    monkeypatch.setattr(keeper, "_stop", lambda pid, wait=30: stopped.append(pid))
    plan = keeper.plan_remove("auto")
    assert [a.kind for a in plan.actions] == ["run", "unlink", "run", "stop"]
    out = keeper.apply(plan)
    assert out["ok"] and stopped == [999999]
    assert not (venv.units / "runspec-scheduler-ops.service").exists()
    assert ["systemctl", "--user", "disable", "--now", "runspec-scheduler-ops.service"] in calls


def test_remove_with_nothing_installed(venv):
    out = keeper.plan_remove("auto").to_dict(written=False)
    assert out["ok"] and out["steps"] == [] and "no keeper" in out["hint"]


def test_remove_system_unit_is_left_to_root(venv):
    unit = venv.etc / "runspec-scheduler-ops.service"
    unit.parent.mkdir()
    unit.write_text(f"ExecStart={venv.root}/bin/runspec-scheduler run\n")
    out = keeper.plan_remove("auto").to_dict(written=False)
    assert out["root_steps"][0] == "sudo systemctl disable --now runspec-scheduler-ops.service"


def test_strip_keeper_lines_takes_tag_and_next_line_only():
    tag = "# runspec:scheduler-keeper:/v"
    lines = ["a", tag, "@reboot /v/bin/runspec-scheduler run --detach", "b", tag, "*/5 * * * * /v/bin/runspec-scheduler run --detach", "c"]
    assert keeper.strip_keeper_lines(lines, tag) == ["a", "b", "c"]
    assert keeper.keeper_lines(lines, tag) == ["@reboot /v/bin/runspec-scheduler run --detach", "*/5 * * * * /v/bin/runspec-scheduler run --detach"]


def test_early_lock_sees_run_after_a_global_config():
    from runspec_scheduler import cli

    assert cli._argv_words(["--config", "/v/c.toml", "run", "--detach"]) == ["run"]
    assert cli._argv_words(["run", "--reload-interval", "1"]) == ["run", "1"]


def test_strip_keeper_lines_keeps_a_line_under_a_lone_tag():
    tag = "# runspec:scheduler-keeper:/opt/venvs/ops"
    existing = [tag, "@reboot /opt/venvs/ops/bin/runspec-scheduler run --detach", tag, "0 3 * * * /usr/bin/mine"]
    assert keeper.strip_keeper_lines(existing, tag) == ["0 3 * * * /usr/bin/mine"]


# ── restart ─────────────────────────────────────────────────────────────────────


def test_restart_through_systemd_user(venv, monkeypatch):
    calls = []
    monkeypatch.setattr(keeper.subprocess, "run", systemctl(calls, venv.units))
    keeper.apply(keeper.plan_install("systemd-user", facts=facts()))
    calls.clear()
    out = keeper.apply(keeper.plan_restart(facts=facts()))
    assert out["ok"] and out["action"] == "restart" and out["manager"] == "systemd-user"
    assert calls == [["systemctl", "--user", "restart", "runspec-scheduler-ops.service"]]


def test_restart_through_supervisor(venv, monkeypatch):
    sup = venv.root / "bin" / "supervisord"
    sup.write_text("")
    f = facts(supervisord=sup, linger=False)
    monkeypatch.setattr(keeper.subprocess, "run", lambda argv, **k: _Proc(0))
    monkeypatch.setattr(keeper, "_supervisord_pid", lambda: None)
    keeper.apply(keeper.plan_install("supervisor", facts=f))
    plan = keeper.plan_restart(facts=f)  # supervisord down: starting it starts the daemon
    assert [a.argv for a in plan.actions] == [[str(sup), "-c", str(keeper.supervisor_conf())]]
    monkeypatch.setattr(keeper, "_supervisord_pid", lambda: 4242)
    plan = keeper.plan_restart(facts=f)
    assert plan.actions[0].argv[-2:] == ["restart", "runspec-scheduler"]


def test_restart_with_the_cron_watchdog_stops_then_detaches(venv, monkeypatch):
    monkeypatch.setattr(keeper.subprocess, "run", lambda argv, **k: _Proc(0))
    keeper.apply(keeper.plan_install("cron", facts=facts()))
    monkeypatch.setattr(keeper, "_daemon_pid", lambda config: 999999)
    plan = keeper.plan_restart(facts=facts())
    assert [a.kind for a in plan.actions] == ["stop", "run"]
    assert plan.actions[1].argv[-2:] == ["run", "--detach"]
    assert plan.warnings == []


def test_restart_without_a_keeper_starts_it_and_warns(venv):
    plan = keeper.plan_restart(facts=facts())
    assert [a.kind for a in plan.actions] == ["run"]
    assert "no keeper" in plan.warnings[0]


def test_restart_refuses_anyone_but_the_owner_and_a_system_unit(venv):
    out = keeper.plan_restart(facts=facts(user="ops")).to_dict(written=False)
    assert out["ok"] is False and "sudo -iu svc" in out["hint"]
    unit = venv.etc / "runspec-scheduler-ops.service"
    unit.parent.mkdir()
    unit.write_text(f"ExecStart={venv.root}/bin/runspec-scheduler run\n")
    out = keeper.plan_restart(facts=facts()).to_dict(written=False)
    assert out["ok"] is False and out["root_steps"] == ["sudo systemctl restart runspec-scheduler-ops.service"]


def test_supervisor_logs_are_left_to_the_daemon_to_rotate(venv):
    assert "logfile_maxbytes=0" in keeper.render_supervisord_conf()
    own = keeper.render_supervisor_program(["/v/bin/runspec-scheduler", "run"])
    assert "stdout_logfile_maxbytes=0" in own
    system = keeper.render_supervisor_program(["/v/bin/runspec-scheduler", "run"], user="svc")
    assert "stdout_logfile_maxbytes=1MB" in system and "stdout_logfile_backups=7" in system


# ── switching keepers ───────────────────────────────────────────────────────────


def test_switching_from_the_cron_watchdog_to_systemd_user(venv, monkeypatch):
    calls = []
    monkeypatch.setattr(keeper.subprocess, "run", systemctl(calls, venv.units))
    keeper.apply(keeper.plan_install("cron", facts=facts()))
    assert any("run --detach" in line for line in venv.tab)
    # the watchdog's daemon is running, so systemd's would find the lock held
    monkeypatch.setattr(keeper, "_daemon_pid", lambda config: 999999)
    stopped = []
    monkeypatch.setattr(keeper, "_stop", lambda pid, wait=30: stopped.append(pid))
    plan = keeper.plan_install("systemd-user", facts=facts())
    assert [a.kind for a in plan.actions] == ["crontab", "stop", "write", "run", "run"]
    out = keeper.apply(plan)
    assert out["ok"] and stopped == [999999]
    assert venv.tab == ["# mine", "17 4 * * * /bin/true"]
    assert [k["manager"] for k in keeper.installed()] == ["systemd-user"]


def test_switching_from_systemd_user_to_cron_disables_the_unit(venv, monkeypatch):
    calls = []
    monkeypatch.setattr(keeper.subprocess, "run", systemctl(calls, venv.units))
    keeper.apply(keeper.plan_install("systemd-user", facts=facts()))
    monkeypatch.setattr(keeper, "_daemon_pid", lambda config: 999999)
    monkeypatch.setattr(keeper, "_stop", lambda pid, wait=30: None)
    calls.clear()
    out = keeper.apply(keeper.plan_install("cron", facts=facts()))
    assert out["ok"]
    assert calls[0] == ["systemctl", "--user", "disable", "--now", "runspec-scheduler-ops.service"]
    assert not (venv.units / "runspec-scheduler-ops.service").exists()
    assert [k["manager"] for k in keeper.installed()] == ["cron"]


def test_reinstalling_the_same_keeper_leaves_the_daemon_running(venv, monkeypatch):
    monkeypatch.setattr(keeper.subprocess, "run", lambda argv, **k: _Proc(0))
    keeper.apply(keeper.plan_install("cron", facts=facts()))
    monkeypatch.setattr(keeper, "_daemon_pid", lambda config: 999999)
    plan = keeper.plan_install("cron", facts=facts())
    assert [a.kind for a in plan.actions] == ["crontab", "run"]
