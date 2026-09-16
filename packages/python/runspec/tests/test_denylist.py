"""Tests for the venv-root denylist kill switch (denylist.py + parser gate)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from runspec import denylist, errors, parser


@pytest.fixture
def dl_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the denylist at a temp file via the RUNSPEC_DENYLIST_FILE seam."""
    path = tmp_path / "runspec_denylist.toml"
    monkeypatch.setenv("RUNSPEC_DENYLIST_FILE", str(path))
    return path


@pytest.fixture
def greeter_cfg(tmp_path: Path) -> Path:
    cfg = tmp_path / "runspec.toml"
    cfg.write_text('[greeter]\ndescription = "hi"\n[greeter.args]\nname = {type = "str", default = "world"}\n')
    return cfg


def _write(path: Path, **section: object) -> None:
    denylist.write_denylist(dict(section), path)


# ── resolution / loading ────────────────────────────────────────────────────


def test_resolve_path_env_override(dl_path: Path) -> None:
    assert denylist.resolve_denylist_path() == dl_path


def test_load_absent_is_none(dl_path: Path) -> None:
    assert denylist.load_denylist(dl_path) is None


def test_load_malformed_is_none(dl_path: Path) -> None:
    dl_path.write_text("this is = = not toml [")
    assert denylist.load_denylist(dl_path) is None


def test_write_roundtrip(dl_path: Path) -> None:
    _write(dl_path, mode="warn", rule=[{"runnables": ["deploy"], "users": ["svc"]}])
    section = denylist.load_denylist(dl_path)
    assert section is not None
    assert section["mode"] == "warn"
    assert section["rule"][0]["runnables"] == ["deploy"]


# ── matching semantics ──────────────────────────────────────────────────────


def test_no_file_no_match(dl_path: Path) -> None:
    assert denylist.check_denylist("deploy", username="u", hostname="h") is None


def test_disabled_denies_everything(dl_path: Path) -> None:
    _write(dl_path, disabled=True, reason="freeze")
    hit = denylist.check_denylist("anything", username="u", hostname="h")
    assert hit == ("enforce", "freeze")


def test_runnable_glob_match(dl_path: Path) -> None:
    _write(dl_path, rule=[{"runnables": ["deploy-*"]}])
    assert denylist.check_denylist("deploy-prod", username="u", hostname="h")
    assert denylist.check_denylist("backup", username="u", hostname="h") is None


def test_combination_user_and_host(dl_path: Path) -> None:
    _write(dl_path, rule=[{"runnables": ["*"], "users": ["svc-batch"], "hosts": ["prod-*"]}])
    assert denylist.check_denylist("deploy", username="svc-batch", hostname="prod-1")
    # right user, wrong host
    assert denylist.check_denylist("deploy", username="svc-batch", hostname="dev-1") is None
    # right host, wrong user
    assert denylist.check_denylist("deploy", username="alice", hostname="prod-1") is None


def test_omitted_dimension_means_all(dl_path: Path) -> None:
    _write(dl_path, rule=[{"runnables": ["deploy"]}])  # users/hosts omitted ⇒ all
    assert denylist.check_denylist("deploy", username="anyone", hostname="anywhere")


def test_multiple_rules_or_together(dl_path: Path) -> None:
    _write(dl_path, rule=[{"runnables": ["deploy"]}, {"runnables": ["backup"]}])
    assert denylist.check_denylist("deploy", username="u", hostname="h")
    assert denylist.check_denylist("backup", username="u", hostname="h")
    assert denylist.check_denylist("restore", username="u", hostname="h") is None


def test_runspec_self_is_exempt(dl_path: Path) -> None:
    _write(dl_path, disabled=True)
    assert denylist.check_denylist("runspec", username="u", hostname="h") is None


def test_mode_propagates(dl_path: Path) -> None:
    _write(dl_path, mode="warn", disabled=True)
    hit = denylist.check_denylist("deploy", username="u", hostname="h")
    assert hit is not None and hit[0] == "warn"


# ── parser integration ──────────────────────────────────────────────────────


def test_enforce_raises_in_parser(dl_path: Path, greeter_cfg: Path) -> None:
    _write(dl_path, disabled=True, reason="freeze")
    with pytest.raises(SystemExit):
        parser.parse("greeter", argv=[], config_path=greeter_cfg)


def test_enforce_raises_impl(dl_path: Path, greeter_cfg: Path) -> None:
    """_parse_impl raises the typed error (parse() converts it to SystemExit)."""
    _write(dl_path, disabled=True, reason="freeze")
    with pytest.raises(errors.RunnableDenied):
        parser._parse_impl("greeter", argv=[], config_path=greeter_cfg)


def test_warn_does_not_raise(dl_path: Path, greeter_cfg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(dl_path, mode="warn", disabled=True)
    spec = parser.parse("greeter", argv=[], config_path=greeter_cfg)
    assert spec.name.value == "world"
    assert "disabled by the runspec denylist" in capsys.readouterr().err


def test_off_is_noop(dl_path: Path, greeter_cfg: Path) -> None:
    _write(dl_path, mode="off", disabled=True)
    spec = parser.parse("greeter", argv=[], config_path=greeter_cfg)
    assert spec.name.value == "world"


def test_help_works_under_deny(dl_path: Path, greeter_cfg: Path) -> None:
    _write(dl_path, disabled=True)
    with pytest.raises(SystemExit) as exc:
        parser.parse("greeter", argv=["--help"], config_path=greeter_cfg)
    assert exc.value.code == 0


def test_load_spec_not_blocked(dl_path: Path, greeter_cfg: Path) -> None:
    _write(dl_path, disabled=True)
    spec = parser.load_spec("greeter", config_path=greeter_cfg)
    assert spec.name.value == "world"


# ── status() / --json ───────────────────────────────────────────────────────


def test_status_absent(dl_path: Path) -> None:
    info = denylist.status(dl_path)
    assert info["present"] is False
    assert info["mode"] == "enforce"
    assert info["rules"] == []


def test_status_normalises_omitted_dimensions(dl_path: Path) -> None:
    _write(dl_path, mode="warn", rule=[{"runnables": ["deploy"]}])
    info = denylist.status(dl_path)
    assert info["present"] is True
    assert info["mode"] == "warn"
    assert info["rules"] == [{"runnables": ["deploy"], "users": ["*"], "hosts": ["*"], "reason": ""}]


def test_status_disabled_flag(dl_path: Path) -> None:
    _write(dl_path, disabled=True, reason="freeze")
    info = denylist.status(dl_path)
    assert info["disabled"] is True and info["reason"] == "freeze"


def test_cli_list_json(dl_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from runspec import cli

    _write(dl_path, rule=[{"runnables": ["deploy"], "hosts": ["prod-*"]}])
    cli.cmd_deny(["list", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["present"] is True
    assert payload["rules"][0]["runnables"] == ["deploy"]
    assert payload["rules"][0]["hosts"] == ["prod-*"]


# ── CLI: permission denied on a venv root owned by someone else ─────────────

_not_root = pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="chmod-based permission test needs a non-root POSIX user",
)


@_not_root
def test_cli_write_permission_denied_is_explained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A denylist write into a venv root the operator can't write (the venv was
    created as a service account) must be a one-line refusal naming the owner
    and the `sudo -u <owner>` remedy — not a PermissionError traceback."""
    from runspec import cli

    venv = tmp_path / "venv"
    venv.mkdir()
    monkeypatch.setenv("RUNSPEC_DENYLIST_FILE", str(venv / "runspec_denylist.toml"))
    venv.chmod(0o555)
    try:
        with pytest.raises(SystemExit) as exc:
            cli.cmd_deny(["all", "--reason", "change freeze"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "Cannot write the denylist: permission denied" in err
        assert str(venv / "runspec_denylist.toml") in err
        assert f"owned by '{venv.owner()}'" in err
        assert f"sudo -u {venv.owner()}" in err
        assert "deny all --reason 'change freeze'" in err
        assert "Traceback" not in err
    finally:
        venv.chmod(0o755)


@_not_root
def test_cli_clear_permission_denied_is_explained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from runspec import cli

    venv = tmp_path / "venv"
    venv.mkdir()
    path = venv / "runspec_denylist.toml"
    denylist.write_denylist({"disabled": True}, path)
    monkeypatch.setenv("RUNSPEC_DENYLIST_FILE", str(path))
    venv.chmod(0o555)
    try:
        with pytest.raises(SystemExit) as exc:
            cli.cmd_deny(["clear"])
        assert exc.value.code == 1
        assert "permission denied" in capsys.readouterr().err
        assert path.exists()  # nothing was removed
    finally:
        venv.chmod(0o755)


def test_cli_write_still_works_when_writable(dl_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The try/except wrapper must not change the happy path."""
    from runspec import cli

    cli.cmd_deny(["all", "--reason", "freeze"])
    assert "Disabled ALL runnables" in capsys.readouterr().out
    assert denylist.load_denylist(dl_path)["disabled"] is True
    cli.cmd_deny(["clear"])
    assert not dl_path.exists()


def test_cli_permission_denied_message_via_seam(dl_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Platform-independent twin of the chmod test (runs as root / on Windows):
    the write raising PermissionError yields the owner-naming refusal."""
    from runspec import cli

    def denied(section: object, path: object = None) -> None:
        raise PermissionError(13, "Permission denied", str(dl_path))

    monkeypatch.setattr(denylist, "write_denylist", denied)
    monkeypatch.setattr(cli, "_venv_owner", lambda _p: "svc-app")
    with pytest.raises(SystemExit) as exc:
        cli.cmd_deny(["add", "-r", "deploy", "--reason", "change freeze"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Cannot write the denylist: permission denied" in err
    assert "owned by 'svc-app'" in err
    assert "sudo -u svc-app" in err
    assert "deny add -r deploy --reason 'change freeze'" in err
    assert "Permission denied" in err


def test_cli_permission_denied_without_owner(dl_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from runspec import cli

    def denied(section: object, path: object = None) -> None:
        raise PermissionError(13, "Permission denied", str(dl_path))

    monkeypatch.setattr(denylist, "write_denylist", denied)
    monkeypatch.setattr(cli, "_venv_owner", lambda _p: "")
    with pytest.raises(SystemExit):
        cli.cmd_deny(["mode", "warn"])
    err = capsys.readouterr().err
    assert "Re-run as the user that owns the virtual environment" in err
    assert "sudo -u" not in err
