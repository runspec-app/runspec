"""config.validate_config — warn on typo'd keys in the schemaless config.toml
sections ([ssh]/[refresh]) that are read inline with no dedicated reader, so a
misspelled key would otherwise be valid TOML and silently ignored.

Warn-only + additive: a section not in the schema is never checked (forward
compatibility — a newer seed/synced config can't false-warn on an older
console), and a real key never warns.
"""

from __future__ import annotations

import runspec_console.config as cfg


def test_unknown_ssh_key_is_flagged():
    warns = cfg.validate_config({"ssh": {"proxxy": "http://p:8080"}})
    assert len(warns) == 1
    assert "[ssh]" in warns[0] and "proxxy" in warns[0]


def test_unknown_refresh_key_is_flagged():
    warns = cfg.validate_config({"refresh": {"discovery_intervl": 300}})
    assert len(warns) == 1
    assert "[refresh]" in warns[0] and "discovery_intervl" in warns[0]


def test_known_keys_never_warn():
    good = {
        "ssh": {
            "proxy": "http://p:8080",
            "max_concurrent": 5,
            "connect_retries": 1,
            "identityFile": "C:/keys/id",
            "rotate_retries": 2,
            "use_ssh_config": True,
        },
        "refresh": {"workers": 16, "interval": 30, "discovery_interval": 300},
    }
    assert cfg.validate_config(good) == []


def test_unlisted_section_is_not_checked():
    # A section with its own reader (or one we don't schema) is skipped entirely,
    # so this never false-warns on a newer/foreign config.
    assert cfg.validate_config({"web": {"whatever_new_key": 1}}) == []


def test_non_dict_or_absent_sections_are_ignored():
    assert cfg.validate_config({}) == []
    assert cfg.validate_config({"ssh": "not-a-table", "refresh": None}) == []


def test_multiple_unknown_keys_each_reported():
    warns = cfg.validate_config(
        {"ssh": {"proxxy": "x", "keepalve": 30}, "refresh": {"workrs": 8}}
    )
    assert len(warns) == 3
