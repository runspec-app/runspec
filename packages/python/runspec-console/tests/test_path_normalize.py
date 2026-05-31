"""
test_path_normalize.py — Tests for backslash → forward-slash normalisation
on the bridge's save paths.

Goal: paths written through the bridge land in TOML with forward slashes,
regardless of whether they came from the native file picker (Windows
backslashes) or were typed manually in either style. Combined with the
`_dict_to_toml` escaping fix, this means config.toml never has to
double-escape `\\` for Windows paths.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from runspec_console.bridge import (
    _normalize_path,
    _normalize_ssh_section,
)


class TestNormalizePath(unittest.TestCase):
    def test_backslashes_to_forward_slashes(self) -> None:
        self.assertEqual(
            _normalize_path(r"C:\Program Files\PuTTY\plink.exe"),
            "C:/Program Files/PuTTY/plink.exe",
        )

    def test_already_forward_slash_unchanged(self) -> None:
        self.assertEqual(
            _normalize_path("C:/Program Files/PuTTY/plink.exe"),
            "C:/Program Files/PuTTY/plink.exe",
        )

    def test_mixed_separators_normalised(self) -> None:
        self.assertEqual(
            _normalize_path(r"C:\Users\jason/.ssh\runspec_ed25519"),
            "C:/Users/jason/.ssh/runspec_ed25519",
        )

    def test_empty_string_passthrough(self) -> None:
        self.assertEqual(_normalize_path(""), "")

    def test_non_string_passthrough(self) -> None:
        # Non-string values (None, ints) come through unchanged so the
        # helper is safe to apply unconditionally to dict values.
        self.assertIsNone(_normalize_path(None))  # type: ignore[arg-type]


class TestNormalizeSshSection(unittest.TestCase):
    def test_binary_and_identity_file_normalised(self) -> None:
        section = {
            "binary": r"C:\Program Files\PuTTY\plink.exe",
            "identityFile": r"C:\Users\jason\.ssh\runspec_ed25519",
            "user": "jason",  # non-path field — must be untouched
        }
        out = _normalize_ssh_section(section)
        self.assertEqual(out["binary"], "C:/Program Files/PuTTY/plink.exe")
        self.assertEqual(out["identityFile"], "C:/Users/jason/.ssh/runspec_ed25519")
        self.assertEqual(out["user"], "jason")

    def test_missing_path_fields_no_crash(self) -> None:
        self.assertEqual(_normalize_ssh_section({}), {})
        self.assertEqual(_normalize_ssh_section({"user": "jason"}), {"user": "jason"})

    def test_input_not_mutated(self) -> None:
        section = {"binary": r"C:\Program Files\PuTTY\plink.exe"}
        _normalize_ssh_section(section)
        self.assertEqual(section["binary"], r"C:\Program Files\PuTTY\plink.exe")


def _make_bridge():
    """Construct a Bridge with the I/O dependencies stubbed out."""
    with (
        patch("runspec_console.bridge.load_hosts", return_value=[]),
        patch.object(
            __import__("runspec_console.bridge", fromlist=["Bridge"]).Bridge,
            "_start_refresh_watcher",
        ),
    ):
        from runspec_console.bridge import Bridge

        return Bridge()


class TestSaveConfigNormalisesPaths(unittest.TestCase):
    def test_save_config_writes_forward_slashes(self) -> None:
        b = _make_bridge()
        with patch("runspec_console.bridge.write_config") as wc:
            b.save_config(
                {
                    "ssh": {
                        "binary": r"C:\Program Files\PuTTY\plink.exe",
                        "identityFile": r"C:\Users\jason\.ssh\runspec_ed25519",
                        "user": "jason",
                    },
                    "llm": {"provider": "anthropic"},
                }
            )
        wc.assert_called_once()
        written = wc.call_args[0][0]
        self.assertEqual(written["ssh"]["binary"], "C:/Program Files/PuTTY/plink.exe")
        self.assertEqual(
            written["ssh"]["identityFile"], "C:/Users/jason/.ssh/runspec_ed25519"
        )
        # Non-path fields untouched.
        self.assertEqual(written["ssh"]["user"], "jason")
        self.assertEqual(written["llm"]["provider"], "anthropic")

    def test_save_config_no_ssh_section_unaffected(self) -> None:
        b = _make_bridge()
        with patch("runspec_console.bridge.write_config") as wc:
            b.save_config({"llm": {"provider": "anthropic"}})
        wc.assert_called_once_with({"llm": {"provider": "anthropic"}})


class TestSaveJumpHostsNormalisesPaths(unittest.TestCase):
    def test_identity_file_and_runspec_paths_normalised(self) -> None:
        b = _make_bridge()
        with (
            patch("runspec_console.bridge.save_hosts") as sh,
            patch.object(b, "_reload_hosts"),
            patch("threading.Thread"),
        ):
            b.save_jump_hosts(
                [
                    {
                        "name": "prod-1",
                        "hostname": "host1.example.com",
                        "user": "jason",
                        "identityFile": r"C:\Users\jason\.ssh\runspec_ed25519",
                        "runspec_paths": ["/home/jason/.venv/bin/runspec"],
                    }
                ]
            )
        sh.assert_called_once()
        entries = sh.call_args[0][1]
        self.assertEqual(
            entries[0]["identityFile"], "C:/Users/jason/.ssh/runspec_ed25519"
        )
        # Remote POSIX paths are already forward-slash so untouched.
        self.assertEqual(entries[0]["runspec_paths"], ["/home/jason/.venv/bin/runspec"])


class TestBrowseSshBinaryNormalises(unittest.TestCase):
    def test_returns_forward_slash_path(self) -> None:
        b = _make_bridge()
        fake_window = MagicMock()
        fake_window.create_file_dialog.return_value = (
            r"C:\Program Files\PuTTY\plink.exe",
        )
        fake_webview = MagicMock(windows=[fake_window], OPEN_DIALOG="OPEN")
        with patch.dict("sys.modules", {"webview": fake_webview}):
            result = b.browse_ssh_binary()
        self.assertEqual(result, "C:/Program Files/PuTTY/plink.exe")

    def test_empty_when_cancelled(self) -> None:
        b = _make_bridge()
        fake_window = MagicMock()
        fake_window.create_file_dialog.return_value = None
        fake_webview = MagicMock(windows=[fake_window], OPEN_DIALOG="OPEN")
        with patch.dict("sys.modules", {"webview": fake_webview}):
            self.assertEqual(b.browse_ssh_binary(), "")


if __name__ == "__main__":
    unittest.main()
