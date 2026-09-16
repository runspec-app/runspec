"""
test_config.py — Unit tests for config TOML serialisation.

Focus on round-tripping through tomllib. The serialiser must produce valid TOML
for every value it writes — in particular Windows paths, which contain
backslashes that TOML treats as escape-sequence starters in basic strings.
"""

from __future__ import annotations

import io
import sys
import unittest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import runspec_console.config as c
from runspec_console.config import _toml_str


def _roundtrip(data: dict) -> dict:
    """Serialise + parse with tomllib, exactly as write_config / read_config do."""
    text = c._dumps_config(c._strip_none(data))
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
            {
                "address": "",
                "folders": ["Inbox"],
                "folder": "Inbox",
                "label": "",
                "once_per_thread": False,
            },
        )

    def test_all_fields(self) -> None:
        # Legacy scalar `folder` is read as a one-element folders list.
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
            out,
            {
                "address": "it@corp",
                "folders": ["Tickets/Open"],
                "folder": "Tickets/Open",
                "label": "IT",
                "once_per_thread": False,
            },
        )

    def test_folders_list(self) -> None:
        # The new folders array: normalised, deduped; folder = folders[0].
        out = self._read(
            {
                "mailbox": {
                    "address": "it@corp",
                    "folders": ["Tickets/Open", "inbox", "Tickets/Open", "Alerts"],
                }
            }
        )
        self.assertEqual(out["folders"], ["Tickets/Open", "Inbox", "Alerts"])
        self.assertEqual(out["folder"], "Tickets/Open")

    def test_empty_folders_list_is_inbox(self) -> None:
        out = self._read({"mailbox": {"address": "it@corp", "folders": []}})
        self.assertEqual(out["folders"], ["Inbox"])

    def test_inbox_aliases_normalize(self) -> None:
        self.assertEqual(
            self._read({"mailbox": {"folder": "inbox"}})["folder"], "Inbox"
        )
        self.assertEqual(self._read({"mailbox": {"folder": "  "}})["folder"], "Inbox")

    def test_once_per_thread_default_and_set(self) -> None:
        # Default off; honoured when set (the mailbox-wide reply-suppression default).
        self.assertIs(self._read({"mailbox": {}})["once_per_thread"], False)
        self.assertIs(
            self._read({"mailbox": {"once_per_thread": True}})["once_per_thread"], True
        )


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

    def test_once_per_thread_only_emitted_when_true(self) -> None:
        # Off → omitted (keeps "watch my own Inbox" a clean empty table); on → kept.
        off = self._roundtrip(
            {"address": "", "folder": "Inbox", "label": "", "once_per_thread": False}
        )
        self.assertEqual(off["mailbox"], {})
        on = self._roundtrip(
            {"address": "", "folder": "Inbox", "label": "", "once_per_thread": True}
        )
        self.assertEqual(on["mailbox"], {"once_per_thread": True})

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
            {"address": "it@corp", "folders": ["Tickets/Open"], "label": "IT"},
        )

    def test_folders_list_round_trips(self) -> None:
        out = self._roundtrip(
            {"address": "it@corp", "folders": ["Tickets/Open", "Alerts"]}
        )
        self.assertEqual(out["mailbox"]["folders"], ["Tickets/Open", "Alerts"])
        parsed = _roundtrip(out)
        self.assertEqual(
            parsed["mailbox"],
            {"address": "it@corp", "folders": ["Tickets/Open", "Alerts"]},
        )


class TestReadWatches(unittest.TestCase):
    """The [[watch]] extras the Outlook source monitors on top of [mailbox]."""

    def _read(self, raw: dict):
        import runspec_console.config as cfg

        orig = cfg.read_config
        cfg.read_config = lambda: raw
        try:
            return cfg.read_watches()
        finally:
            cfg.read_config = orig

    def test_absent_is_empty(self) -> None:
        self.assertEqual(self._read({}), [])

    def test_non_list_is_empty(self) -> None:
        self.assertEqual(self._read({"watch": {"address": "x"}}), [])

    def test_empty_entry_is_default_inbox(self) -> None:
        # A bare [[watch]] entry = my own default account's Inbox.
        self.assertEqual(
            self._read({"watch": [{}]}),
            [{"address": "", "folders": ["Inbox"], "folder": "Inbox", "label": ""}],
        )

    def test_fields_and_normalisation(self) -> None:
        out = self._read(
            {
                "watch": [
                    {"address": " a@corp ", "folder": "inbox", "label": " A "},
                    {"address": "b@corp", "folders": ["Tickets/Open", "Alerts"]},
                ]
            }
        )
        self.assertEqual(
            out,
            [
                {
                    "address": "a@corp",
                    "folders": ["Inbox"],
                    "folder": "Inbox",
                    "label": "A",
                },
                {
                    "address": "b@corp",
                    "folders": ["Tickets/Open", "Alerts"],
                    "folder": "Tickets/Open",
                    "label": "",
                },
            ],
        )

    def test_non_dict_entries_skipped(self) -> None:
        self.assertEqual(
            self._read({"watch": ["nope", {}]}),
            [{"address": "", "folders": ["Inbox"], "folder": "Inbox", "label": ""}],
        )


class TestWriteWatches(unittest.TestCase):
    """write_watches replaces the [[watch]] array (or drops it), preserving the rest."""

    def _roundtrip(self, watches, existing: dict | None = None) -> dict:
        import runspec_console.config as cfg

        captured: dict = {}
        orig_read, orig_write = cfg.read_config, cfg.write_config
        cfg.read_config = lambda: dict(existing or {})
        cfg.write_config = lambda data: captured.update(data)
        try:
            cfg.write_watches(watches)
        finally:
            cfg.read_config, cfg.write_config = orig_read, orig_write
        return captured

    def test_empty_drops_section(self) -> None:
        out = self._roundtrip([], existing={"watch": [{"address": "old@corp"}]})
        self.assertNotIn("watch", out)
        self.assertNotIn("watch", self._roundtrip(None, existing={"watch": [{}]}))

    def test_default_inbox_is_empty_table(self) -> None:
        out = self._roundtrip([{"address": "", "folder": "Inbox", "label": ""}])
        self.assertEqual(out["watch"], [{}])

    def test_fields_emitted_and_normalised(self) -> None:
        out = self._roundtrip(
            [{"address": " a@corp ", "folder": "inbox", "label": " A "}]
        )
        # folder "inbox" is the default → dropped; address/label trimmed + kept.
        self.assertEqual(out["watch"], [{"address": "a@corp", "label": "A"}])

    def test_other_sections_preserved(self) -> None:
        out = self._roundtrip(
            [{"address": "a@corp", "folder": "Inbox"}],
            existing={"mailbox": {"address": "shared@corp"}},
        )
        self.assertEqual(out["mailbox"], {"address": "shared@corp"})
        self.assertEqual(out["watch"], [{"address": "a@corp"}])

    def test_round_trips_through_toml(self) -> None:
        out = self._roundtrip(
            [{}, {"address": "b@corp", "folder": "Tickets/Open", "label": "B"}]
        )
        parsed = _roundtrip(out)
        self.assertEqual(
            parsed["watch"],
            [{}, {"address": "b@corp", "folders": ["Tickets/Open"], "label": "B"}],
        )

    def test_folders_list_round_trips(self) -> None:
        out = self._roundtrip(
            [{"address": "b@corp", "folders": ["Tickets/Open", "Escalations"]}]
        )
        parsed = _roundtrip(out)
        self.assertEqual(
            parsed["watch"],
            [{"address": "b@corp", "folders": ["Tickets/Open", "Escalations"]}],
        )


class TestTableArrayLayout(unittest.TestCase):
    """A top-level array-of-tables (``[[watch]]``) must serialise as real
    ``[[key]]`` blocks *after* the ordinary sections — not as an inline
    ``watch = [ { … } ]`` floated to the top of the file (tomli_w's default,
    which reads as out of place above ``[mailbox]``)."""

    def test_watch_emitted_as_table_array_blocks_after_sections(self) -> None:
        text = c._dumps_config(
            c._strip_none(
                {
                    "mailbox": {"address": "help@corp.com"},
                    "watch": [{"address": "me@corp.com", "folder": "Inbox"}],
                }
            )
        )
        self.assertIn("[[watch]]", text)
        self.assertNotIn("watch = [", text)  # not the inline form
        # The array-of-tables comes after the [mailbox] section, not floated above it.
        self.assertLess(text.index("[mailbox]"), text.index("[[watch]]"))

    def test_empty_default_inbox_watch_is_bare_block_not_inline_dict(self) -> None:
        text = c._dumps_config(c._strip_none({"watch": [{}]}))
        self.assertIn("[[watch]]", text)
        self.assertNotIn("{}", text)  # never a bare inline empty table

    def test_multiple_watch_entries_round_trip(self) -> None:
        data = {
            "mailbox": {"address": "h@corp"},
            "watch": [{}, {"address": "b@corp", "label": "B"}],
        }
        self.assertEqual(_roundtrip(data), data)


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


def test_confirm_timeout_defaults_and_overrides(monkeypatch):
    # Defaults: chat 300, headless (trigger/schedule) 3600.
    monkeypatch.setattr(c, "read_config", lambda: {})
    assert c.confirm_timeout(headless=False) == 300.0
    assert c.confirm_timeout(headless=True) == 3600.0
    # Explicit overrides per surface.
    monkeypatch.setattr(
        c,
        "read_config",
        lambda: {"console": {"confirm_timeout": 120, "trigger_confirm_timeout": 900}},
    )
    assert c.confirm_timeout(headless=False) == 120.0
    assert c.confirm_timeout(headless=True) == 900.0
    # 0 means wait indefinitely (surfaced by the bridge as no timeout).
    monkeypatch.setattr(
        c, "read_config", lambda: {"console": {"trigger_confirm_timeout": 0}}
    )
    assert c.confirm_timeout(headless=True) == 0.0


def test_read_mcp_server_defaults(monkeypatch):
    # No [mcp_server] section → off, loopback, default port, no token/floor.
    monkeypatch.setattr(c, "read_config", lambda: {})
    s = c.read_mcp_server()
    assert s == {
        "enabled": False,
        "host": "127.0.0.1",
        "port": c.DEFAULT_MCP_SERVER_PORT,
        "token": None,
        "default_autonomy": None,
        "confirm_wait": 50.0,
        "tool_style": "leaf",
        "expose_builtins": False,
    }


def test_read_mcp_server_overrides_and_validation(monkeypatch):
    monkeypatch.setattr(
        c,
        "read_config",
        lambda: {
            "mcp_server": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 9001,
                "token": "  s3cret ",
                "default_autonomy": "CONFIRM",  # case-insensitive
                "confirm_wait": 25,
                "tool_style": "RUNNABLE",  # case-insensitive
                "expose_builtins": True,
            }
        },
    )
    s = c.read_mcp_server()
    assert s["enabled"] is True
    assert s["port"] == 9001
    assert s["token"] == "s3cret"
    assert s["default_autonomy"] == "confirm"
    assert s["confirm_wait"] == 25.0
    assert s["tool_style"] == "runnable"
    assert s["expose_builtins"] is True

    # Bogus autonomy → dropped (None); bad tool_style → default leaf;
    # out-of-range/garbage port → clamped/default.
    monkeypatch.setattr(
        c,
        "read_config",
        lambda: {
            "mcp_server": {
                "default_autonomy": "yolo",
                "port": 999999,
                "tool_style": "nonsense",
            }
        },
    )
    s = c.read_mcp_server()
    assert s["default_autonomy"] is None
    assert s["port"] == 65535
    assert s["tool_style"] == "leaf"
    monkeypatch.setattr(
        c, "read_config", lambda: {"mcp_server": {"port": "not-a-number"}}
    )
    assert c.read_mcp_server()["port"] == c.DEFAULT_MCP_SERVER_PORT


def test_set_app_dir_override_wins_over_env(tmp_path, monkeypatch):
    """--config-dir (set_app_dir) uses the directory directly and beats
    RUNSPEC_CONSOLE_DIR / APPDATA."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("RUNSPEC_CONSOLE_DIR", str(tmp_path / "env"))
    target = tmp_path / "explicit"
    monkeypatch.setattr(c, "_APP_DIR_OVERRIDE", None)  # ensure clean start
    try:
        c.set_app_dir(target)
        got = c.app_dir()
        assert got == target
        assert got.is_dir()  # created on demand
        # A derived path lands inside the override, not APPDATA/runspec-console.
        assert c.hosts_path() == target / "runspec_hosts.toml"
    finally:
        c.set_app_dir(None)


def test_app_dir_env_var_override(tmp_path, monkeypatch):
    """RUNSPEC_CONSOLE_DIR is honoured (used directly) when no explicit
    override is pinned, and beats APPDATA."""
    monkeypatch.setattr(c, "_APP_DIR_OVERRIDE", None)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("RUNSPEC_CONSOLE_DIR", str(tmp_path / "env"))
    assert c.app_dir() == tmp_path / "env"


def test_app_dir_default_is_appdata_runspec_console(tmp_path, monkeypatch):
    """With no override/env var and no white-label seed package, state lives under
    %APPDATA%/runspec-console — the historic, writable location."""
    monkeypatch.setattr(c, "_APP_DIR_OVERRIDE", None)
    monkeypatch.setattr(c, "_APP_SLUG", None)  # reset the session cache
    monkeypatch.delenv("RUNSPEC_CONSOLE_DIR", raising=False)
    monkeypatch.setattr(c, "_app_slug", lambda: "runspec-console")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert c.app_dir() == tmp_path / "runspec-console"


def test_app_dir_folder_is_named_for_white_label_package(tmp_path, monkeypatch):
    """A white-label package that ships a config seed brands the folder with its
    own dist name (e.g. jason-console), so two different installs are isolated
    under one writable %APPDATA% root — matching the window-title source."""
    monkeypatch.setattr(c, "_APP_DIR_OVERRIDE", None)
    monkeypatch.setattr(c, "_APP_SLUG", None)  # reset the cache
    monkeypatch.delenv("RUNSPEC_CONSOLE_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    # The slug comes from configseed.branding_dist_name (installed metadata).
    from runspec_console import configseed

    monkeypatch.setattr(configseed, "branding_dist_name", lambda: "jason-console")
    assert c.app_dir() == tmp_path / "jason-console"
    assert c.hosts_path() == tmp_path / "jason-console" / "runspec_hosts.toml"


def test_app_slug_is_cached(monkeypatch):
    """_app_slug resolves once and caches — app_dir is hot, so it must not
    re-enumerate entry points on every call."""
    from runspec_console import configseed

    monkeypatch.setattr(c, "_APP_SLUG", None)
    calls = {"n": 0}

    def _count():
        calls["n"] += 1
        return "acme-console"

    monkeypatch.setattr(configseed, "branding_dist_name", _count)
    assert c._app_slug() == "acme-console"
    assert c._app_slug() == "acme-console"
    assert calls["n"] == 1


def test_set_app_dir_none_clears_override(tmp_path, monkeypatch):
    monkeypatch.delenv("RUNSPEC_CONSOLE_DIR", raising=False)
    monkeypatch.setattr(c, "_APP_SLUG", None)
    monkeypatch.setattr(c, "_app_slug", lambda: "runspec-console")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    c.set_app_dir(tmp_path / "explicit")
    c.set_app_dir(None)
    assert c.app_dir() == tmp_path / "runspec-console"


if __name__ == "__main__":
    unittest.main()


def test_app_slug_public_accessor_delegates(monkeypatch):
    """``app_slug()`` (the SSH key marker's source) reads the same cached slug the
    config folder uses, resolved from installed metadata."""
    from runspec_console import configseed

    monkeypatch.setattr(c, "_APP_SLUG", None)
    monkeypatch.setattr(configseed, "branding_dist_name", lambda: "acme-console")
    assert c.app_slug() == "acme-console"
    monkeypatch.setattr(c, "_APP_SLUG", None)
    monkeypatch.setattr(configseed, "branding_dist_name", lambda: None)
    assert c.app_slug() == "runspec-console"
