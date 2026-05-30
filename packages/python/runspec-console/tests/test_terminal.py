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
        ssh_binary = "C:/Program Files/PuTTY/plink.exe"
        expected_putty = str(Path(ssh_binary).parent / "putty.exe")
        with (
            patch("runspec_console.bridge.Path.exists", return_value=True),
            patch("runspec_console.bridge.Bridge._ssh_binary", return_value=ssh_binary),
            patch("subprocess.Popen") as popen,
        ):
            b.launch_terminal("myhost")
        popen.assert_called_once()
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[0], expected_putty)
        self.assertIn("-ssh", cmd)
        self.assertIn("user@myhost", cmd)
        self.assertIn("-i", cmd)
        self.assertIn("C:/Users/jason/.ssh/runspec_ed25519", cmd)

    def test_launches_putty_without_identity_when_none_configured(self):
        b = _make_bridge()
        with (
            patch("runspec_console.bridge.Path.exists", return_value=True),
            patch(
                "runspec_console.bridge.Bridge._ssh_binary",
                return_value="C:/Program Files/PuTTY/plink.exe",
            ),
            patch("subprocess.Popen") as popen,
        ):
            b.launch_terminal("no-key")
        cmd = popen.call_args.args[0]
        self.assertNotIn("-i", cmd)
        self.assertIn("user@no-key", cmd)

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
            patch("runspec_console.bridge.Path.exists", return_value=False),
            patch(
                "runspec_console.bridge.Bridge._ssh_binary",
                return_value="C:/Program Files/PuTTY/plink.exe",
            ),
            patch("subprocess.Popen") as popen,
        ):
            with self.assertRaises(ValueError) as cm:
                b.launch_terminal("myhost")
        self.assertIn("putty.exe", str(cm.exception))
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
