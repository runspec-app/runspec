"""find_large_files / find_large_files_report: size filtering and the mount boundary."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from runspec_linux_core import _mounts, files, find_large_files, find_large_files_report

MB = 1_048_576


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "big.bin").write_bytes(b"x" * (2 * MB))
    (tmp_path / "small.bin").write_bytes(b"x" * 10)
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "huge.bin").write_bytes(b"x" * (3 * MB))
    return tmp_path


def _names(report: dict) -> list[str]:
    return [Path(f["path"]).name for f in report["files"]]


def _mount_other_filesystem(monkeypatch: pytest.MonkeyPatch, target: Path, fstype: str) -> None:
    """Make ``target`` look like the mount point of a separate ``fstype`` filesystem:
    a different device from its parent, listed in the mount table."""
    real_lstat = os.lstat

    def fake_lstat(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        st = real_lstat(path, *args, **kwargs)
        if os.fspath(path) == str(target):
            return SimpleNamespace(st_mode=st.st_mode, st_dev=st.st_dev + 1)
        return st

    monkeypatch.setattr(files.os, "lstat", fake_lstat)
    table = {"/": "ext4", os.path.realpath(target): fstype}
    monkeypatch.setattr(_mounts, "read_mount_types", lambda *a, **k: table)


def test_report_lists_big_files_largest_first(tmp_path: Path) -> None:
    out = find_large_files_report(str(_tree(tmp_path)), min_mb=1)
    assert _names(out) == ["huge.bin", "big.bin"]
    assert out["match_count"] == 2
    assert out["min_mb"] == 1
    assert "skipped_mounts" not in out


def test_limit_keeps_the_match_count(tmp_path: Path) -> None:
    out = find_large_files_report(str(_tree(tmp_path)), min_mb=1, limit=1)
    assert _names(out) == ["huge.bin"]
    assert out["match_count"] == 2


def test_find_large_files_returns_the_file_list(tmp_path: Path) -> None:
    assert [Path(f["path"]).name for f in find_large_files(str(_tree(tmp_path)), 1)] == ["huge.bin", "big.bin"]


def test_symlinked_directories_are_not_followed(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "link").symlink_to(root / "other", target_is_directory=True)
    assert _names(find_large_files_report(str(root), min_mb=1)) == ["huge.bin", "big.bin"]


def test_default_does_not_cross_into_another_filesystem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _tree(tmp_path)
    _mount_other_filesystem(monkeypatch, root / "other", "ext4")
    out = find_large_files_report(str(root), min_mb=1)
    assert _names(out) == ["big.bin"]
    assert out["skipped_mounts"] == 1


def test_cross_mounts_enters_a_real_filesystem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _tree(tmp_path)
    _mount_other_filesystem(monkeypatch, root / "other", "ext4")
    out = find_large_files_report(str(root), min_mb=1, cross_mounts=True)
    assert _names(out) == ["huge.bin", "big.bin"]
    assert "skipped_mounts" not in out


def test_cross_mounts_never_enters_a_virtual_filesystem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _tree(tmp_path)
    _mount_other_filesystem(monkeypatch, root / "other", "proc")
    out = find_large_files_report(str(root), min_mb=1, cross_mounts=True)
    assert _names(out) == ["big.bin"]
    assert out["skipped_mounts"] == 1


def test_bad_paths_raise(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        find_large_files_report(str(tmp_path / "missing"))
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    with pytest.raises(NotADirectoryError):
        find_large_files_report(str(a_file))


@pytest.mark.skipif(not sys.platform.startswith("linux") or not os.path.ismount("/proc"), reason="needs a Linux /proc mount")
def test_a_virtual_root_is_refused() -> None:
    with pytest.raises(ValueError, match="virtual filesystem"):
        find_large_files_report("/proc")
