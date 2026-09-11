"""The `filesystem` runnable folded into runspec-windows (backed by runspec-fs-core).

Verbs run in-process and cross-platform; the ``--run-as`` worker path is
POSIX-only and raises a clear error where escalation is unsupported (Windows).
"""

from __future__ import annotations

import pytest

from runspec_windows.fs import _dispatch, cli


def test_dispatch_read_and_list(tmp_path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("hello\n")
    assert _dispatch.run_verb("read-text-file", {"path": str(f)})["content"] == "hello\n"
    assert _dispatch.run_verb("list-directory", {"path": str(tmp_path)})["count"] == 1


def test_run_as_unsupported_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate a non-POSIX host (Windows): escalation is unsupported, so a
    # --run-as request is a clear error rather than a silent in-process run.
    monkeypatch.setattr(cli, "escalation_supported", lambda: False)
    with pytest.raises(cli.BecomeError, match="POSIX"):
        cli._run_as_worker("svc", "sudo", "list-directory", {"path": "/tmp"})
