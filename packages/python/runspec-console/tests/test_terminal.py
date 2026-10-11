"""
test_terminal.py — Unit tests for launch_terminal in Bridge.

launch_terminal is fire-and-forget: it builds a PuTTY command line and
spawns putty.exe in a separate window via subprocess.Popen, then returns.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_bridge():
    """Instantiate Bridge with all external I/O mocked."""
    with (
        patch("runspec_console.hosts.load_hosts", return_value=[]),
        patch("runspec_console.config.hosts_path", return_value=MagicMock()),
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
            patch(
                "runspec_console.bridge_utils._find_putty_exe", return_value=putty_path
            ),
            patch.object(b, "get_config", return_value={"ssh": {"identityFile": idf}}),
            patch("pathlib.Path.exists", return_value=True),
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
            patch(
                "runspec_console.bridge_utils._find_putty_exe", return_value=putty_path
            ),
            patch.object(b, "get_config", return_value={"ssh": {}}),
            patch("pathlib.Path.exists", return_value=True),
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
            patch("runspec_console.bridge_utils._find_putty_exe", return_value=None),
            patch("subprocess.Popen") as popen,
        ):
            with self.assertRaises(ValueError) as cm:
                b.launch_terminal("myhost")
        self.assertIn("putty.exe", str(cm.exception))
        popen.assert_not_called()


class TestLaunchTerminals(unittest.TestCase):
    def _patches(self, b, popen_pid=100):
        putty_path = Path("C:/Program Files/PuTTY/putty.exe")
        pids = iter(range(popen_pid, popen_pid + 50))

        def _popen(cmd):
            proc = MagicMock()
            proc.pid = next(pids)
            return proc

        return (
            patch(
                "runspec_console.bridge_utils._find_putty_exe", return_value=putty_path
            ),
            patch.object(b, "get_config", return_value={"ssh": {}}),
            patch("pathlib.Path.exists", return_value=True),
            patch("subprocess.Popen", side_effect=_popen),
        )

    def test_opens_each_host_and_reports_failures(self):
        b = _make_bridge()
        p1, p2, p3, p4 = self._patches(b)
        with p1, p2, p3, p4 as popen, patch("threading.Thread") as thread:
            res = b.launch_terminals(["myhost", "nonexistent", "no-key", "myhost"])
        self.assertEqual(res["opened"], ["myhost", "no-key"])
        self.assertEqual([e["host"] for e in res["errors"]], ["nonexistent"])
        self.assertEqual(popen.call_count, 2)  # duplicate skipped
        if sys.platform == "win32":
            thread.assert_called_once()
        else:
            self.assertFalse(res["tiling"])
            thread.assert_not_called()

    def test_tile_off_never_starts_tiler(self):
        b = _make_bridge()
        p1, p2, p3, p4 = self._patches(b)
        with p1, p2, p3, p4, patch("threading.Thread") as thread:
            res = b.launch_terminals(["myhost", "no-key"], tile=False)
        self.assertFalse(res["tiling"])
        thread.assert_not_called()

    def test_tiles_on_windows(self):
        b = _make_bridge()
        p1, p2, p3, p4 = self._patches(b, popen_pid=7)
        with (
            p1,
            p2,
            p3,
            p4,
            patch("runspec_console.bridge_internals.sys.platform", "win32"),
            patch("threading.Thread") as thread,
        ):
            res = b.launch_terminals(["myhost", "no-key"])
        self.assertTrue(res["tiling"])
        self.assertEqual(thread.call_args.kwargs["args"], ([7, 8],))
        self.assertEqual(thread.call_args.kwargs["kwargs"], {"layout": "grid"})

    def test_layout_passed_through_and_unknown_falls_back(self):
        for asked, sent in (("columns", "columns"), ("bogus", "grid")):
            b = _make_bridge()
            p1, p2, p3, p4 = self._patches(b, popen_pid=7)
            with (
                p1,
                p2,
                p3,
                p4,
                patch("runspec_console.bridge_internals.sys.platform", "win32"),
                patch("threading.Thread") as thread,
            ):
                b.launch_terminals(["myhost", "no-key"], layout=asked)
            self.assertEqual(thread.call_args.kwargs["kwargs"], {"layout": sent})

    def test_putty_missing_reported_per_host(self):
        b = _make_bridge()
        with (
            patch("runspec_console.bridge_utils._find_putty_exe", return_value=None),
            patch("subprocess.Popen") as popen,
        ):
            res = b.launch_terminals(["myhost", "no-key"])
        self.assertEqual(res["opened"], [])
        self.assertEqual(len(res["errors"]), 2)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TestTerminalThroughJumpHost(unittest.TestCase):
    def _bridge(self):
        b = _make_bridge()
        b._hosts.append(
            {"name": "behind", "ssh": "svc@10.0.0.5", "jump": "ops@bastion:2200"}
        )
        return b

    def test_loads_jump_session_with_plink_proxy_command(self):
        from runspec_console import bridge_utils

        b = self._bridge()
        exe = {
            "putty.exe": Path("C:/Program Files/PuTTY/putty.exe"),
            "plink.exe": Path("C:/Program Files/PuTTY/plink.exe"),
        }
        with (
            patch(
                "runspec_console.bridge_utils._find_putty_exe",
                side_effect=lambda n="putty.exe": exe[n],
            ),
            patch.object(
                b, "get_config", return_value={"ssh": {"identityFile": "C:/k/id"}}
            ),
            patch("pathlib.Path.exists", return_value=True),
            patch(
                "runspec_console.bridge_utils._ensure_putty_jump_session",
                return_value="runspec-console-jump-x",
            ) as ensure,
            patch("subprocess.Popen") as popen,
        ):
            b.launch_terminal("behind")
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[1:3], ["-load", "runspec-console-jump-x"])
        self.assertIn("10.0.0.5", cmd)
        jump, proxy_cmd = ensure.call_args.args
        self.assertEqual(jump, "ops@bastion:2200")
        self.assertIn("-nc %host:%port bastion", proxy_cmd)
        self.assertIn("-l ops", proxy_cmd)
        self.assertIn("-P 2200", proxy_cmd)
        self.assertIn("-batch", proxy_cmd)
        self.assertEqual(
            bridge_utils._putty_jump_session_name("ops@bastion:2200"),
            "runspec-console-jump-ops-bastion-2200",
        )

    def test_missing_plink_is_a_clear_error(self):
        b = self._bridge()
        with (
            patch(
                "runspec_console.bridge_utils._find_putty_exe",
                side_effect=lambda n="putty.exe": (
                    Path("C:/PuTTY/putty.exe") if n == "putty.exe" else None
                ),
            ),
            patch.object(b, "get_config", return_value={"ssh": {}}),
            self.assertRaises(ValueError) as ctx,
        ):
            b.launch_terminal("behind")
        self.assertIn("plink.exe", str(ctx.exception))

    def test_proxy_command_escaping(self):
        from runspec_console.bridge_utils import _putty_jump_proxy_command

        cmd = _putty_jump_proxy_command(
            r"C:\Program Files\PuTTY\plink.exe",
            "bastion",
            user="deploy",
            key_file=r"C:\keys\100%.ppk",
            proxy_session="runspec-console-proxy",
        )
        # PuTTY un-doubles backslashes and %% when it runs the command.
        self.assertTrue(cmd.startswith(r'"C:\\Program Files\\PuTTY\\plink.exe" -batch'))
        self.assertIn(r"C:\\keys\\100%%.ppk", cmd)
        self.assertIn("-load runspec-console-proxy", cmd)
        self.assertIn("-l deploy", cmd)
        self.assertTrue(cmd.endswith("-nc %host:%port bastion"))

    def test_without_jump_keeps_http_proxy_session(self):
        b = _make_bridge()
        with (
            patch(
                "runspec_console.bridge_utils._find_putty_exe",
                return_value=Path("C:/PuTTY/putty.exe"),
            ),
            patch.object(
                b, "get_config", return_value={"ssh": {"proxy": "http://p:8080"}}
            ),
            patch(
                "runspec_console.bridge_internals._ensure_putty_proxy_session",
                return_value="runspec-console-proxy",
            ),
            patch("subprocess.Popen") as popen,
        ):
            b.launch_terminal("no-key")
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[1:3], ["-load", "runspec-console-proxy"])
