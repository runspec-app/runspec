"""
test_config.py — Unit tests for config TOML serialisation.

Focus on round-tripping through tomllib. The custom `_dict_to_toml`
serialiser must produce valid TOML for every value it writes — in
particular Windows paths, which contain backslashes that TOML treats
as escape-sequence starters in basic strings.
"""

from __future__ import annotations

import io
import sys
import unittest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from runspec_console.config import _dict_to_toml, _toml_str


def _roundtrip(data: dict) -> dict:
    """Serialise + parse with tomllib, exactly as write_config / read_config do."""
    text = _dict_to_toml(data)
    return tomllib.load(io.BytesIO(text.encode("utf-8")))


class TestWindowsPathRoundTrip(unittest.TestCase):
    """Regression: SSH binary paths with backslashes were silently lost."""

    def test_top_level_windows_path(self) -> None:
        data = {"binary": r"C:\Program Files\PuTTY\plink.exe"}
        self.assertEqual(_roundtrip(data), data)

    def test_nested_ssh_binary(self) -> None:
        """The real-world case — config.toml has [ssh] binary = "...""."""
        data = {"ssh": {"binary": r"C:\Program Files\PuTTY\plink.exe"}}
        self.assertEqual(_roundtrip(data), data)

    def test_path_with_x86_paren(self) -> None:
        data = {"ssh": {"binary": r"C:\Program Files (x86)\PuTTY\plink.exe"}}
        self.assertEqual(_roundtrip(data), data)

    def test_identity_file_with_backslashes(self) -> None:
        data = {"ssh": {"identityFile": r"C:\Users\jason\.ssh\runspec_ed25519"}}
        self.assertEqual(_roundtrip(data), data)

    def test_double_quote_in_string(self) -> None:
        """A literal " in a value would terminate the string early."""
        data = {"llm": {"model": 'claude-"opus"-4'}}
        self.assertEqual(_roundtrip(data), data)

    def test_mixed_types_preserved(self) -> None:
        data = {
            "ssh": {
                "binary": r"C:\Program Files\PuTTY\plink.exe",
                "user": "jason",
            },
            "llm": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
            },
        }
        self.assertEqual(_roundtrip(data), data)


class TestReadMailboxes(unittest.TestCase):
    """The [mailboxes] shared-mailbox → queue map (the email channel's surface)."""

    def _read(self, raw: dict) -> dict:
        import runspec_console.config as cfg

        orig = cfg.read_config
        cfg.read_config = lambda: raw
        try:
            return cfg.read_mailboxes()
        finally:
            cfg.read_config = orig

    def test_missing_section_is_empty(self) -> None:
        self.assertEqual(self._read({}), {})

    def test_address_lowercased_queue_preserved(self) -> None:
        out = self._read({"mailboxes": {"IT-Support@Corp.com": "it-support"}})
        self.assertEqual(out, {"it-support@corp.com": "it-support"})

    def test_blank_address_or_queue_dropped(self) -> None:
        out = self._read(
            {
                "mailboxes": {
                    "  ": "it-support",
                    "hr@corp.com": "  ",
                    "ok@corp.com": "ok",
                }
            }
        )
        self.assertEqual(out, {"ok@corp.com": "ok"})

    def test_non_table_section_is_empty(self) -> None:
        self.assertEqual(self._read({"mailboxes": "nope"}), {})


class TestTomlStringEscaper(unittest.TestCase):
    def test_plain_string_unchanged_apart_from_quotes(self) -> None:
        self.assertEqual(_toml_str("hello"), '"hello"')

    def test_backslash_escaped(self) -> None:
        self.assertEqual(_toml_str(r"a\b"), r'"a\\b"')

    def test_double_quote_escaped(self) -> None:
        self.assertEqual(_toml_str('a"b'), r'"a\"b"')

    def test_backslash_then_quote(self) -> None:
        # Order matters — escape \ first, then ". Otherwise the \ we inject
        # to escape the " would itself get escaped.
        self.assertEqual(_toml_str(r"a\"b"), r'"a\\\"b"')


if __name__ == "__main__":
    unittest.main()
