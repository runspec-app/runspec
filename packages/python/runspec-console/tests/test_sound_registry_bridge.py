"""Tests for the named-sound registry Bridge methods (get/save/import/delete/
preview) — the Settings-UI CRUD surface. A lightweight stub borrows just the
methods under test (no pywebview/COM), mirroring test_action_tools.py."""

from __future__ import annotations

import pytest

from runspec_console import config, sounds_registry
from runspec_console.bridge import Bridge


class _SoundBridge:
    get_sounds = Bridge.get_sounds
    save_sounds = Bridge.save_sounds
    import_sound = Bridge.import_sound
    delete_sound = Bridge.delete_sound
    preview_sound = Bridge.preview_sound
    _play_sound_id = Bridge._play_sound_id


@pytest.fixture
def store(tmp_path, monkeypatch):
    reg = tmp_path / "runspec_sounds.toml"
    sdir = tmp_path / "sounds"
    monkeypatch.setattr(config, "sounds_registry_path", lambda: reg)
    monkeypatch.setattr(config, "sounds_store_dir", lambda: sdir)
    return tmp_path


def test_get_sounds_empty(store):
    assert _SoundBridge().get_sounds() == []


def test_save_and_get_sounds_reports_exists(store, tmp_path):
    present = tmp_path / "there.wav"
    present.write_bytes(b"x")
    b = _SoundBridge()
    res = b.save_sounds(
        [
            {"id": "here", "label": "Here", "file": str(present)},
            {"id": "gone", "label": "Gone", "file": str(tmp_path / "missing.mp3")},
        ]
    )
    assert res["ok"] is True
    got = {s["id"]: s for s in b.get_sounds()}
    assert got["here"]["exists"] is True
    assert got["gone"]["exists"] is False  # synced-elsewhere entry → silent


def test_save_sounds_rejects_duplicate_id(store):
    b = _SoundBridge()
    res = b.save_sounds(
        [
            {"id": "dup", "label": "A", "file": "/x/a.wav"},
            {"id": "dup", "label": "B", "file": "/x/b.wav"},
        ]
    )
    assert res["ok"] is False and "duplicate" in res["error"]


def test_save_sounds_rejects_bad_entry(store):
    b = _SoundBridge()
    res = b.save_sounds([{"id": "bad id", "file": "/x/a.wav"}])
    assert res["ok"] is False and res["error"]


def test_import_sound_copies_into_store(store, tmp_path):
    src = tmp_path / "source.mp3"
    src.write_bytes(b"audio")
    b = _SoundBridge()
    res = b.import_sound(str(src), "Deploy Done")
    assert res["ok"] is True
    entry = res["sound"]
    assert entry["id"] == "deploy-done"
    # copied under the app-data store, not referencing the original path
    copied = config.sounds_store_dir() / "deploy-done.mp3"
    assert copied.is_file() and entry["file"] == str(copied)
    # and it is now in the registry
    assert sounds_registry.get_sound("deploy-done") is not None


def test_import_sound_rejects_bad_ext(store, tmp_path):
    src = tmp_path / "clip.ogg"
    src.write_bytes(b"x")
    res = _SoundBridge().import_sound(str(src), "Clip")
    assert res["ok"] is False and "wav" in res["error"]


def test_import_sound_missing_file(store):
    res = _SoundBridge().import_sound("/no/such/file.wav", "X")
    assert res["ok"] is False


def test_import_dedupes_ids(store, tmp_path):
    src = tmp_path / "a.wav"
    src.write_bytes(b"x")
    b = _SoundBridge()
    first = b.import_sound(str(src), "Ping")["sound"]["id"]
    second = b.import_sound(str(src), "Ping")["sound"]["id"]
    assert first == "ping" and second == "ping-2"


def test_delete_sound_removes_entry_and_imported_file(store, tmp_path):
    src = tmp_path / "a.wav"
    src.write_bytes(b"x")
    b = _SoundBridge()
    b.import_sound(str(src), "Ping")
    copied = config.sounds_store_dir() / "ping.wav"
    assert copied.is_file()
    res = b.delete_sound("ping")
    assert res["ok"] is True
    assert sounds_registry.get_sound("ping") is None
    assert not copied.exists()  # the console-owned copy is cleaned up


def test_delete_sound_leaves_external_file_untouched(store, tmp_path):
    external = tmp_path / "elsewhere" / "keep.wav"
    external.parent.mkdir()
    external.write_bytes(b"x")
    b = _SoundBridge()
    b.save_sounds([{"id": "ext", "label": "Ext", "file": str(external)}])
    b.delete_sound("ext")
    assert external.exists()  # a referenced-in-place file is never deleted


def test_preview_sound_never_raises_off_windows(store):
    # No-op off Windows (available() is False), but must not raise.
    assert _SoundBridge().preview_sound("confirm") is False
