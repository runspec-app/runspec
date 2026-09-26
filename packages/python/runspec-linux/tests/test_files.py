"""The find-large-files runnable: declared args and the JSON report it prints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import runspec as rs

from runspec_linux import files

TOML = Path(__file__).resolve().parent.parent / "runspec_linux" / "runspec.toml"


def _fixed_parse(argv: list[str]):
    """A real parse of find-large-files bound to ``argv``, for patching ``files.rs.parse``."""
    real = rs.parse
    return lambda *_a, **_k: real("find-large-files", argv, config_path=TOML)


def test_cross_mounts_is_off_by_default() -> None:
    assert rs.parse("find-large-files", ["--path", "/"], config_path=TOML).cross_mounts.value is False
    assert rs.parse("find-large-files", ["--path", "/", "--cross-mounts"], config_path=TOML).cross_mounts.value is True


def test_main_prints_the_report(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    (tmp_path / "big.bin").write_bytes(b"x" * 2_097_152)
    monkeypatch.setattr(files.rs, "parse", _fixed_parse(["--path", str(tmp_path), "--min-mb", "1"]))
    files.main_find_large_files()
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == str(tmp_path)
    assert [Path(f["path"]).name for f in out["files"]] == ["big.bin"]
    assert out["match_count"] == 1


def test_main_missing_path_exits_1(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    monkeypatch.setattr(files.rs, "parse", _fixed_parse(["--path", str(tmp_path / "missing")]))
    with pytest.raises(SystemExit) as exc:
        files.main_find_large_files()
    assert exc.value.code == 1
    assert "error" in json.loads(capsys.readouterr().out)
