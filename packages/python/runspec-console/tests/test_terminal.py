"""
test_terminal.py — Unit tests for SSH terminal session methods in Bridge.

These tests mock the subprocess layer so no real SSH connection is required.
They verify the full data-flow:
  open_terminal  → spawns process, reader thread dispatches terminal_data events
  terminal_input → base64-decoded bytes written to process stdin
  resize_terminal → no-op (documents the behaviour)
  close_terminal  → terminates process, dispatches terminal_closed event
"""

from __future__ import annotations

import base64
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal Bridge bootstrap — avoids loading pywebview, config files, etc.
# ---------------------------------------------------------------------------


def _make_bridge():
    """Instantiate Bridge with all external I/O mocked."""
    with (
        patch("runspec_console.bridge.load_hosts", return_value=[]),
        patch("runspec_console.bridge.hosts_path", return_value=MagicMock()),
        patch("runspec_console.bridge.Bridge._start_refresh_watcher"),
    ):
        from runspec_console.bridge import Bridge

        b = Bridge()
        b._window = MagicMock()
        b._window.evaluate_js = MagicMock()
        # Remote host for terminal tests
        b._hosts = [
            {
                "name": "myhost",
                "ssh": "user@myhost",
                "runspec_paths": ["/opt/venv/bin/runspec"],
            }
        ]
        return b


class FakeProcess:
    """Simulates a subprocess.Popen backed by real OS pipes (required for os.read).

    By default the write end is kept open so the reader thread doesn't see EOF
    immediately.  Call close_pipe() or terminate() to signal end-of-stream.
    """

    def __init__(self, initial_output: bytes = b""):
        r_fd, self._w_fd = os.pipe()
        if initial_output:
            os.write(self._w_fd, initial_output)
        # Wrap read end as unbuffered binary file (matches how Bridge reads it)
        self.stdout = os.fdopen(r_fd, "rb", buffering=0)
        self.stdin = MagicMock()
        self.returncode = None
        self._terminated = threading.Event()

    def poll(self):
        return self.returncode

    def close_pipe(self):
        """Close write end → reader thread sees EOF."""
        if self._w_fd is not None:
            try:
                os.close(self._w_fd)
            except OSError:
                pass
            self._w_fd = None

    def terminate(self):
        self.returncode = -15
        self._terminated.set()
        self.close_pipe()

    def kill(self):
        self.returncode = -9
        self._terminated.set()
        self.close_pipe()

    def wait(self, timeout=None):
        self._terminated.wait(timeout=timeout)
        return self.returncode

    def cleanup(self):
        """Close any remaining file descriptors."""
        self.close_pipe()
        try:
            self.stdout.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOpenTerminal(unittest.TestCase):
    def test_returns_session_id(self):
        b = _make_bridge()
        proc = FakeProcess()
        try:
            with patch("subprocess.Popen", return_value=proc):
                sid = b.open_terminal("myhost")
            self.assertIsInstance(sid, str)
            self.assertTrue(len(sid) > 0)
        finally:
            proc.cleanup()

    def test_session_registered_while_pipe_open(self):
        """Session stays in _terminals while the remote connection is live."""
        b = _make_bridge()
        proc = FakeProcess()  # write end open → reader thread blocks
        try:
            with patch("subprocess.Popen", return_value=proc):
                sid = b.open_terminal("myhost")
            with b._lock:
                self.assertIn(sid, b._terminals)
                self.assertEqual(b._terminals[sid]["host"], "myhost")
        finally:
            proc.cleanup()

    def test_plink_command_includes_t_flag(self):
        b = _make_bridge()
        captured_cmd = []
        proc = FakeProcess()

        def fake_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return proc

        try:
            with patch("subprocess.Popen", side_effect=fake_popen):
                with patch.object(b, "_ssh_binary", return_value="plink"):
                    b.open_terminal("myhost")
            self.assertIn("-t", captured_cmd)
            self.assertIn("user@myhost", captured_cmd)
        finally:
            proc.cleanup()

    def test_raises_for_unknown_host(self):
        b = _make_bridge()
        with self.assertRaises(ValueError):
            b.open_terminal("nonexistent")

    def test_raises_for_local_host(self):
        b = _make_bridge()
        with self.assertRaises(ValueError):
            b.open_terminal("local")

    def test_reader_thread_dispatches_terminal_data(self):
        b = _make_bridge()
        output_data = b"hello from remote\r\n"
        proc = FakeProcess(initial_output=output_data)
        dispatched: list[dict] = []

        def capture(event, detail):
            dispatched.append({"event": event, "detail": detail})

        b._dispatch = capture

        try:
            with patch("subprocess.Popen", return_value=proc):
                sid = b.open_terminal("myhost")

            # Close pipe → reader thread drains and exits
            proc.close_pipe()
            time.sleep(0.3)

            data_events = [
                d
                for d in dispatched
                if d["event"] == "runspec:terminal_data"
                and d["detail"].get("id") == sid
            ]
            self.assertTrue(len(data_events) > 0)

            all_data = b"".join(
                base64.b64decode(e["detail"]["data"]) for e in data_events
            )
            self.assertEqual(all_data, output_data)
        finally:
            proc.cleanup()

    def test_reader_thread_dispatches_terminal_closed_on_eof(self):
        b = _make_bridge()
        proc = FakeProcess()
        closed_ids: list[str] = []

        def capture(event, detail):
            if event == "runspec:terminal_closed":
                closed_ids.append(detail.get("id", ""))

        b._dispatch = capture

        try:
            with patch("subprocess.Popen", return_value=proc):
                sid = b.open_terminal("myhost")

            proc.close_pipe()
            time.sleep(0.3)

            self.assertIn(sid, closed_ids)
        finally:
            proc.cleanup()

    def test_session_removed_after_eof(self):
        b = _make_bridge()
        proc = FakeProcess()

        try:
            with patch("subprocess.Popen", return_value=proc):
                sid = b.open_terminal("myhost")

            proc.close_pipe()
            time.sleep(0.3)

            with b._lock:
                self.assertNotIn(sid, b._terminals)
        finally:
            proc.cleanup()


class TestTerminalInput(unittest.TestCase):
    def test_writes_decoded_bytes(self):
        b = _make_bridge()
        proc = FakeProcess()

        try:
            with patch("subprocess.Popen", return_value=proc):
                sid = b.open_terminal("myhost")

            data = "hello\r"
            encoded = base64.b64encode(data.encode()).decode()
            b.terminal_input(sid, encoded)

            proc.stdin.write.assert_called_once_with(data.encode())
            proc.stdin.flush.assert_called_once()
        finally:
            proc.cleanup()

    def test_ignores_unknown_session(self):
        b = _make_bridge()
        b.terminal_input("nonexistent_session", base64.b64encode(b"x").decode())

    def test_ignores_dead_process(self):
        b = _make_bridge()
        proc = FakeProcess()
        proc.returncode = 1  # simulate dead process

        try:
            with patch("subprocess.Popen", return_value=proc):
                sid = b.open_terminal("myhost")

            b.terminal_input(sid, base64.b64encode(b"x").decode())
            proc.stdin.write.assert_not_called()
        finally:
            proc.cleanup()

    def test_utf8_multibyte_characters(self):
        b = _make_bridge()
        proc = FakeProcess()

        try:
            with patch("subprocess.Popen", return_value=proc):
                sid = b.open_terminal("myhost")

            raw = "é".encode("utf-8")  # 0xc3 0xa9
            encoded = base64.b64encode(raw).decode()
            b.terminal_input(sid, encoded)

            proc.stdin.write.assert_called_once_with(raw)
        finally:
            proc.cleanup()


class TestResizeTerminal(unittest.TestCase):
    def test_noop_no_exception(self):
        b = _make_bridge()
        b.resize_terminal("any_session", 120, 40)

    def test_noop_with_live_session(self):
        b = _make_bridge()
        proc = FakeProcess()

        try:
            with patch("subprocess.Popen", return_value=proc):
                sid = b.open_terminal("myhost")
            b.resize_terminal(sid, 100, 30)
            proc.stdin.write.assert_not_called()
        finally:
            proc.cleanup()


class TestCloseTerminal(unittest.TestCase):
    def test_terminates_process(self):
        b = _make_bridge()
        proc = FakeProcess()

        try:
            with patch("subprocess.Popen", return_value=proc):
                sid = b.open_terminal("myhost")
            b.close_terminal(sid)
            self.assertIsNotNone(proc.returncode)
        finally:
            proc.cleanup()

    def test_dispatches_terminal_closed(self):
        b = _make_bridge()
        proc = FakeProcess()
        closed: list[str] = []

        def capture(event, detail):
            if event == "runspec:terminal_closed":
                closed.append(detail.get("id", ""))

        b._dispatch = capture

        try:
            with patch("subprocess.Popen", return_value=proc):
                sid = b.open_terminal("myhost")
            b.close_terminal(sid)
            self.assertIn(sid, closed)
        finally:
            proc.cleanup()

    def test_removes_session_from_registry(self):
        b = _make_bridge()
        proc = FakeProcess()

        try:
            with patch("subprocess.Popen", return_value=proc):
                sid = b.open_terminal("myhost")
            b.close_terminal(sid)
            with b._lock:
                self.assertNotIn(sid, b._terminals)
        finally:
            proc.cleanup()

    def test_ignores_unknown_session(self):
        b = _make_bridge()
        b.close_terminal("nonexistent_session_id")

    def test_idempotent_double_close(self):
        b = _make_bridge()
        proc = FakeProcess()

        try:
            with patch("subprocess.Popen", return_value=proc):
                sid = b.open_terminal("myhost")
            b.close_terminal(sid)
            b.close_terminal(sid)  # second call should be silent
        finally:
            proc.cleanup()


if __name__ == "__main__":
    unittest.main()
