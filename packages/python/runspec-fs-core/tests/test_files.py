"""Read / write / edit helpers against real files under tmp_path."""

from pathlib import Path

import pytest

from runspec_fs_core.errors import EditNotAppliedError
from runspec_fs_core.files import (
    edit_file,
    read_media_file,
    read_multiple_files,
    read_text_file,
    write_file,
)


def test_read_text_file_full(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("one\ntwo\nthree\n")
    out = read_text_file(f)
    assert out["content"] == "one\ntwo\nthree\n"
    assert out["truncated"] is False


def test_read_text_file_head_and_tail(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("l1\nl2\nl3\nl4\n")
    assert read_text_file(f, head=2)["content"] == "l1\nl2\n"
    assert read_text_file(f, head=2)["truncated"] is True
    assert read_text_file(f, tail=1)["content"] == "l4\n"


def test_read_text_file_head_tail_mutually_exclusive(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x\n")
    with pytest.raises(ValueError):
        read_text_file(f, head=1, tail=1)


def test_read_text_file_missing_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_text_file(tmp_path / "nope.txt")


def test_read_media_file(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"\x00\x01\x02")
    out = read_media_file(f)
    assert out["bytes"] == 3
    assert out["base64"] == "AAEC"


def test_read_multiple_files_captures_per_file_errors(tmp_path: Path) -> None:
    good = tmp_path / "good.txt"
    good.write_text("hi")
    out = read_multiple_files([good, tmp_path / "missing.txt"])
    assert out["count"] == 2
    assert out["files"][0] == {"path": str(good), "content": "hi"}
    assert "error" in out["files"][1]


def test_write_file(tmp_path: Path) -> None:
    f = tmp_path / "sub" / "new.txt"
    f.parent.mkdir()
    out = write_file(f, "hello")
    assert f.read_text() == "hello"
    assert out["bytes_written"] == 5


def test_edit_file_applies_ordered_replacements(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("alpha beta gamma")
    out = edit_file(f, [("alpha", "ALPHA"), ("gamma", "GAMMA")])
    assert f.read_text() == "ALPHA beta GAMMA"
    assert out["edits_applied"] == 2
    assert out["diff"]


def test_edit_file_dry_run_does_not_write(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("keep me")
    out = edit_file(f, [("keep", "drop")], dry_run=True)
    assert f.read_text() == "keep me"
    assert out["dry_run"] is True
    assert "drop" in out["diff"]


def test_edit_file_missing_old_text_raises_and_does_not_write(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("original")
    with pytest.raises(EditNotAppliedError):
        edit_file(f, [("absent", "x")])
    assert f.read_text() == "original"
