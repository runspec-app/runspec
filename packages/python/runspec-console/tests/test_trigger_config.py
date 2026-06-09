"""
Tests for trigger persistence (config.read_triggers / write_triggers) and the
[[triggers]] serialiser — verified by round-tripping through tomllib.
"""

from __future__ import annotations

from runspec_console import config
from runspec_console.triggers import Trigger


def _use_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(
        config, "triggers_path", lambda: tmp_path / "runspec_triggers.toml"
    )


def test_read_missing_returns_empty(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    assert config.read_triggers() == []


def test_round_trip_full_trigger(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    triggers = [
        {
            "name": "vip-outage",
            "source": "outlook",
            "event": "new_mail",
            "mode": "agent",
            "enabled": True,
            "autonomy": "confirm",
            "post_to_chat": False,
            "rules": 'Line one.\nLine two with "quotes" and a \\ backslash.',
            "match": {"from": ".*@big\\.com", "subject": "(?i)outage"},
            "reply": {"draft": True, "style": "short"},
        },
        {
            "name": "rule-one",
            "source": "outlook",
            "event": "reminder",
            "mode": "rule",
            "run": "host__do",
            "map": {"svc": "{{ subject }}"},
        },
    ]
    config.write_triggers(triggers)
    back = config.read_triggers()
    assert back == triggers


def test_round_trip_survives_trigger_parsing(monkeypatch, tmp_path):
    """The serialised form must reload into valid Trigger objects."""
    _use_tmp(monkeypatch, tmp_path)
    config.write_triggers(
        [
            {
                "name": "t",
                "source": "outlook",
                "event": "new_mail",
                "match": {"subject": "(?i)urgent"},
                "rules": "Be careful.\nAlways confirm.",
            }
        ]
    )
    triggers = [Trigger.from_config(t) for t in config.read_triggers()]
    assert triggers[0].name == "t"
    assert triggers[0].rules == "Be careful.\nAlways confirm."
    assert triggers[0].match == {"subject": "(?i)urgent"}


def test_write_empty_list(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    config.write_triggers([])
    assert config.read_triggers() == []


def test_categories_list_round_trip(monkeypatch, tmp_path):
    """List scalars (used by future sources) serialise and reload."""
    _use_tmp(monkeypatch, tmp_path)
    config.write_triggers(
        [{"name": "t", "source": "s", "event": "e", "tags": ["a", "b", "c"]}]
    )
    assert config.read_triggers()[0]["tags"] == ["a", "b", "c"]
