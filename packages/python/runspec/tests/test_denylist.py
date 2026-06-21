"""Tests for the venv-root denylist kill switch (denylist.py + parser gate)."""

from __future__ import annotations

import json
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
