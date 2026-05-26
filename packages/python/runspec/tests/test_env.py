"""
Tests for the .runspec_env file feature (env.py + loader.py prefix guard).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from runspec.env import (
    _parse_dotenv,
    apply_env_file,
    load_env_file,
    make_env_namespace,
    resolve_env_path,
    resolve_env_path_with_source,
)
from runspec.loader import load_raw

# ── _parse_dotenv ─────────────────────────────────────────────────────────────


def test_parse_dotenv_basic(tmp_path: Path) -> None:
    f = tmp_path / ".runspec_env"
    f.write_text("FOO=bar\nBAZ=qux\n")
    assert _parse_dotenv(f) == {"FOO": "bar", "BAZ": "qux"}


def test_parse_dotenv_strips_quotes(tmp_path: Path) -> None:
    f = tmp_path / ".runspec_env"
    f.write_text("KEY=\"hello world\"\nOTHER='quoted'\n")
    assert _parse_dotenv(f) == {"KEY": "hello world", "OTHER": "quoted"}


def test_parse_dotenv_ignores_comments(tmp_path: Path) -> None:
    f = tmp_path / ".runspec_env"
    f.write_text("# comment\nKEY=value\n\n# another\n")
    assert _parse_dotenv(f) == {"KEY": "value"}


def test_parse_dotenv_ignores_lines_without_equals(tmp_path: Path) -> None:
    f = tmp_path / ".runspec_env"
    f.write_text("NOEQUALS\nKEY=value\n")
    assert _parse_dotenv(f) == {"KEY": "value"}


def test_parse_dotenv_missing_file(tmp_path: Path) -> None:
    assert _parse_dotenv(tmp_path / "nonexistent") == {}


def test_parse_dotenv_value_with_equals(tmp_path: Path) -> None:
    f = tmp_path / ".runspec_env"
    f.write_text("URL=postgres://user:pass@host/db?ssl=true\n")
    assert _parse_dotenv(f) == {"URL": "postgres://user:pass@host/db?ssl=true"}


# ── resolve_env_path ──────────────────────────────────────────────────────────


def test_resolve_env_path_default() -> None:
    raw: dict = {}
    path = resolve_env_path(raw, "myscript")
    assert path == Path(sys.prefix) / ".runspec_env"


def test_resolve_env_path_env_var_override(tmp_path: Path) -> None:
    override = str(tmp_path / "override.env")
    with patch.dict(os.environ, {"RUNSPEC_ENV_FILE": override}):
        assert resolve_env_path({}, "myscript") == Path(override)


def test_resolve_env_path_per_runnable(tmp_path: Path) -> None:
    raw = {
        "config": {},
        "runnables": {"myscript": {"runspec_env": str(tmp_path / "per.env")}},
    }
    assert resolve_env_path(raw, "myscript") == tmp_path / "per.env"


def test_resolve_env_path_config_level(tmp_path: Path) -> None:
    raw = {
        "config": {"runspec_env": str(tmp_path / "config.env")},
        "runnables": {},
    }
    assert resolve_env_path(raw, "myscript") == tmp_path / "config.env"


def test_resolve_env_path_per_runnable_wins_over_config(tmp_path: Path) -> None:
    raw = {
        "config": {"runspec_env": str(tmp_path / "config.env")},
        "runnables": {"myscript": {"runspec_env": str(tmp_path / "per.env")}},
    }
    assert resolve_env_path(raw, "myscript") == tmp_path / "per.env"


# ── resolve_env_path_with_source ──────────────────────────────────────────────


def test_resolve_env_path_with_source_default() -> None:
    path, source = resolve_env_path_with_source({}, "myscript")
    assert path == Path(sys.prefix) / ".runspec_env"
    assert "default" in source


def test_resolve_env_path_with_source_env_var(tmp_path: Path) -> None:
    override = str(tmp_path / "override.env")
    with patch.dict(os.environ, {"RUNSPEC_ENV_FILE": override}):
        path, source = resolve_env_path_with_source({}, "myscript")
    assert path == Path(override)
    assert "RUNSPEC_ENV_FILE" in source


def test_resolve_env_path_with_source_per_runnable(tmp_path: Path) -> None:
    raw = {
        "runnables": {"myscript": {"runspec_env": str(tmp_path / "per.env")}},
    }
    _, source = resolve_env_path_with_source(raw, "myscript")
    assert "myscript" in source


def test_resolve_env_path_with_source_config(tmp_path: Path) -> None:
    raw = {"config": {"runspec_env": str(tmp_path / "config.env")}}
    _, source = resolve_env_path_with_source(raw, "myscript")
    assert "config" in source


# ── load_env_file / apply_env_file ────────────────────────────────────────────


def test_load_env_file_returns_values(tmp_path: Path) -> None:
    f = tmp_path / ".runspec_env"
    f.write_text("MY_KEY=myvalue\n")
    with patch.dict(os.environ, {"RUNSPEC_ENV_FILE": str(f)}):
        result = load_env_file({}, "myscript")
    assert result == {"MY_KEY": "myvalue"}


def test_load_env_file_silent_skip_if_absent() -> None:
    with patch.dict(os.environ, {"RUNSPEC_ENV_FILE": "/nonexistent/path/.runspec_env"}):
        result = load_env_file({}, "myscript")
    assert result == {}


def test_apply_env_file_sets_os_environ(tmp_path: Path) -> None:
    f = tmp_path / ".runspec_env"
    f.write_text("TEST_APPLY_KEY=injected\n")
    with patch.dict(os.environ, {"RUNSPEC_ENV_FILE": str(f)}, clear=False):
        os.environ.pop("TEST_APPLY_KEY", None)
        result = apply_env_file({}, "myscript")
        assert os.environ.get("TEST_APPLY_KEY") == "injected"
        assert result == {"TEST_APPLY_KEY": "injected"}
    os.environ.pop("TEST_APPLY_KEY", None)


def test_apply_env_file_existing_vars_win(tmp_path: Path) -> None:
    f = tmp_path / ".runspec_env"
    f.write_text("EXISTING_KEY=file-value\n")
    with patch.dict(os.environ, {"RUNSPEC_ENV_FILE": str(f), "EXISTING_KEY": "original"}, clear=False):
        apply_env_file({}, "myscript")
        assert os.environ["EXISTING_KEY"] == "original"
    os.environ.pop("EXISTING_KEY", None)


# ── make_env_namespace ────────────────────────────────────────────────────────


def test_make_env_namespace_lowercases_keys() -> None:
    ns = make_env_namespace({"MY_API_KEY": "abc123", "DB_URL": "postgres://localhost"})
    assert ns.my_api_key == "abc123"
    assert ns.db_url == "postgres://localhost"


def test_make_env_namespace_empty() -> None:
    ns = make_env_namespace({})
    assert isinstance(ns, SimpleNamespace)


# ── get_runspec_env on RunSpec ────────────────────────────────────────────────


def test_get_runspec_env_via_parse(tmp_path: Path) -> None:
    toml = tmp_path / "pkg" / "runspec.toml"
    toml.parent.mkdir()
    toml.write_text("[myscript]\nautonomy = 'autonomous'\n\n[myscript.args]\n")

    env_file = tmp_path / ".runspec_env"
    env_file.write_text("API_TOKEN=secret123\n")

    from runspec.parser import parse as _parse

    with patch.dict(os.environ, {"RUNSPEC_ENV_FILE": str(env_file)}):
        args = _parse(script_name="myscript", argv=[], config_path=toml)

    ns = args.get_runspec_env()
    assert ns.api_token == "secret123"


def test_get_runspec_env_returns_empty_namespace_when_no_file(tmp_path: Path) -> None:
    toml = tmp_path / "pkg" / "runspec.toml"
    toml.parent.mkdir()
    toml.write_text("[myscript]\nautonomy = 'autonomous'\n\n[myscript.args]\n")

    with patch.dict(os.environ, {"RUNSPEC_ENV_FILE": str(tmp_path / "nonexistent.env")}):
        from runspec.parser import parse as _parse

        args = _parse(script_name="myscript", argv=[], config_path=toml)

    ns = args.get_runspec_env()
    assert isinstance(ns, SimpleNamespace)
    assert vars(ns) == {}


# ── runspec_ prefix reservation in loader ────────────────────────────────────


def test_loader_rejects_runspec_underscore_prefix(tmp_path: Path) -> None:
    toml = tmp_path / "runspec.toml"
    toml.write_text("[myscript]\n\n[myscript.args]\nrunspec_debug = false\n")
    with pytest.raises(ValueError, match="reserved prefix"):
        load_raw(toml)


def test_loader_rejects_runspec_hyphen_prefix(tmp_path: Path) -> None:
    toml = tmp_path / "runspec.toml"
    toml.write_text("[myscript]\n\n[myscript.args]\nrunspec-debug = false\n")
    with pytest.raises(ValueError, match="reserved prefix"):
        load_raw(toml)


def test_loader_allows_normal_arg_names(tmp_path: Path) -> None:
    toml = tmp_path / "runspec.toml"
    toml.write_text("[myscript]\n\n[myscript.args]\ndebug = false\n")
    raw = load_raw(toml)
    assert "debug" in raw["runnables"]["myscript"]["args"]


# ── runspec_env key in loader ─────────────────────────────────────────────────


def test_loader_normalises_runspec_env_in_config(tmp_path: Path) -> None:
    toml = tmp_path / "runspec.toml"
    toml.write_text('[config]\nrunspec_env = "/path/to/.env"\n')
    raw = load_raw(toml)
    assert raw["config"]["runspec_env"] == "/path/to/.env"


def test_loader_normalises_runspec_env_in_runnable(tmp_path: Path) -> None:
    toml = tmp_path / "runspec.toml"
    toml.write_text('[myscript]\nrunspec_env = "/path/to/.env"\n\n[myscript.args]\n')
    raw = load_raw(toml)
    assert raw["runnables"]["myscript"]["runspec_env"] == "/path/to/.env"
