"""Directory helpers against real trees under tmp_path."""

from pathlib import Path

import pytest

from runspec_fs_core.dirs import (
    create_directory,
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
