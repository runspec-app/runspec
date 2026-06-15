"""
test_config_io.py — config.toml persistence: serialisation, corruption-safety, atomicity.

Regression cover for the bug where the hand-rolled serialiser emitted invalid
TOML for anything beyond one-level-flat sections (arrays-of-tables, nested
tables), which then failed to re-parse — and read_config() silently returned {},
wiping every setting (including [ssh] identityFile → SSH auth then failed).
"""

from __future__ import annotations

import threading

import pytest

import runspec_console.config as c


@pytest.fixture
def cfg_path(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(c, "config_path", lambda: path)
    return path


def test_ssh_identity_round_trips(cfg_path):
    data = {
        "ssh": {
            "user": "deploy",
            "identityFile": "C:/Users/jason/AppData/Roaming/runspec-console/runspec_ed25519",
            "key_created_at": "2026-06-15T08:00:00+00:00",
            "use_ssh_config": True,
        },
        "llm": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
    }
    c.write_config(data)
    assert c.read_config() == data


def test_array_of_tables_round_trips(cfg_path):
    # The exact shape that corrupted the old serialiser (generic — not mailbox).
    data = {
        "ssh": {"identityFile": "C:/keys/id_ed25519"},
        "widgets": [
            {"name": "a", "enabled": True},
            {"name": "b", "enabled": False},
        ],
    }
    c.write_config(data)
    back = c.read_config()
    assert back == data
    # The SSH key must survive even with an array-of-tables in the file.
    assert back["ssh"]["identityFile"] == "C:/keys/id_ed25519"


def test_nested_table_round_trips(cfg_path):
    data = {"section": {"sub": {"k": "v", "n": 3}}, "ssh": {"user": "x"}}
    c.write_config(data)
    assert c.read_config() == data


def test_none_values_dropped(cfg_path):
    c.write_config({"ssh": {"user": "x", "proxy": None}, "empty": None})
    assert c.read_config() == {"ssh": {"user": "x"}}


def test_corrupt_file_is_backed_up_not_silently_wiped(cfg_path):
    # A file the serialiser bug (or a bad hand-edit) could have produced.
    cfg_path.write_text("mailboxes = [{'a': 'b'}]\n", encoding="utf-8")
    assert c.read_config() == {}  # unparseable → empty, app still starts
    backups = list(cfg_path.parent.glob("config.toml.corrupt-*"))
    assert len(backups) == 1  # the bad file was preserved, not lost
    assert not cfg_path.exists()  # moved aside so the next write can't clobber it


def test_update_config_preserves_other_sections(cfg_path):
    c.write_config(
        {"ssh": {"identityFile": "C:/keys/id"}, "llm": {"provider": "anthropic"}}
    )
    c.update_config(lambda cfg: cfg.__setitem__("mailbox", {"address": "x@y.com"}))
    back = c.read_config()
    assert back["ssh"]["identityFile"] == "C:/keys/id"  # untouched
    assert back["llm"]["provider"] == "anthropic"
    assert back["mailbox"] == {"address": "x@y.com"}


def test_update_config_concurrent_no_lost_update(cfg_path):
    c.write_config({"base": {"k": 1}})

    def add(section: str):
        c.update_config(lambda cfg: cfg.__setitem__(section, {"set": True}))

    threads = [threading.Thread(target=add, args=(f"s{i}",)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    back = c.read_config()
    # Every concurrent update's section must be present (none lost).
    assert back["base"] == {"k": 1}
    for i in range(20):
        assert back[f"s{i}"] == {"set": True}


def test_atomic_write_leaves_no_temp_file(cfg_path):
    c.write_config({"ssh": {"user": "x"}})
    leftovers = list(cfg_path.parent.glob("config.toml.tmp*"))
    assert leftovers == []


def test_write_mailbox_preserves_ssh(cfg_path):
    c.write_config({"ssh": {"identityFile": "C:/keys/id"}})
    c.write_mailbox({"address": "help@corp.com", "folder": "Inbox", "label": "Help"})
    back = c.read_config()
    assert back["ssh"]["identityFile"] == "C:/keys/id"
    assert back["mailbox"]["address"] == "help@corp.com"
    assert "folder" not in back["mailbox"]  # "Inbox" is the default → omitted
    # Clearing it drops only [mailbox].
    c.write_mailbox(None)
    back2 = c.read_config()
    assert "mailbox" not in back2
    assert back2["ssh"]["identityFile"] == "C:/keys/id"
