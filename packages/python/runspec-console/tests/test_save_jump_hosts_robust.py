"""Regression: saving a jump host must always write valid TOML and never wedge.

The reported failure: on a fresh install the operator adds a host and leaves an
empty "Add path" row. That row arrives as ``runspec_paths = [null]`` over the
bridge, which the hand-rolled TOML writer emitted verbatim as ``[None]`` — invalid
TOML. The corrupt file then made every later ``save_jump_hosts`` throw while
reading the existing entries, so the editor modal could never close.

These cover the three fixes: the serializer escapes/handles values safely,
``save_jump_hosts`` drops blank path rows, and a corrupt file self-heals on save.
"""

from __future__ import annotations

import threading

import pytest

from runspec_console import config as config_mod
from runspec_console import hosts as hosts_mod
from runspec_console.bridge import Bridge


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "app_dir", lambda: tmp_path)
    return tmp_path


class _StubBridge:
    def __init__(self) -> None:
        self._hosts: list[dict] = []
        self._lock = threading.Lock()
        self.dispatched: list[str] = []

    def restart_chat(self) -> None:
        pass

    def _kick_refresh(self, *, force: bool = False) -> None:
        pass

    def _dispatch(self, event: str, detail: dict) -> None:
        self.dispatched.append(event)

    _reload_hosts = Bridge._reload_hosts
    _MANAGED_HOST_KEYS = Bridge._MANAGED_HOST_KEYS
    get_jump_hosts = Bridge.get_jump_hosts
    save_jump_hosts = Bridge.save_jump_hosts


def _reload_from_disk() -> list[dict]:
    return hosts_mod.load_hosts(config_mod.hosts_path())


# ── the serializer itself ────────────────────────────────────────────────────


def test_toml_value_escapes_and_handles_non_strings():
    # Strings with quotes/backslashes must escape; None must never become `None`.
    assert hosts_mod._toml_value('a"b') == '"a\\"b"'
    assert hosts_mod._toml_value("C:\\venvs\\x") == '"C:\\\\venvs\\\\x"'
    assert hosts_mod._toml_value(True) == "true"
    assert hosts_mod._toml_value(22) == "22"
    assert hosts_mod._toml_value(None) == '""'
    assert hosts_mod._toml_value([None, "x"]) == '["", "x"]'


def test_save_hosts_roundtrips_awkward_values(app):
    hosts_mod.save_hosts(
        config_mod.hosts_path(),
        [{"name": 'weird"name', "ssh": "u@h", "identityFile": "C:\\keys\\id_ed25519"}],
    )
    # Before the fix this raised TOMLDecodeError; now it re-parses cleanly.
    (entry,) = _reload_from_disk()
    assert entry["name"] == 'weird"name'
    assert entry["identityFile"] == "C:\\keys\\id_ed25519"


# ── save_jump_hosts drops blank path rows ────────────────────────────────────


@pytest.mark.parametrize("bad_paths", [[None], [""], ["   "], [None, ""]])
def test_save_jump_hosts_drops_blank_path_rows(app, bad_paths):
    b = _StubBridge()
    b.save_jump_hosts(
        [
            {
                "name": "h1",
                "hostname": "h1.corp",
                "user": "deploy",
                "runspec_paths": bad_paths,
            }
        ]
    )
    (entry,) = _reload_from_disk()  # must re-parse — was invalid `[None]` TOML
    assert entry["ssh"] == "deploy@h1.corp"
    assert entry["runspec_paths"] == ["runspec"]  # fell back, not [null]/[""]


def test_save_jump_hosts_keeps_real_paths(app):
    b = _StubBridge()
    b.save_jump_hosts(
        [
            {
                "name": "h1",
                "hostname": "h1.corp",
                "runspec_paths": ["/opt/v/bin/runspec", None, ""],
            }
        ]
    )
    (entry,) = _reload_from_disk()
    assert entry["runspec_paths"] == ["/opt/v/bin/runspec"]


# ── a corrupt file never crashes; it is quarantined, not raised ──────────────

CORRUPT = '[[host]]\nname = "h1"\nssh = "u@h1"\nrunspec_paths = [None]\n'


def test_load_hosts_quarantines_a_corrupt_file(app):
    # This is the launch path (Bridge.__init__ -> _reload_hosts -> load_hosts):
    # a malformed file must return [] instead of raising, or the app won't start.
    path = config_mod.hosts_path()
    path.write_text(CORRUPT, encoding="utf-8")

    assert hosts_mod.load_hosts(path) == []  # no exception — app can launch
    assert not path.exists()  # moved aside, not left to crash the next read
    backups = list(app.glob("runspec_hosts.toml.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == CORRUPT  # data preserved


def test_save_jump_hosts_heals_corrupt_existing_file(app):
    # The wedged state the operator hit: a hosts file left invalid by the old
    # writer. Saving through the editor must recover rather than throw forever.
    config_mod.hosts_path().write_text(CORRUPT, encoding="utf-8")

    b = _StubBridge()
    b.save_jump_hosts(
        [{"name": "h2", "hostname": "h2.corp", "runspec_paths": ["runspec"]}]
    )

    (entry,) = _reload_from_disk()  # save wrote a clean file over the quarantine
    assert entry["name"] == "h2"
