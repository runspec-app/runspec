"""
Tests for trigger persistence (config.read_triggers / write_triggers) and the
named ``[triggers.<name>]`` serialiser — verified by round-tripping through
tomllib. The named-table form is the only supported shape; the old
``[[triggers]]`` array-of-tables form is rejected (and flagged) on read.
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
    assert config.read_rota() == {}


def test_rota_round_trip(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    rota = {
        "enabled": True,
        "mon": {"on": True, "start": "09:00", "end": "17:30"},
        "sat": {"on": False, "start": "00:00", "end": "00:00"},
    }
    config.write_rota(rota)
    assert config.read_rota() == rota


def test_write_rota_preserves_triggers_and_vice_versa(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    triggers = [{"name": "t", "source": "outlook", "event": "new_mail"}]
    rota = {"enabled": True, "mon": {"on": True, "start": "09:00", "end": "17:00"}}
    # Write triggers first, then rota — each must preserve the other.
    config.write_triggers(triggers)
    config.write_rota(rota)
    assert config.read_triggers() == triggers
    assert config.read_rota() == rota
    # Re-saving triggers keeps the rota intact.
    config.write_triggers(triggers)
    assert config.read_rota() == rota


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


def test_read_named_tables(monkeypatch, tmp_path):
    """A hand-written ``[triggers.<name>]`` file reads, with name from the key."""
    _use_tmp(monkeypatch, tmp_path)
    config.triggers_path().write_text(
        "[triggers.one]\n"
        'source = "outlook"\n'
        'event = "new_mail"\n'
        'mode = "rule"\n'
        'run = "host__do"\n'
        "[triggers.one.match]\n"
        'subject = "(?i)urgent"\n'
        "[triggers.one.map]\n"
        'svc = "{{ subject }}"\n'
        "\n"
        "[triggers.two]\n"
        'source = "outlook"\n'
        'event = "reminder"\n',
        encoding="utf-8",
    )
    trigs = config.read_triggers()
    assert [t["name"] for t in trigs] == ["one", "two"]
    one = trigs[0]
    assert one["source"] == "outlook" and one["run"] == "host__do"
    assert one["match"] == {"subject": "(?i)urgent"}
    assert one["map"] == {"svc": "{{ subject }}"}
    # Reloads into valid Trigger objects — proves named-table grouping survives.
    parsed = [Trigger.from_config(t) for t in trigs]
    assert parsed[0].mode == "rule" and parsed[1].mode == "agent"


def test_table_key_is_authoritative_name(monkeypatch, tmp_path):
    """The table key is the trigger name; an in-table ``name`` is ignored."""
    _use_tmp(monkeypatch, tmp_path)
    config.triggers_path().write_text(
        '[triggers.alias]\nname = "ignored"\nsource = "s"\nevent = "e"\n',
        encoding="utf-8",
    )
    assert config.read_triggers()[0]["name"] == "alias"


def test_legacy_array_of_tables_rejected(monkeypatch, tmp_path):
    """The old ``[[triggers]]`` array form is no longer read, and is flagged."""
    _use_tmp(monkeypatch, tmp_path)
    config.triggers_path().write_text(
        '[[triggers]]\nname = "old"\nsource = "outlook"\nevent = "new_mail"\n',
        encoding="utf-8",
    )
    assert config.read_triggers() == []
    err = config.trigger_file_error()
    assert err is not None and "[[triggers]]" in err and "[triggers.<name>]" in err


def test_write_emits_named_tables(monkeypatch, tmp_path):
    """The serialiser writes the named form, not the legacy array-of-tables."""
    _use_tmp(monkeypatch, tmp_path)
    config.write_triggers([{"name": "vip", "source": "outlook", "event": "new_mail"}])
    text = config.triggers_path().read_text(encoding="utf-8")
    assert '[triggers."vip"]' in text
    assert "[[triggers]]" not in text


def test_duplicate_names_dedupe_on_write(monkeypatch, tmp_path):
    """Two triggers sharing a name still serialise to distinct, valid tables."""
    _use_tmp(monkeypatch, tmp_path)
    config.write_triggers(
        [
            {"name": "dup", "source": "s", "event": "e"},
            {"name": "dup", "source": "s", "event": "f"},
        ]
    )
    back = config.read_triggers()
    assert len(back) == 2
    assert {t["name"] for t in back} == {"dup", "dup-2"}


def test_trigger_file_error_none_when_valid(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    assert config.trigger_file_error() is None  # missing file → no error
    config.write_triggers([{"name": "t", "source": "outlook", "event": "new_mail"}])
    assert config.trigger_file_error() is None  # valid TOML → no error


def test_trigger_file_error_reports_bad_toml(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    # Duplicate key — exactly the kind of breakage one bad trigger causes; it
    # rejects the whole file, so this must be surfaced rather than silently empty.
    config.triggers_path().write_text(
        "[[triggers]]\nname = 'a'\nname = 'b'\n", encoding="utf-8"
    )
    err = config.trigger_file_error()
    assert err is not None and "not valid TOML" in err
    assert config.read_triggers() == []  # whole file unreadable → no triggers


# ── Bridge._clean_trigger (UI dict → on-disk shape) ───────────────────────────


def test_clean_trigger_normalises_and_round_trips(monkeypatch, tmp_path):
    from runspec_console.bridge import Bridge

    _use_tmp(monkeypatch, tmp_path)
    ui = {
        "name": "  password-reset  ",  # the name arg is authoritative + trimmed
        "source": "outlook",
        "event": "new_mail",
        "mode": "agent",
        "enabled": True,
        "autonomy": "confirm",
        "run": "   ",  # blank scalar → dropped
        "rules": "Reset the password.",
        "runnables": ["reset-password", "  ", "request-vm"],
        "match": {"subject": "(?i)password", "from": "  ", "  ": "x"},
        "map": {},  # empty sub-table → dropped
        "reply": {"draft": True, "style": "formal"},
    }
    clean = Bridge._clean_trigger(ui, "password-reset")
    assert clean["name"] == "password-reset"
    assert "run" not in clean  # blank dropped
    assert clean["runnables"] == ["reset-password", "request-vm"]  # blanks dropped
    assert clean["match"] == {"subject": "(?i)password"}  # blank value/key dropped
    assert "map" not in clean  # empty dropped
    assert clean["reply"] == {"draft": True, "style": "formal"}

    # …and it persists + parses back as a valid Trigger.
    config.write_triggers([clean])
    raw = config.read_triggers()
    assert len(raw) == 1
    trig = Trigger.from_config(raw[0])
    assert trig.name == "password-reset" and trig.match == {"subject": "(?i)password"}
    assert trig.reply.draft is True


def test_clean_trigger_once_per_thread_is_tri_state():
    # An explicit bool override is written; "inherit" (omitted / non-bool) is left
    # out so the trigger falls back to the mailbox default.
    from runspec_console.bridge import Bridge

    base = {"name": "t", "source": "outlook", "event": "new_mail"}
    assert "once_per_thread" not in Bridge._clean_trigger(base, "t")  # omitted
    assert (
        Bridge._clean_trigger({**base, "once_per_thread": True}, "t")["once_per_thread"]
        is True
    )
    assert (
        Bridge._clean_trigger({**base, "once_per_thread": False}, "t")[
            "once_per_thread"
        ]
        is False
    )
    # A null/non-bool (the UI's "inherit") is not written.
    assert "once_per_thread" not in Bridge._clean_trigger(
        {**base, "once_per_thread": None}, "t"
    )


def test_clean_trigger_requires_name_via_bridge_guard():
    # save_trigger guards an empty name; _clean_trigger itself just trusts the arg.
    from runspec_console.bridge import Bridge

    out = Bridge._clean_trigger({"source": "outlook"}, "x")
    assert out["name"] == "x" and out["source"] == "outlook"
