"""Unit tests for the `which` runnable's PATH lookup — cross-platform."""

from __future__ import annotations

import os
import stat

import pytest

from runspec_windows.commands import _candidate_files, which


def _make_exec(path):
    path.write_text("@echo off\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_which_finds_executable(tmp_path):
    binp = tmp_path / "mytool"
    _make_exec(binp)
    res = which("mytool", path=str(tmp_path))
    assert res["all_found"] is True
    assert res["path"] == str(tmp_path)
    (r,) = res["results"]
    assert r == {
        "name": "mytool",
        "found": True,
        "path": str(binp),
        "paths": [str(binp)],
    }


def test_which_not_found(tmp_path):
    res = which("nope", path=str(tmp_path))
    assert res["all_found"] is False
    assert res["results"][0] == {
        "name": "nope",
        "found": False,
        "path": None,
        "paths": [],
    }


def test_which_multiple_names_space_separated(tmp_path):
    _make_exec(tmp_path / "a")
    res = which("a b", path=str(tmp_path))
    assert [r["name"] for r in res["results"]] == ["a", "b"]
    assert res["results"][0]["found"] is True
    assert res["results"][1]["found"] is False
    assert res["all_found"] is False


def test_which_all_matches_lists_every_dir(tmp_path):
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    _make_exec(d1 / "dup")
    _make_exec(d2 / "dup")
    search = os.pathsep.join([str(d1), str(d2)])

    first = which("dup", path=search)
    assert first["results"][0]["paths"] == [str(d1 / "dup")]

    allm = which("dup", all_matches=True, path=search)
    assert allm["results"][0]["paths"] == [str(d1 / "dup"), str(d2 / "dup")]


def test_which_empty_raises():
    with pytest.raises(ValueError):
        which("   ")


def test_candidate_files_non_windows():
    # Off Windows (the CI host) a bare name is used as-is.
    assert _candidate_files("python") == ["python"]


def test_candidate_files_windows_pathext(monkeypatch):
    # Simulate Windows (via the _is_nt indirection, not the global os.name) so a
    # bare name expands across PATHEXT and a suffixed name is used verbatim.
    monkeypatch.setattr("runspec_windows.commands._is_nt", lambda: True)
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT")
    assert _candidate_files("python") == ["python.COM", "python.EXE", "python.BAT"]
    assert _candidate_files("setup.exe") == ["setup.exe"]
