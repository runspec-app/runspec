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


class TestReadMailboxWatches(unittest.TestCase):
    """The [[mailboxes]] folder-watch list (the email folder-watch channel)."""

    def _read(self, raw: dict) -> list:
        import runspec_console.config as cfg

        orig = cfg.read_config
        cfg.read_config = lambda: raw
        try:
            return cfg.read_mailbox_watches()
        finally:
            cfg.read_config = orig

    def test_missing_is_empty(self) -> None:
        self.assertEqual(self._read({}), [])

    def test_table_form_is_not_a_watch_list(self) -> None:
        # The legacy [mailboxes] *table* (address→queue) is not a watch list.
        self.assertEqual(self._read({"mailboxes": {"a@corp": "q"}}), [])

    def test_empty_entry_is_default_inbox(self) -> None:
        self.assertEqual(
            self._read({"mailboxes": [{}]}),
            [{"mailbox": "", "folder": "Inbox"}],
        )

    def test_inbox_aliases_normalize(self) -> None:
        out = self._read(
            {
                "mailboxes": [
                    {"mailbox": "me@corp", "folder": "inbox"},
                    {"mailbox": "you@corp", "folder": "  "},
                ]
            }
        )
        self.assertEqual(
            out,
            [
                {"mailbox": "me@corp", "folder": "Inbox"},
                {"mailbox": "you@corp", "folder": "Inbox"},
            ],
        )

    def test_subfolder_and_shared_mailbox(self) -> None:
        out = self._read(
            {"mailboxes": [{"mailbox": "it-support@corp", "folder": "Tickets/Open"}]}
        )
        self.assertEqual(
            out, [{"mailbox": "it-support@corp", "folder": "Tickets/Open"}]
        )

    def test_duplicates_collapse(self) -> None:
        out = self._read(
            {
                "mailboxes": [
                    {"folder": "Inbox"},
                    {"folder": "inbox"},  # same watch, different spelling
                    {"mailbox": "ME@corp", "folder": "Tickets"},
                    {"mailbox": "me@corp", "folder": "tickets"},  # same again
                ]
            }
        )
        self.assertEqual(
            out,
            [
                {"mailbox": "", "folder": "Inbox"},
                {"mailbox": "ME@corp", "folder": "Tickets"},
            ],
        )

    def test_non_dict_entries_skipped(self) -> None:
        self.assertEqual(self._read({"mailboxes": ["nope", {}]})[0]["folder"], "Inbox")


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
