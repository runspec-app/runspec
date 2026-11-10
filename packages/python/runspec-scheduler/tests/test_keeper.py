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
    monkeypatch.setattr(keeper.subprocess, "run", lambda argv, **k: calls.append(argv) or _Proc(0))
    out = keeper.apply(keeper.plan_install("systemd-user", facts=facts()))
    assert out["ok"] and out["written"]
    assert (venv.units / "runspec-scheduler-ops.service").is_file()
    assert calls == [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", "runspec-scheduler-ops.service"]]


def test_systemd_user_warns_without_linger(venv):
    out = keeper.plan_install("systemd-user", facts=facts(linger=False)).to_dict(written=False)
    assert any("enable-linger svc" in w for w in out["warnings"])


def test_systemd_user_bus_error_gives_login_shell_hint(venv, monkeypatch):
    monkeypatch.setattr(keeper.subprocess, "run", lambda *a, **k: _Proc(1, stderr="Failed to connect to bus: No medium found"))
    out = keeper.apply(keeper.plan_install("systemd-user", facts=facts()))
    assert out["ok"] is False and "-iu" in out["hint"] and "enable-linger" in out["hint"]
    assert (venv.units / "runspec-scheduler-ops.service").is_file()  # written before the failing step
    assert out["done"] == [f"write {venv.units / 'runspec-scheduler-ops.service'}"]


def test_remove_undoes_systemd_user_and_stops_the_daemon(venv, monkeypatch):
    calls = []
    monkeypatch.setattr(keeper.subprocess, "run", lambda argv, **k: calls.append(argv) or _Proc(0))
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
    monkeypatch.setattr(keeper.subprocess, "run", lambda argv, **k: calls.append(argv) or _Proc(0))
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
