"""
Tests for runspec.finder — config file location.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from runspec.finder import find_config


def test_find_config_finds_in_cwd(tmp_path, monkeypatch):
    (tmp_path / "runspec.toml").touch()
    monkeypatch.chdir(tmp_path)
    assert find_config() == tmp_path / "runspec.toml"


def test_find_config_walks_up(tmp_path, monkeypatch):
    toml = tmp_path / "runspec.toml"
    toml.touch()
    subdir = tmp_path / "a" / "b"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)
    assert find_config() == toml


def test_find_config_with_explicit_start(tmp_path):
    toml = tmp_path / "runspec.toml"
    toml.touch()
    assert find_config(tmp_path) == toml


def test_find_config_raises_when_not_found(tmp_path, monkeypatch):
    # Patch Path.exists so stale runspec.toml files in ancestor dirs don't interfere
    original = Path.exists

    def _exists(self: Path) -> bool:
        if self.name == "runspec.toml" and not self.is_relative_to(tmp_path):
            return False
        return original(self)

    monkeypatch.setattr(Path, "exists", _exists)
    with pytest.raises(FileNotFoundError, match="runspec.toml"):
        find_config(tmp_path)


def test_caller_directory_beats_cwd(tmp_path, monkeypatch):
    """Caller-relative walk wins over cwd walk. Mirrors the installed-package
    case: runspec.toml lives next to the calling module, not in cwd."""
    caller_pkg = tmp_path / "installed" / "mypkg"
    caller_pkg.mkdir(parents=True)
    caller_toml = caller_pkg / "runspec.toml"
    caller_toml.touch()
    caller_file = caller_pkg / "cli.py"
    caller_file.touch()

    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    (cwd_dir / "runspec.toml").touch()  # would-be winner under old behavior
    monkeypatch.chdir(cwd_dir)

    assert find_config(caller=caller_file) == caller_toml


def test_caller_walk_falls_back_to_cwd(tmp_path, monkeypatch):
    """When the caller's tree has no runspec.toml, fall back to the cwd walk."""
    caller_pkg = tmp_path / "elsewhere" / "mypkg"
    caller_pkg.mkdir(parents=True)
    caller_file = caller_pkg / "cli.py"
    caller_file.touch()

    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    cwd_toml = cwd_dir / "runspec.toml"
    cwd_toml.touch()
    monkeypatch.chdir(cwd_dir)

    # Block the caller-walk from finding anything in tmp_path's parents
    original = Path.exists

    def _exists(self: Path) -> bool:
        if self.name == "runspec.toml" and not self.is_relative_to(tmp_path):
            return False
        return original(self)

    monkeypatch.setattr(Path, "exists", _exists)

    assert find_config(caller=caller_file) == cwd_toml
