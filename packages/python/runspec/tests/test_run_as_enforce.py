"""
test_run_as_enforce.py — the enforce_run_as identity gate.

`run_as` is consumed by an executor (runspec serve / SSH `sudo -u`) *before* the
process starts, so under one the effective user already matches and the check
passes silently. These tests cover the direct/import path, where nothing
escalates: `verify_run_as_identity` (the comparison) and the `enforce_run_as`
reaction wired into the parser (off / warn / error, runnable + [config] default).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from runspec import errors
from runspec.become import verify_run_as_identity
from runspec.parser import _parse_impl, load_spec

posix_only = pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX uid model required")


def _write(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "runspec.toml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


# ── verify_run_as_identity ────────────────────────────────────────────────────


def test_empty_target_is_a_noop():
    # No escalation intended → no identity to assert.
    assert verify_run_as_identity("") is None


@posix_only
def test_current_user_matches():
    import pwd

    current = pwd.getpwuid(os.geteuid()).pw_name
    assert verify_run_as_identity(current) is None


@posix_only
def test_current_uid_matches_by_number():
    # A numeric run_as is taken as a uid directly.
    assert verify_run_as_identity(str(os.geteuid())) is None


@posix_only
def test_nonexistent_user_is_reported():
    detail = verify_run_as_identity("nope-user-xyz")
    assert detail is not None
    assert "does not exist" in detail


@posix_only
def test_uid_mismatch_is_reported(monkeypatch):
    import pwd

    current = pwd.getpwuid(os.geteuid()).pw_name
    # The target resolves to the real current uid; pretend the process runs as a
    # different uid so the comparison fails deterministically.
    monkeypatch.setattr(os, "geteuid", lambda: 999999)
    detail = verify_run_as_identity(current)
    assert detail is not None
    assert "999999" in detail
    assert current in detail


def test_no_posix_uid_model_is_a_noop(monkeypatch):
    # Simulate Windows: no os.geteuid → nothing to compare, never fires.
    monkeypatch.delattr(os, "geteuid", raising=False)
    assert verify_run_as_identity("root") is None


# ── enforce_run_as wired into the parser ──────────────────────────────────────

_BODY = """
[config]
{config_enforce}

[tool]
description = "x"
run_as = "nope-user-xyz"
{enforce}
"""


def _parse(tmp_path, *, enforce="", config_enforce=""):
    cfg = _write(tmp_path, _BODY.format(enforce=enforce, config_enforce=config_enforce))
    return _parse_impl("tool", argv=[], config_path=cfg)


@posix_only
def test_error_is_the_default(tmp_path):
    with pytest.raises(errors.RunAsMismatch) as exc:
        _parse(tmp_path)
    assert "Refusing to run 'tool'" in str(exc.value)


@posix_only
def test_explicit_error(tmp_path):
    with pytest.raises(errors.RunAsMismatch):
        _parse(tmp_path, enforce='enforce_run_as = "error"')


@posix_only
def test_warn_continues_and_writes_stderr(tmp_path, capsys):
    spec = _parse(tmp_path, enforce='enforce_run_as = "warn"')
    assert spec.runspec_runnable == "tool"
    err = capsys.readouterr().err
    assert "⚠" in err
    assert "nope-user-xyz" in err


@posix_only
def test_off_is_silent(tmp_path, capsys):
    spec = _parse(tmp_path, enforce='enforce_run_as = "off"')
    assert spec.runspec_runnable == "tool"
    assert capsys.readouterr().err == ""


@posix_only
def test_config_default_applies(tmp_path, capsys):
    # Runnable omits enforce_run_as → the [config] default wins.
    _parse(tmp_path, config_enforce='enforce_run_as = "warn"')
    assert "⚠" in capsys.readouterr().err


@posix_only
def test_runnable_overrides_config(tmp_path):
    # Runnable "error" overrides a permissive [config] default.
    with pytest.raises(errors.RunAsMismatch):
        _parse(tmp_path, enforce='enforce_run_as = "error"', config_enforce='enforce_run_as = "off"')


def test_no_run_as_passes_for_the_environment_owner(tmp_path):
    # No run_as → "@owner"; the test process owns (or is root over) its venv.
    cfg = _write(tmp_path, '[tool]\ndescription = "x"\n')
    spec = _parse_impl("tool", argv=[], config_path=cfg)
    assert spec.runspec_runnable == "tool"


def test_empty_run_as_means_no_check(tmp_path, monkeypatch):
    monkeypatch.setattr("runspec.become.environment_owner", lambda env_root=None: "nope-user-xyz")
    cfg = _write(tmp_path, '[tool]\ndescription = "x"\nrun_as = ""\n')
    assert _parse_impl("tool", argv=[], config_path=cfg).runspec_runnable == "tool"


# ── the "@owner" default ──────────────────────────────────────────────────────


@posix_only
def test_default_refuses_anyone_but_the_owner(tmp_path, monkeypatch):
    monkeypatch.setattr("runspec.become.environment_owner", lambda env_root=None: "nope-user-xyz")
    cfg = _write(tmp_path, '[tool]\ndescription = "x"\n')
    with pytest.raises(errors.RunAsMismatch) as exc:
        _parse_impl("tool", argv=[], config_path=cfg)
    assert "nope-user-xyz" in str(exc.value)
    assert "owns its environment" in str(exc.value)


@posix_only
def test_config_run_as_empty_opts_the_package_out(tmp_path, monkeypatch):
    monkeypatch.setattr("runspec.become.environment_owner", lambda env_root=None: "nope-user-xyz")
    cfg = _write(tmp_path, '[config]\nrun_as = ""\n\n[tool]\ndescription = "x"\n')
    assert _parse_impl("tool", argv=[], config_path=cfg).runspec_runnable == "tool"


@posix_only
def test_config_run_as_is_the_package_default(tmp_path):
    cfg = _write(tmp_path, '[config]\nrun_as = "nope-user-xyz"\n\n[tool]\ndescription = "x"\n')
    with pytest.raises(errors.RunAsMismatch):
        _parse_impl("tool", argv=[], config_path=cfg)


@posix_only
def test_runnable_run_as_beats_config(tmp_path):
    cfg = _write(tmp_path, '[config]\nrun_as = "nope-user-xyz"\n\n[tool]\ndescription = "x"\nrun_as = ""\n')
    assert _parse_impl("tool", argv=[], config_path=cfg).runspec_runnable == "tool"


@posix_only
def test_owner_default_warns_when_relaxed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("runspec.become.environment_owner", lambda env_root=None: "nope-user-xyz")
    cfg = _write(tmp_path, '[config]\nenforce_run_as = "warn"\n\n[tool]\ndescription = "x"\n')
    _parse_impl("tool", argv=[], config_path=cfg)
    assert "nope-user-xyz" in capsys.readouterr().err


@posix_only
def test_load_spec_skips_the_check(tmp_path):
    # Introspection must never depend on who is running it.
    cfg = _write(tmp_path, _BODY.format(enforce='enforce_run_as = "error"', config_enforce=""))
    spec = load_spec("tool", config_path=cfg)
    assert spec.runspec_runnable == "tool"
