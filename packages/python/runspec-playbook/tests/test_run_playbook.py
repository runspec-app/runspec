"""Tests for run-playbook.

The pure helpers are tested directly. The end-to-end path is exercised
in-process with a stubbed ``run_ansible`` (so no Ansible install is needed):
``rs.parse`` still reads the bundled runspec.toml because the finder walks up
from the calling module's directory.
"""

import json

import pytest

from runspec_playbook import run_playbook

# ── pure helpers ──────────────────────────────────────────────────────────────


def test_compose_package_spec_plain() -> None:
    assert run_playbook.compose_package_spec("myapp", "1.4.2") == "myapp==1.4.2"


def test_compose_package_spec_preserves_extras() -> None:
    assert run_playbook.compose_package_spec("myapp[postgres,redis]", "1.4.2") == "myapp[postgres,redis]==1.4.2"


def test_compose_package_spec_requires_package() -> None:
    with pytest.raises(ValueError):
        run_playbook.compose_package_spec("", "1.0.0")


def test_parse_extra_vars_splits_on_first_equals() -> None:
    assert run_playbook.parse_extra_vars(["venv_path=/opt/x/venv", "url=https://h/p?a=b"]) == {
        "venv_path": "/opt/x/venv",
        "url": "https://h/p?a=b",
    }


def test_parse_extra_vars_rejects_bare_token() -> None:
    with pytest.raises(ValueError):
        run_playbook.parse_extra_vars(["novalue"])


def test_summarize_stats_totals_across_hosts() -> None:
    stats = {
        "ok": {"localhost": 3},
        "changed": {"localhost": 2},
        "failures": {"localhost": 1},
        "dark": {},
    }
    assert run_playbook.summarize_stats(stats) == {
        "ok": 3,
        "changed": 2,
        "failed": 1,
        "unreachable": 0,
        "skipped": 0,
    }


# ── end-to-end (stubbed Ansible) ────────────────────────────────────────────────


def _stub_runner(monkeypatch, captured, *, status="successful", rc=0, stats=None):
    def fake_run_ansible(playbook_name, extravars, *, check, verbosity, tags, skip_tags, envvars):
        captured.update(
            playbook_name=playbook_name,
            extravars=extravars,
            check=check,
            verbosity=verbosity,
            tags=tags,
            skip_tags=skip_tags,
        )
        return {"status": status, "rc": rc, "stats": stats if stats is not None else {"changed": {"localhost": 1}, "ok": {"localhost": 2}}}

    monkeypatch.setattr(run_playbook, "run_ansible", fake_run_ansible)


def test_main_builds_spec_and_reports_change(monkeypatch, capsys) -> None:
    captured: dict = {}
    _stub_runner(monkeypatch, captured)
    monkeypatch.setattr("sys.argv", ["run-playbook", "myapp[pg]", "1.2.3", "--check", "--extra-var", "venv_path=/opt/x/venv"])

    with pytest.raises(SystemExit) as exc:
        run_playbook.main_run_playbook()
    assert exc.value.code == 0

    out = json.loads(capsys.readouterr().out)
    assert out["package"] == "myapp[pg]==1.2.3"
    assert out["changed"] is True
    assert out["check"] is True
    assert out["status"] == "successful"
    assert out["stats"]["changed"] == 1

    assert captured["extravars"]["deploy_package_spec"] == "myapp[pg]==1.2.3"
    assert captured["extravars"]["deploy_package"] == "myapp[pg]"
    assert captured["extravars"]["venv_path"] == "/opt/x/venv"
    assert captured["check"] is True


def test_main_propagates_failure_exit_code(monkeypatch, capsys) -> None:
    captured: dict = {}
    _stub_runner(monkeypatch, captured, status="failed", rc=2, stats={"failures": {"localhost": 1}})
    monkeypatch.setattr("sys.argv", ["run-playbook", "myapp", "9.9.9"])

    with pytest.raises(SystemExit) as exc:
        run_playbook.main_run_playbook()
    assert exc.value.code == 2

    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "failed"
    assert out["changed"] is False
    assert out["stats"]["failed"] == 1


def test_main_reports_bad_extra_var(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.argv", ["run-playbook", "myapp", "1.0.0", "--extra-var", "oops"])
    with pytest.raises(SystemExit) as exc:
        run_playbook.main_run_playbook()
    assert exc.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert "key=value" in out["error"]
