"""The `filesystem` runnable folded into runspec-linux (backed by runspec-fs-core).

Smoke coverage that the folded verbs run in-process and that the run-as worker
path builds the right escalation command. Full verb behaviour is covered by
runspec-fs-core's own suite.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from runspec_linux.fs import _dispatch, cli


def test_dispatch_read_and_list(tmp_path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("hello\nworld\n")
    assert _dispatch.run_verb("read-text-file", {"path": str(f)})["content"] == "hello\nworld\n"
    listing = _dispatch.run_verb("list-directory", {"path": str(tmp_path)})
    assert listing["count"] == 1
    assert listing["entries"][0]["name"] == "a.txt"


def test_dispatch_directory_size(tmp_path) -> None:
    (tmp_path / "big").mkdir()
    (tmp_path / "big" / "a.bin").write_bytes(b"x" * 100)
    (tmp_path / "tiny.bin").write_bytes(b"x")
    out = _dispatch.run_verb("directory-size", {"path": str(tmp_path), "limit": 20, "depth": 1})
    by_name = {e["name"]: e for e in out["entries"]}
    assert by_name["big"]["size"] == 100  # recursive folder total
    assert out["entries"][0]["name"] == "big"  # largest first
    assert out["total_size"] == 101


def test_dispatch_unknown_verb() -> None:
    with pytest.raises(ValueError):
        _dispatch.run_verb("frobnicate", {})


def test_pair_edits_mismatch() -> None:
    with pytest.raises(ValueError, match="count mismatch"):
        cli._pair_edits(["a", "b"], ["x"])


def test_run_as_worker_builds_sudo_command(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, input=None, capture_output=None, text=None):  # noqa: A002
        captured["argv"] = argv
        captured["input"] = input
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    out = cli._run_as_worker("svc", "sudo", "list-directory", {"path": "/tmp"})
    argv = captured["argv"]
    assert argv[:4] == ["sudo", "-n", "-u", "svc"]
    assert argv[-2:] == ["-m", "runspec_linux.fs._worker"]
    assert json.loads(captured["input"]) == {"verb": "list-directory", "kwargs": {"path": "/tmp"}}
    assert json.loads(out) == {"ok": True}
