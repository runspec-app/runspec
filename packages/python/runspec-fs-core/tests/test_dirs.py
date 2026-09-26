"""Directory helpers against real trees under tmp_path."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from runspec_fs_core import _mounts, dirs
from runspec_fs_core.dirs import (
    create_directory,
    directory_size,
    directory_tree,
    list_directory,
    list_directory_with_sizes,
    move_file,
)


def test_create_directory_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b"
    assert create_directory(target)["created"] is True
    assert target.is_dir()
    assert create_directory(target)["created"] is False


def test_list_directory(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "file.txt").write_text("x")
    out = list_directory(tmp_path)
    names = {(e["name"], e["type"]) for e in out["entries"]}
    assert ("sub", "directory") in names
    assert ("file.txt", "file") in names
    assert out["count"] == 2


def test_list_directory_with_sizes_sort_by_size(tmp_path: Path) -> None:
    (tmp_path / "small.txt").write_text("a")
    (tmp_path / "big.txt").write_text("a" * 100)
    out = list_directory_with_sizes(tmp_path, sort_by="size")
    files = [e for e in out["entries"] if e["type"] == "file"]
    assert files[0]["name"] == "big.txt"
    assert out["total_files"] == 2
    assert out["total_size"] == 101


def test_list_directory_with_sizes_bad_sort(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        list_directory_with_sizes(tmp_path, sort_by="nope")


def test_directory_tree_with_exclude(tmp_path: Path) -> None:
    (tmp_path / "keep").mkdir()
    (tmp_path / "keep" / "f.txt").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.txt").write_text("y")
    out = directory_tree(tmp_path, exclude=["node_modules"])
    child_names = {c["name"] for c in out["tree"]["children"]}
    assert "keep" in child_names
    assert "node_modules" not in child_names


def test_move_file_renames(tmp_path: Path) -> None:
    src = tmp_path / "src.txt"
    src.write_text("data")
    dst = tmp_path / "dst.txt"
    out = move_file(src, dst)
    assert not src.exists()
    assert dst.read_text() == "data"
    assert out["destination"] == str(dst)


def test_move_file_refuses_existing_destination(tmp_path: Path) -> None:
    src = tmp_path / "src.txt"
    src.write_text("a")
    dst = tmp_path / "dst.txt"
    dst.write_text("b")
    with pytest.raises(FileExistsError):
        move_file(src, dst)
    assert src.exists()  # nothing moved


def test_directory_size_recursive_totals(tmp_path: Path) -> None:
    # big/ holds 300 bytes across a nested tree; small/ holds 10; loose file 50.
    (tmp_path / "big" / "nested").mkdir(parents=True)
    (tmp_path / "big" / "a.bin").write_bytes(b"x" * 200)
    (tmp_path / "big" / "nested" / "b.bin").write_bytes(b"x" * 100)
    (tmp_path / "small").mkdir()
    (tmp_path / "small" / "c.bin").write_bytes(b"x" * 10)
    (tmp_path / "loose.bin").write_bytes(b"x" * 50)

    out = directory_size(tmp_path)
    by_name = {e["name"]: e for e in out["entries"]}
    assert by_name["big"]["size"] == 300  # recursive, includes nested/
    assert by_name["big"]["type"] == "directory"
    assert by_name["small"]["size"] == 10
    assert by_name["loose.bin"]["size"] == 50
    assert by_name["loose.bin"]["type"] == "file"
    assert out["total_size"] == 360
    # entries are sorted largest first
    assert [e["name"] for e in out["entries"]] == ["big", "loose.bin", "small"]


def test_directory_size_limit_keeps_true_total(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"f{i}.bin").write_bytes(b"x" * (i + 1))
    out = directory_size(tmp_path, limit=2)
    assert len(out["entries"]) == 2
    assert out["entry_count"] == 5
    assert out["entries"][0]["name"] == "f4.bin"  # largest first
    assert out["total_size"] == 1 + 2 + 3 + 4 + 5  # total spans all, not just top 2


def test_directory_size_limit_zero_returns_all(tmp_path: Path) -> None:
    for i in range(3):
        (tmp_path / f"f{i}.bin").write_bytes(b"x")
    out = directory_size(tmp_path, limit=0)
    assert len(out["entries"]) == 3


def test_directory_size_does_not_follow_symlinks(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "big.bin").write_bytes(b"x" * 500)
    link = tmp_path / "link"
    try:
        link.symlink_to(tmp_path / "real", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform/user")
    out = directory_size(tmp_path)
    by_name = {e["name"]: e for e in out["entries"]}
    assert by_name["link"]["type"] == "symlink"
    assert by_name["real"]["size"] == 500
    # the symlink is not walked, so its size does not include the 500-byte target
    assert by_name["link"]["size"] < 500


def test_directory_size_rejects_a_file(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hi")
    with pytest.raises(NotADirectoryError):
        directory_size(f)


def test_directory_size_depth_1_has_no_children_key(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "f.bin").write_bytes(b"x" * 50)
    out = directory_size(tmp_path)  # depth defaults to 1
    a = next(e for e in out["entries"] if e["name"] == "a")
    assert a["size"] == 50  # recursive total
    assert "children" not in a  # depth=1 = flat, backward-compatible shape


def test_directory_size_depth_2_nests_children(tmp_path: Path) -> None:
    (tmp_path / "a" / "big").mkdir(parents=True)
    (tmp_path / "a" / "small").mkdir()
    (tmp_path / "a" / "big" / "f.bin").write_bytes(b"x" * 200)
    (tmp_path / "a" / "small" / "f.bin").write_bytes(b"x" * 10)
    out = directory_size(tmp_path, depth=2)
    a = next(e for e in out["entries"] if e["name"] == "a")
    assert a["size"] == 210
    kids = {c["name"]: c for c in a["children"]}
    assert kids["big"]["size"] == 200 and kids["small"]["size"] == 10
    assert a["children"][0]["name"] == "big"  # each level sorted largest first
    # depth stops here: the grandchildren dirs carry no further breakdown
    assert "children" not in kids["big"]


def test_directory_size_depth_clamped_to_one(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f.bin").write_bytes(b"x" * 5)
    out = directory_size(tmp_path, depth=0)  # clamped up to 1
    a = next(e for e in out["entries"] if e["name"] == "a")
    assert a["size"] == 5 and "children" not in a


def test_directory_size_limit_caps_each_level(tmp_path: Path) -> None:
    (tmp_path / "top").mkdir()
    for i in range(4):
        d = tmp_path / "top" / f"c{i}"
        d.mkdir()
        (d / "f.bin").write_bytes(b"x" * (i + 1))
    out = directory_size(tmp_path, depth=2, limit=2)
    top = next(e for e in out["entries"] if e["name"] == "top")
    assert top["size"] == 1 + 2 + 3 + 4  # total spans all children
    assert len(top["children"]) == 2  # but the breakdown is capped per level
    assert top["children"][0]["name"] == "c3"  # largest kept


# ── Mount boundary ─────────────────────────────────────────────────────────────


def _mount_other_filesystem(monkeypatch: pytest.MonkeyPatch, target: Path, fstype: str) -> None:
    """Make ``target`` look like the mount point of a separate ``fstype`` filesystem:
    a different device from its parent, listed in the mount table."""
    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        st = real_stat(path, *args, **kwargs)
        if os.fspath(path) == str(target):
            return SimpleNamespace(st_mode=st.st_mode, st_dev=st.st_dev + 1)
        return st

    monkeypatch.setattr(dirs.os, "stat", fake_stat)
    table = {"/": "ext4", os.path.realpath(target): fstype}
    monkeypatch.setattr(_mounts, "read_mount_types", lambda *a, **k: table)


def _mounted_tree(tmp_path: Path) -> Path:
    (tmp_path / "local.bin").write_bytes(b"x" * 100)
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "far.bin").write_bytes(b"x" * 1000)
    return tmp_path


def test_directory_size_stays_on_one_filesystem_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _mounted_tree(tmp_path)
    _mount_other_filesystem(monkeypatch, root / "other", "ext4")
    out = directory_size(root)
    assert out["total_size"] == 100
    assert out["skipped_mounts"] == 1
    other = next(e for e in out["entries"] if e["name"] == "other")
    assert other["size"] == 0 and other["skipped_mount"] is True


def test_directory_size_cross_mounts_enters_a_real_filesystem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _mounted_tree(tmp_path)
    _mount_other_filesystem(monkeypatch, root / "other", "nfs4")
    out = directory_size(root, cross_mounts=True)
    assert out["total_size"] == 1100
    assert "skipped_mounts" not in out
    assert all("skipped_mount" not in e for e in out["entries"])


def test_directory_size_never_enters_a_virtual_filesystem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _mounted_tree(tmp_path)
    _mount_other_filesystem(monkeypatch, root / "other", "proc")
    out = directory_size(root, cross_mounts=True)
    assert out["total_size"] == 100
    assert out["skipped_mounts"] == 1


def test_directory_size_counts_mounts_skipped_below_the_breakdown_depth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a").mkdir()
    deep = _mounted_tree(tmp_path / "a")
    _mount_other_filesystem(monkeypatch, deep / "other", "ext4")
    out = directory_size(tmp_path)  # depth 1: "other" sits below the breakdown
    assert out["total_size"] == 100
    assert out["skipped_mounts"] == 1


@pytest.mark.skipif(not sys.platform.startswith("linux") or not os.path.ismount("/proc"), reason="needs a Linux /proc mount")
def test_directory_size_refuses_a_virtual_root() -> None:
    with pytest.raises(ValueError, match="virtual filesystem"):
        directory_size("/proc")


def _deny_scandir(monkeypatch: pytest.MonkeyPatch, denied: Path) -> None:
    """Make ``os.scandir`` refuse ``denied`` the way a root-owned 0700 dir does
    for a normal user (tests may run as root, where chmod wouldn't block)."""
    real = os.scandir

    def fake(path):  # type: ignore[no-untyped-def]
        if Path(path) == denied:
            raise PermissionError(13, "Permission denied", str(path))
        return real(path)

    monkeypatch.setattr(dirs.os, "scandir", fake)


def test_directory_size_flags_unreadable_dirs_with_a_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "f").write_text("x" * 10)
    locked = tmp_path / "var" / "docker"
    locked.mkdir(parents=True)
    (locked / "volume").write_text("x" * 1000)
    _deny_scandir(monkeypatch, locked)
    monkeypatch.setattr(dirs, "_running_as_root", lambda: False)

    out = directory_size(tmp_path, depth=2)

    assert out["skipped"] == 1
    assert out["unreadable"] == [str(locked)]
    assert "lower bound" in out["hint"] and "root" in out["hint"]
    var = next(e for e in out["entries"] if e["name"] == "var")
    # the denied dir is still listed (size a floor of 0) and flagged
    assert var["children"] == [{"name": "docker", "path": str(locked), "type": "directory", "size": 0, "size_mb": 0.0, "unreadable": True}]


def test_directory_size_flags_denied_child_at_top_level(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    locked = tmp_path / "private"
    locked.mkdir()
    _deny_scandir(monkeypatch, locked)
    monkeypatch.setattr(dirs, "_running_as_root", lambda: False)
    out = directory_size(tmp_path)
    rec = next(e for e in out["entries"] if e["name"] == "private")
    assert rec["unreadable"] is True and rec["size"] == 0


def test_directory_size_no_hint_as_root_or_when_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "f").write_text("x")
    clean = directory_size(tmp_path)
    assert "hint" not in clean and "unreadable" not in clean and "skipped" not in clean

    locked = tmp_path / "private"
    locked.mkdir()
    _deny_scandir(monkeypatch, locked)
    monkeypatch.setattr(dirs, "_running_as_root", lambda: True)
    as_root = directory_size(tmp_path)
    assert as_root["skipped"] == 1 and as_root["unreadable"] == [str(locked)]
    assert "hint" not in as_root


def test_directory_size_caps_the_unreadable_sample(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(dirs.UNREADABLE_SAMPLE + 5):
        (tmp_path / f"d{i:02d}").mkdir()
    monkeypatch.setattr(dirs.os, "scandir", _raising_for_children(tmp_path))
    monkeypatch.setattr(dirs, "_running_as_root", lambda: False)
    out = directory_size(tmp_path)
    assert out["skipped"] == dirs.UNREADABLE_SAMPLE + 5
    assert len(out["unreadable"]) == dirs.UNREADABLE_SAMPLE


def _raising_for_children(root: Path):  # type: ignore[no-untyped-def]
    real = os.scandir

    def fake(path):  # type: ignore[no-untyped-def]
        if Path(path) != root:
            raise PermissionError(13, "Permission denied", str(path))
        return real(path)

    return fake
