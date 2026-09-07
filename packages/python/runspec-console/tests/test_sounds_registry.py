"""Tests for sounds_registry — the named-sound registry (runspec_sounds.toml).

Pure/OS-independent: parsing, id validation/derivation, and the load/save
round-trip against a temp registry path. Playback is exercised elsewhere (it is a
Windows-only no-op).
"""

from __future__ import annotations

import pytest

from runspec_console import config, sounds_registry


@pytest.fixture
def registry_path(tmp_path, monkeypatch):
    path = tmp_path / "runspec_sounds.toml"
    monkeypatch.setattr(config, "sounds_registry_path", lambda: path)
    return path


# ── validation / parsing ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sid,ok",
    [
        ("deploy-done", True),
        ("chime_2", True),
        ("A1", True),
        ("", False),
        ("-leading", False),  # must start alnum
        ("has space", False),
        ("bad!", False),
        ("x" * 65, False),  # too long
        (42, False),
    ],
)
def test_valid_id(sid, ok):
    assert sounds_registry.valid_id(sid) is ok


def test_parse_sound_ok():
    s = sounds_registry.parse_sound(
        {"id": "deploy-done", "label": "Deploy done", "file": "C:/s/deploy.mp3"}
    )
    assert (s.id, s.label, s.file) == ("deploy-done", "Deploy done", "C:/s/deploy.mp3")


@pytest.mark.parametrize(
    "data,msg",
    [
        ({"file": "a.wav"}, "missing an 'id'"),
        ({"id": "bad id", "file": "a.wav"}, "invalid"),
        ({"id": "ok"}, "missing a 'file'"),
        ({"id": "ok", "file": "clip.ogg"}, "must be a"),
    ],
)
def test_parse_sound_rejects(data, msg):
    with pytest.raises(ValueError, match=msg):
        sounds_registry.parse_sound(data)


def test_slugify_and_unique_id():
    assert sounds_registry.slugify("Deploy Done!") == "deploy-done"
    assert sounds_registry.slugify("  ") == "sound"  # empty → stable base
    assert sounds_registry.unique_id("Deploy Done", set()) == "deploy-done"
    assert sounds_registry.unique_id("Deploy Done", {"deploy-done"}) == "deploy-done-2"
    assert (
        sounds_registry.unique_id("Deploy Done", {"deploy-done", "deploy-done-2"})
        == "deploy-done-3"
    )


# ── I/O round-trip ────────────────────────────────────────────────────────────


def test_save_and_load_roundtrip(registry_path):
    rows = [
        {"id": "chime", "label": "Chime", "file": "/x/chime.wav"},
        {"id": "alarm", "label": "Alarm", "file": "/x/alarm.mp3"},
    ]
    sounds_registry.save_rows(rows)
    assert registry_path.exists()
    assert sounds_registry.load_rows() == rows
    assert [s.id for s in sounds_registry.list_sounds()] == ["chime", "alarm"]
    assert sounds_registry.get_sound("alarm").file == "/x/alarm.mp3"
    assert sounds_registry.get_sound("nope") is None
    assert sounds_registry.registry_map() == {
        "chime": "/x/chime.wav",
        "alarm": "/x/alarm.mp3",
    }


def test_load_absent_is_empty(registry_path):
    assert sounds_registry.load_rows() == []
    assert sounds_registry.list_sounds() == []
    assert sounds_registry.registry_map() == {}


def test_list_sounds_skips_malformed(registry_path):
    sounds_registry.save_rows(
        [
            {"id": "good", "label": "Good", "file": "/x/g.wav"},
            {"id": "bad id", "file": "/x/b.wav"},  # invalid id
            {"id": "noext", "file": "/x/n.ogg"},  # bad ext
        ]
    )
    # load_rows returns everything (raw); list_sounds drops the malformed ones.
    assert len(sounds_registry.load_rows()) == 3
    assert [s.id for s in sounds_registry.list_sounds()] == ["good"]


def test_exists_reflects_disk(tmp_path):
    present = tmp_path / "c.wav"
    present.write_bytes(b"x")
    assert sounds_registry.Sound(id="c", file=str(present)).exists() is True
    assert (
        sounds_registry.Sound(id="m", file=str(tmp_path / "gone.wav")).exists() is False
    )
    assert sounds_registry.Sound(id="e", file="").exists() is False
