"""search_files + get_file_info / allowed_directories against tmp_path."""

from pathlib import Path

from runspec_fs_core.info import allowed_directories, get_file_info
from runspec_fs_core.search import search_files


def test_search_files_substring_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / "Report.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "monthly_report.md").write_text("y")
    (tmp_path / "other.log").write_text("z")
    out = search_files(tmp_path, "report")
    found = {Path(m).name for m in out["matches"]}
    assert found == {"Report.txt", "monthly_report.md"}
    assert out["count"] == 2


def test_search_files_glob(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    out = search_files(tmp_path, "*.py")
    assert {Path(m).name for m in out["matches"]} == {"a.py"}


def test_search_files_prunes_excluded_dirs(tmp_path: Path) -> None:
    (tmp_path / "keep").mkdir()
    (tmp_path / "keep" / "match.txt").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "match.txt").write_text("y")
    out = search_files(tmp_path, "match", exclude=["node_modules"])
    assert all("node_modules" not in m for m in out["matches"])
    assert out["count"] == 1


def test_get_file_info(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("hello")
    info = get_file_info(f)
    assert info["type"] == "file"
    assert info["size"] == 5
    assert info["is_file"] is True
    assert info["permissions"].startswith("0o")


def test_allowed_directories_is_informational() -> None:
    out = allowed_directories()
    assert "user" in out and "home" in out and "cwd" in out
    assert "jail" in out["note"].lower()
