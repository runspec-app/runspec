"""stdout_is_terminal() / RunSpec.runspec_text_output — the text-vs-JSON check.

The point of the helper: after parse() the logging tee has replaced sys.stdout
and reports isatty() False, so runnables must ask the real stdout. The pty test
proves that end to end in a child process with logging on.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import runspec
from runspec import terminal
from runspec.testing import ParseHarness


class _Stream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_exported() -> None:
    assert runspec.stdout_is_terminal is terminal.stdout_is_terminal


@pytest.mark.parametrize("tty", [True, False])
def test_reads_real_stdout_not_sys_stdout(monkeypatch: pytest.MonkeyPatch, tty: bool) -> None:
    monkeypatch.setattr(sys, "__stdout__", _Stream(tty))
    monkeypatch.setattr(sys, "stdout", _Stream(not tty))  # what the tee would be
    assert terminal.stdout_is_terminal() is tty


def test_missing_or_closed_stdout_is_not_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "__stdout__", None)
    assert terminal.stdout_is_terminal() is False
    closed = _Stream(True)
    closed.close()

    def boom() -> bool:
        raise ValueError("I/O operation on closed file")

    closed.isatty = boom  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "__stdout__", closed)
    assert terminal.stdout_is_terminal() is False


TOML = """\
[demo]
description = "demo"
"""


@pytest.mark.parametrize(
    ("tty", "agent", "expected"),
    [(True, False, True), (False, False, False), (True, True, False), (False, True, False)],
)
def test_text_output(monkeypatch: pytest.MonkeyPatch, tty: bool, agent: bool, expected: bool) -> None:
    if agent:
        monkeypatch.setenv("RUNSPEC_AGENT", "1")
    else:
        monkeypatch.delenv("RUNSPEC_AGENT", raising=False)
    h = ParseHarness(script_name="demo", toml=TOML)
    try:
        spec = h.expect_ok([])
    finally:
        h.close()
    monkeypatch.setattr(terminal, "stdout_is_terminal", lambda: tty)
    assert spec.runspec_text_output is expected


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pty")
def test_true_at_a_terminal_after_parse_with_logging_on(tmp_path: Path) -> None:
    import pty

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "runspec.toml").write_text('[config.logging]\n\n[demo]\ndescription = "demo"\n')
    (pkg / "demo.py").write_text(
        textwrap.dedent(
            """\
            import json, os, sys
            import runspec
            args = runspec.parse("demo")
            print(json.dumps({
                "tee": type(sys.stdout).__name__,
                "isatty": sys.stdout.isatty(),
                "terminal": runspec.stdout_is_terminal(),
                "text": args.runspec_text_output,
            }), file=open(os.environ["OUT"], "w"))
            """
        )
    )
    out = tmp_path / "out.json"
    env = {k: v for k, v in os.environ.items() if k not in ("RUNSPEC_AGENT", "RUNSPEC_AUDIT")}
    env.update(OUT=str(out), HOME=str(tmp_path), RUNSPEC_INTEGRITY="off")
    primary, secondary = pty.openpty()
    try:
        proc = subprocess.run(
            [sys.executable, str(pkg / "demo.py")],
            stdout=secondary,
            stderr=subprocess.PIPE,
            env=env,
            cwd=tmp_path,
            timeout=60,
        )
    finally:
        os.close(secondary)
        os.close(primary)
    assert proc.returncode == 0, proc.stderr.decode()
    seen = json.loads(out.read_text())
    assert seen["tee"] == "_StdoutTee"  # logging is on, so the tee is in place
    assert seen["isatty"] is False  # ...and it hides the terminal
    assert seen["terminal"] is True
    assert seen["text"] is True
