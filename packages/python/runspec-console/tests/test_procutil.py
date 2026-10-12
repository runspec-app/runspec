"""run_with_deadline: a timeout kills the whole process tree and returns."""

from __future__ import annotations

import sys
import time

from runspec_console.procutil import run_with_deadline


def test_returns_output_and_code():
    code, out, _err = run_with_deadline(
        [sys.executable, "-c", "print('hi'); raise SystemExit(3)"], 10
    )
    assert code == 3 and out.strip() == "hi"


def test_timeout_does_not_wait_for_a_grandchild_holding_the_pipes():
    # Like ssh's ProxyCommand child: it inherits stdout and outlives the parent.
    # subprocess.run(timeout=) on Windows waited for it to close the pipe.
    script = (
        "import subprocess, sys, time;"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        "time.sleep(60)"
    )
    started = time.monotonic()
    code, _out, _err = run_with_deadline([sys.executable, "-c", script], 1)
    assert code is None
    assert time.monotonic() - started < 15
