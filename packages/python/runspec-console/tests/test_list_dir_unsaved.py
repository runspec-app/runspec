"""
test_list_dir_unsaved.py — browse a not-yet-saved host (Settings → Jump Hosts
add/edit form) so the runspec-path picker can pick where runspec lives.

list_dir_unsaved connects *straight* to the host from the form's
hostname/user/port/identityFile (no jump, no saved entry), caches the SFTP for
the browse session alongside the relay ad-hoc connections, and is torn down by
close_adhoc. Binds the real bridge methods onto a light stub to skip
Bridge.__init__'s side effects (mirrors test_copy_ssh_id.py); _make_ssh_client
and fs_browse.list_sftp are faked so no real SSH happens.
"""

from __future__ import annotations

import threading
import types

import runspec_console.executor as executor
import runspec_console.fs_browse as fs_browse
from runspec_console.bridge import Bridge


class _FakeSftp:
    def __init__(self) -> None:
        self.active = True
        self.closed = False

    def get_channel(self):
        transport = types.SimpleNamespace(is_active=lambda: self.active)
        return types.SimpleNamespace(get_transport=lambda: transport)

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    def __init__(self, sftp: _FakeSftp) -> None:
        self._sftp = sftp
        self.closed = False

    def open_sftp(self):
        return self._sftp

    def close(self) -> None:
        self.closed = True


class _StubBridge:
    def __init__(self, cfg: dict) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self._adhoc_conns: dict = {}

    def get_config(self) -> dict:
        return self._cfg

    list_dir_unsaved = Bridge.list_dir_unsaved
    _get_unsaved_sftp = Bridge._get_unsaved_sftp
    _adhoc_alive = staticmethod(Bridge._adhoc_alive)
    _close_adhoc_key = Bridge._close_adhoc_key
    close_adhoc = Bridge.close_adhoc


def _patch_ssh(monkeypatch):
    """Replace _make_ssh_client with a fake; return (calls, listing_calls)."""
    sftp = _FakeSftp()
    calls: list[tuple] = []

    def fake_make(target, identity_file, cfg=None):
        calls.append((target, identity_file, cfg))
        return _FakeClient(sftp)

    listing_calls: list[tuple] = []

    def fake_list_sftp(s, path, dirs_only=False):
        listing_calls.append((s, path, dirs_only))
        return {
            "cwd": path or "/home/deploy",
            "parent": "/home",
            "entries": [],
            "truncated": False,
        }

    monkeypatch.setattr(executor, "_make_ssh_client", fake_make)
    monkeypatch.setattr(fs_browse, "list_sftp", fake_list_sftp)
    return calls, listing_calls, sftp


def test_empty_hostname_errors_without_connecting(monkeypatch):
    calls, _, _ = _patch_ssh(monkeypatch)
    bridge = _StubBridge({"ssh": {}})

    res = bridge.list_dir_unsaved("")

    assert res == {"error": "no hostname specified"}
    assert calls == []  # never tried to connect


def test_builds_target_from_form_details_and_lists(monkeypatch):
    calls, listing_calls, _ = _patch_ssh(monkeypatch)
    bridge = _StubBridge({"ssh": {"user": "ignored-global"}})

    res = bridge.list_dir_unsaved(
        "host.example.com",
        path="/opt",
        user="deploy",
        port=2222,
        identity_file="/keys/id_ed25519",
        dirs_only=True,
    )

    assert res["cwd"] == "/opt"
    # Target string is user@host:port; identity + [ssh] cfg threaded through.
    assert calls == [
        ("deploy@host.example.com:2222", "/keys/id_ed25519", {"user": "ignored-global"})
    ]
    assert listing_calls == [(listing_calls[0][0], "/opt", True)]


def test_no_user_or_port_uses_bare_hostname(monkeypatch):
    calls, _, _ = _patch_ssh(monkeypatch)
    bridge = _StubBridge({"ssh": {}})

    bridge.list_dir_unsaved("plainhost")

    assert calls[0][0] == "plainhost"


def test_connection_is_cached_and_reused(monkeypatch):
    calls, _, _ = _patch_ssh(monkeypatch)
    bridge = _StubBridge({"ssh": {}})

    bridge.list_dir_unsaved("host", path="/a", user="u", port=22)
    bridge.list_dir_unsaved("host", path="/b", user="u", port=22)

    # Same target → one client opened, reused for the second navigation.
    assert len(calls) == 1


def test_close_adhoc_tears_down_browse_connection(monkeypatch):
    _, _, sftp = _patch_ssh(monkeypatch)
    bridge = _StubBridge({"ssh": {}})

    bridge.list_dir_unsaved("host", user="u")
    assert bridge._adhoc_conns  # cached
    bridge.close_adhoc()

    assert bridge._adhoc_conns == {}
    assert sftp.closed is True


def test_dropped_connection_reconnects_once(monkeypatch):
    calls, _, sftp = _patch_ssh(monkeypatch)
    bridge = _StubBridge({"ssh": {}})

    # Prime a cached connection, then simulate it going stale so the reuse path
    # treats it as dead and a fresh client is opened.
    bridge.list_dir_unsaved("host", user="u")
    sftp.active = False

    res = bridge.list_dir_unsaved("host", path="/c", user="u")

    assert "error" not in res
    assert len(calls) == 2  # reconnected
