"""
test_terminal.py — Unit tests for launch_terminal in Bridge.

launch_terminal is fire-and-forget: it builds a PuTTY command line and
spawns putty.exe in a separate window via subprocess.Popen, then returns.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


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
        b._hosts = [
            {
                "name": "myhost",
                "ssh": "user@myhost",
                "runspec_paths": ["/opt/venv/bin/runspec"],
                "identityFile": "C:/Users/jason/.ssh/runspec_ed25519",
            },
            {
                "name": "no-key",
                "ssh": "user@no-key",
                "runspec_paths": ["/opt/venv/bin/runspec"],
            },
        ]
        return b


class TestLaunchTerminal(unittest.TestCase):
    def test_launches_putty_with_ssh_and_identity(self):
        b = _make_bridge()
        putty_path = Path("C:/Program Files/PuTTY/putty.exe")
        idf = "C:/Users/jason/.ssh/runspec_ed25519"
        with (
            patch("runspec_console.bridge._find_putty_exe", return_value=putty_path),
            patch.object(b, "get_config", return_value={"ssh": {"identityFile": idf}}),
            patch("runspec_console.bridge.Path.exists", return_value=True),
            patch("subprocess.Popen") as popen,
        ):
            b.launch_terminal("myhost")
        popen.assert_called_once()
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[0], str(putty_path))
        self.assertIn("-ssh", cmd)
        self.assertIn("myhost", cmd)
        self.assertIn("-l", cmd)
        self.assertIn("user", cmd)
        self.assertIn("-i", cmd)
        # PPK is preferred when it exists; the path has .ppk suffix
        self.assertTrue(any(".ppk" in arg for arg in cmd))

    def test_launches_putty_without_identity_when_none_configured(self):
        b = _make_bridge()
        putty_path = Path("C:/Program Files/PuTTY/putty.exe")
        with (
            patch("runspec_console.bridge._find_putty_exe", return_value=putty_path),
            patch.object(b, "get_config", return_value={"ssh": {}}),
            patch("runspec_console.bridge.Path.exists", return_value=True),
            patch("subprocess.Popen") as popen,
        ):
            b.launch_terminal("no-key")
        cmd = popen.call_args.args[0]
        self.assertNotIn("-i", cmd)
        self.assertIn("no-key", cmd)

    def test_raises_for_unknown_host(self):
        b = _make_bridge()
        with self.assertRaises(ValueError):
            b.launch_terminal("nonexistent")

    def test_raises_for_local_host(self):
        b = _make_bridge()
        with self.assertRaises(ValueError):
            b.launch_terminal("local")

    def test_raises_when_putty_missing(self):
        b = _make_bridge()
        with (
            patch("runspec_console.bridge._find_putty_exe", return_value=None),
            patch("subprocess.Popen") as popen,
        ):
            with self.assertRaises(ValueError) as cm:
                b.launch_terminal("myhost")
        self.assertIn("putty.exe", str(cm.exception))
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
