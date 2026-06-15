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


class TestReadMailbox(unittest.TestCase):
    """The single [mailbox] the Outlook source monitors."""

    def _read(self, raw: dict):
        import runspec_console.config as cfg

        orig = cfg.read_config
        cfg.read_config = lambda: raw
        try:
            return cfg.read_mailbox()
        finally:
            cfg.read_config = orig

    def test_absent_is_none(self) -> None:
        # No [mailbox] table → email monitoring off.
        self.assertIsNone(self._read({}))

    def test_non_dict_is_none(self) -> None:
        self.assertIsNone(self._read({"mailbox": "nope"}))

    def test_empty_table_is_default_inbox(self) -> None:
        # Present-but-empty [mailbox] means "watch my default account's Inbox".
        self.assertEqual(
            self._read({"mailbox": {}}),
            {"address": "", "folder": "Inbox", "label": ""},
        )

    def test_all_fields(self) -> None:
        out = self._read(
            {
                "mailbox": {
                    "address": " it@corp ",
                    "folder": "Tickets/Open",
                    "label": " IT ",
                }
            }
        )
        self.assertEqual(
            out, {"address": "it@corp", "folder": "Tickets/Open", "label": "IT"}
        )

    def test_inbox_aliases_normalize(self) -> None:
        self.assertEqual(
            self._read({"mailbox": {"folder": "inbox"}})["folder"], "Inbox"
        )
        self.assertEqual(self._read({"mailbox": {"folder": "  "}})["folder"], "Inbox")


class TestWriteMailbox(unittest.TestCase):
    """write_mailbox replaces [mailbox] (or drops it), preserving the rest."""

    def _roundtrip(self, mailbox, existing: dict | None = None) -> dict:
        import runspec_console.config as cfg

        captured: dict = {}
        orig_read, orig_write = cfg.read_config, cfg.write_config
        cfg.read_config = lambda: dict(existing or {})
        cfg.write_config = lambda data: captured.update(data)
        try:
            cfg.write_mailbox(mailbox)
        finally:
            cfg.read_config, cfg.write_config = orig_read, orig_write
        return captured

    def test_default_inbox_is_empty_table(self) -> None:
        # "Watch my own Inbox" → a present-but-empty [mailbox].
        out = self._roundtrip({"address": "", "folder": "Inbox", "label": ""})
        self.assertEqual(out["mailbox"], {})

    def test_fields_emitted_and_normalised(self) -> None:
        out = self._roundtrip(
            {"address": " it@corp ", "folder": "inbox", "label": " IT "}
        )
        # folder "inbox" is the default → dropped; address/label trimmed + kept.
        self.assertEqual(out["mailbox"], {"address": "it@corp", "label": "IT"})

    def test_none_drops_the_section(self) -> None:
        out = self._roundtrip(None, existing={"mailbox": {"address": "old@corp"}})
        self.assertNotIn("mailbox", out)

    def test_other_sections_preserved(self) -> None:
        out = self._roundtrip(
            {"address": "a@corp", "folder": "Inbox"},
            existing={"ssh": {"user": "jason"}, "mailbox": {"address": "old@corp"}},
        )
        self.assertEqual(out["ssh"], {"user": "jason"})
        self.assertEqual(out["mailbox"], {"address": "a@corp"})

    def test_round_trips_through_toml(self) -> None:
        out = self._roundtrip(
            {"address": "it@corp", "folder": "Tickets/Open", "label": "IT"}
        )
        parsed = _roundtrip(out)
        self.assertEqual(
            parsed["mailbox"],
            {"address": "it@corp", "folder": "Tickets/Open", "label": "IT"},
        )


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
