"""runner.py — run a runnable the way ``runspec exec`` does, streaming its output.

Every run goes through core's :func:`runspec.runexec.exec_runnable`, so it is
recorded exactly like ``runspec exec``: a runs-index row (or a logs-inbox file
when you aren't the venv owner), the observed exit code, the output tail in the
run's log, and ``run_as`` applied with the env frame on stdin. The child's stdin
is ``/dev/null``, so it can never read keys from the terminal the TUI owns.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

from runspec.runexec import exec_runnable

LABEL = "tui"
"""The ``runspec exec --label`` every TUI run carries, so ``runspec history list
--label tui`` finds them."""

LineSink = Callable[[str, str], None]
"""``(stream, line)`` — ``stream`` is ``"stdout"`` or ``"stderr"``."""


class _Lines:
    """A text stream (with a binary ``.buffer``, which exec's tee writes) that
    hands complete lines to ``sink``."""

    def __init__(self, stream: str, sink: LineSink) -> None:
        self.stream, self.sink = stream, sink
        self.buffer = self
        self._pending = b""
        self._lock = threading.Lock()

    def write(self, data: bytes | str) -> int:
        chunk = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        with self._lock:
            self._pending += chunk
            *lines, self._pending = self._pending.split(b"\n")
        for line in lines:
            self.sink(self.stream, line.decode("utf-8", errors="replace").rstrip("\r"))
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        with self._lock:
            tail, self._pending = self._pending, b""
        if tail:
            self.sink(self.stream, tail.decode("utf-8", errors="replace").rstrip("\r"))


class Run:
    """One run. :meth:`start` returns at once; ``on_line`` gets each output line
    and ``on_done`` the exit code, both from the run's thread."""

    def __init__(
        self,
        runnable: str,
        argv: list[str],
        env: dict[str, str],
        on_line: LineSink,
        on_done: Callable[[int, float], None],
        executor: Callable[..., int] = exec_runnable,
    ) -> None:
        self.runnable, self.argv = runnable, argv
        self.env = {**env, "PYTHONUNBUFFERED": "1"}  # stream lines as they're printed
        self.on_line, self.on_done = on_line, on_done
        self._executor = executor
        self._proc: subprocess.Popen[bytes] | None = None
        self._cancelled = False
        self._lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.exit_code: int | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name=f"runspec-tui:{self.runnable}", daemon=True)
        self.thread.start()

    def _started(self, proc: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._proc = proc
            stop = self._cancelled
        if stop:
            proc.terminate()

    def _run(self) -> None:
        out, err = _Lines("stdout", self.on_line), _Lines("stderr", self.on_line)
        t0 = time.monotonic()
        try:
            code = self._executor(
                self.runnable,
                self.argv,
                label=LABEL,
                env=self.env,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                on_start=self._started,
            )
        except Exception as exc:  # noqa: BLE001 — report, don't kill the UI
            err.write(f"runspec-tui: could not run {self.runnable}: {exc}\n")
            code = 1
        out.close()
        err.close()
        self.exit_code = code
        self.on_done(code, time.monotonic() - t0)

    def cancel(self, grace: float = 5.0) -> None:
        """Stop the run (SIGTERM, then SIGKILL after ``grace`` seconds). The run
        is still recorded, with the exit code the child ended with."""
        with self._lock:
            self._cancelled = True
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()

        def _reap(p: Any = proc) -> None:
            try:
                p.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                p.kill()

        threading.Thread(target=_reap, daemon=True).start()
