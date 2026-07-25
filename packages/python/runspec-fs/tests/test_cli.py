"""Wrapper-only logic (the edit-pairing) plus an end-to-end run of a couple of
verbs through the installed `fs` entry point against tmp_path."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from runspec_fs.cli import _pair_edits


def test_pair_edits_ok() -> None:
    assert _pair_edits(["a", "b"], ["x", "y"]) == [("a", "x"), ("b", "y")]


def test_pair_edits_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="count mismatch"):
        _pair_edits(["a"], ["x", "y"])


def _run_fs(argv: list[str], tmp_path: Path) -> dict:
    """Invoke the runnable in-process via `python -m runspec_fs.cli`, pointing
    runspec at the packaged spec. Returns the parsed JSON stdout."""
    spec = Path(__file__).parent.parent / "runspec_fs" / "runspec.toml"
    env = {**os.environ, "RUNSPEC_CONFIG": str(spec)}
    proc = subprocess.run(
        [sys.executable, "-c", "from runspec_fs.cli import main; main()", *argv],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_list_directory_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi")
    out = _run_fs(["list-directory", "--path", str(tmp_path)], tmp_path)
    names = {e["name"] for e in out["entries"]}
    assert "hello.txt" in names


def test_read_text_file_head_end_to_end(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("l1\nl2\nl3\n")
    out = _run_fs(["read-text-file", "--path", str(f), "--head", "2"], tmp_path)
    assert out["content"] == "l1\nl2\n"
    assert out["truncated"] is True
