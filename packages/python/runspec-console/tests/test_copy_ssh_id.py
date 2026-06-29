"""
test_copy_ssh_id.py — in-app ssh-copy-id (copy_ssh_id).

copy_ssh_id pushes our public key to a host's authorized_keys over a one-off
password connection so a freshly added host can switch to key auth. It must push
the host's *effective* key (per-host override wins), refuse when no key exists
(generate-first, never auto-create), surface auth/push failures clearly, never
persist the password, and re-probe connectivity on success.

Binds the real bridge methods onto a light stub to skip Bridge.__init__'s side
effects (mirrors test_rotate_ssh_key.py). Real cryptography generates the keys;
paramiko.SSHClient is replaced with a fake whose connect/exec behaviour is
scripted via class attributes.
"""

from __future__ import annotations

import threading
import types
from pathlib import Path

import paramiko
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import runspec_console.executor as executor
from runspec_console.bridge import Bridge
from runspec_console.bridge_utils import _public_key_from_private


class FakeSSHClient:
    """Stand-in for paramiko.SSHClient. Behaviour scripted via class attrs."""

    next_connect_exc: BaseException | None = None
    next_exit_status: int = 0
    next_stderr: bytes = b""
    instances: list["FakeSSHClient"] = []

    def __init__(self) -> None:
        self.connect_kwargs: dict | None = None
        self.exec_commands: list[str] = []
        self.closed = False
        FakeSSHClient.instances.append(self)

    def set_missing_host_key_policy(self, policy) -> None:  # pragma: no cover
        pass

    def load_system_host_keys(self) -> None:  # pragma: no cover
        pass

    def load_host_keys(self, path) -> None:  # pragma: no cover
        pass

    def connect(self, **kwargs) -> None:
        self.connect_kwargs = kwargs
        if FakeSSHClient.next_connect_exc is not None:
            raise FakeSSHClient.next_connect_exc

    def exec_command(self, command):
        self.exec_commands.append(command)
        channel = types.SimpleNamespace(
            recv_exit_status=lambda: FakeSSHClient.next_exit_status
        )
        stdout = types.SimpleNamespace(channel=channel)
        stderr = types.SimpleNamespace(read=lambda: FakeSSHClient.next_stderr)
        return types.SimpleNamespace(), stdout, stderr

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fake_paramiko(monkeypatch):
    FakeSSHClient.next_connect_exc = None
    FakeSSHClient.next_exit_status = 0
    FakeSSHClient.next_stderr = b""
    FakeSSHClient.instances = []
    monkeypatch.setattr(paramiko, "SSHClient", FakeSSHClient)
    # Don't touch the real known_hosts file during the connect setup.
    monkeypatch.setattr(executor, "_apply_host_key_policy", lambda client, cfg: None)
    yield


class _StubBridge:
    def __init__(self, cfg: dict, hosts: list[dict], probe_result: bool = True) -> None:
        self._cfg = cfg
        self._hosts = hosts
        self._connected_cache: dict[str, bool] = {}
        self._lock = threading.Lock()
        self._probe_result = probe_result
        self.checked: list[str | None] = []
        self.dispatched: list[str] = []

    def get_config(self) -> dict:
        return self._cfg

    def _check_connected(self, host: dict) -> bool:
        self.checked.append(host.get("name"))
        return self._probe_result

    def _dispatch(self, event: str, detail: dict) -> None:
        self.dispatched.append(event)

    def _refresh_cycle(self) -> None:  # background thread no-op in tests
        pass

    _local_host_entry = Bridge._local_host_entry
    _host_entry = Bridge._host_entry
    _public_key_for = Bridge._public_key_for
    _push_key_over_password = Bridge._push_key_over_password
    copy_ssh_id = Bridge.copy_ssh_id


def _write_key(path: Path) -> str:
    """Write a real OpenSSH private key, return its derived public-key line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return _public_key_from_private(path)


def test_happy_path_pushes_key_and_marks_connected(tmp_path):
    key = tmp_path / "id"
    pub = _write_key(key)
    bridge = _StubBridge(
        {"ssh": {"identityFile": str(key)}},
        [{"name": "h1", "ssh": "deploy@h1:2222"}],
        probe_result=True,
    )

    res = bridge.copy_ssh_id("h1", "secret")

    assert res["ok"] is True
    assert res["connected"] is True
    client = FakeSSHClient.instances[-1]
    assert client.connect_kwargs["hostname"] == "h1"
    assert client.connect_kwargs["port"] == 2222
    assert client.connect_kwargs["username"] == "deploy"
    assert client.connect_kwargs["password"] == "secret"
    # Password path only — no silent success via an unrelated agent/key.
    assert client.connect_kwargs["allow_agent"] is False
    assert client.connect_kwargs["look_for_keys"] is False
    assert pub in client.exec_commands[0]
    assert client.closed is True
    assert bridge._connected_cache["h1"] is True
    assert "h1" in bridge.checked
    assert "runspec:hosts_updated" in bridge.dispatched


def test_username_falls_back_to_global(tmp_path):
    key = tmp_path / "id"
    _write_key(key)
    bridge = _StubBridge(
        {"ssh": {"identityFile": str(key), "user": "ops"}},
        [{"name": "h1", "ssh": "h1"}],
        probe_result=False,
    )

    res = bridge.copy_ssh_id("h1", "pw")

    assert res["ok"] is True
    assert res["connected"] is False
    assert "verifying" in res["message"].lower()
    assert FakeSSHClient.instances[-1].connect_kwargs["username"] == "ops"


def test_http_proxy_is_tunnelled(tmp_path, monkeypatch):
    # The whole fleet may sit behind an HTTP CONNECT proxy — copy-id must reach
    # the host through the same [ssh] proxy as every other connection.
    key = tmp_path / "id"
    _write_key(key)
    sentinel = object()
    calls: dict = {}

    def fake_sock(proxy, host, port, timeout=10.0):
        calls["args"] = (proxy, host, port)
        return sentinel

    monkeypatch.setattr(executor, "_http_connect_sock", fake_sock)
    bridge = _StubBridge(
        {"ssh": {"identityFile": str(key), "proxy": "http://proxy.corp:8080"}},
        [{"name": "h1", "ssh": "deploy@h1:2222"}],
        probe_result=True,
    )

    res = bridge.copy_ssh_id("h1", "secret")

    assert res["ok"] is True
    assert calls["args"] == ("http://proxy.corp:8080", "h1", 2222)
    assert FakeSSHClient.instances[-1].connect_kwargs["sock"] is sentinel


def test_no_key_configured_errors_generate_first(tmp_path):
    bridge = _StubBridge(
        {"ssh": {"identityFile": ""}},
        [{"name": "h1", "ssh": "deploy@h1"}],
    )

    res = bridge.copy_ssh_id("h1", "secret")

    assert res["ok"] is False
    assert "generate" in res["message"].lower()
    assert FakeSSHClient.instances == []  # never connected


def test_missing_key_file_errors(tmp_path):
    bridge = _StubBridge(
        {"ssh": {"identityFile": str(tmp_path / "nope")}},
        [{"name": "h1", "ssh": "deploy@h1"}],
    )

    res = bridge.copy_ssh_id("h1", "secret")

    assert res["ok"] is False
    assert "not found" in res["message"].lower()
    assert FakeSSHClient.instances == []


def test_per_host_override_key_is_pushed(tmp_path):
    gkey = tmp_path / "global"
    gpub = _write_key(gkey)
    hkey = tmp_path / "hostkey"
    hpub = _write_key(hkey)
    bridge = _StubBridge(
        {"ssh": {"identityFile": str(gkey)}},
        [{"name": "h1", "ssh": "deploy@h1", "identityFile": str(hkey)}],
        probe_result=True,
    )

    res = bridge.copy_ssh_id("h1", "pw")

    assert res["ok"] is True
    cmd = FakeSSHClient.instances[-1].exec_commands[0]
    assert hpub in cmd
    assert gpub not in cmd


def test_auth_failure_is_friendly(tmp_path):
    key = tmp_path / "id"
    _write_key(key)
    bridge = _StubBridge(
        {"ssh": {"identityFile": str(key)}},
        [{"name": "h1", "ssh": "deploy@h1"}],
    )
    FakeSSHClient.next_connect_exc = paramiko.AuthenticationException("nope")

    res = bridge.copy_ssh_id("h1", "wrong")

    assert res["ok"] is False
    assert "authentication failed" in res["message"].lower()
    assert FakeSSHClient.instances[-1].exec_commands == []  # nothing executed
    assert "h1" not in bridge._connected_cache


def test_nonzero_push_exit_reports_error(tmp_path):
    key = tmp_path / "id"
    _write_key(key)
    bridge = _StubBridge(
        {"ssh": {"identityFile": str(key)}},
        [{"name": "h1", "ssh": "deploy@h1"}],
    )
    FakeSSHClient.next_exit_status = 1
    FakeSSHClient.next_stderr = b"Permission denied"

    res = bridge.copy_ssh_id("h1", "pw")

    assert res["ok"] is False
    assert "authorized_keys" in res["message"]
    assert "Permission denied" in res["message"]
    assert bridge._connected_cache.get("h1") is None
    assert FakeSSHClient.instances[-1].closed is True  # closed even on failure


def test_no_ssh_host_rejected(tmp_path):
    bridge = _StubBridge(
        {"ssh": {"identityFile": ""}},
        [{"name": "h1", "ssh": None}],
    )

    res = bridge.copy_ssh_id("h1", "pw")

    assert res["ok"] is False
    assert "not a reachable remote host" in res["message"]
    assert FakeSSHClient.instances == []


def test_empty_password_rejected(tmp_path):
    key = tmp_path / "id"
    _write_key(key)
    bridge = _StubBridge(
        {"ssh": {"identityFile": str(key)}},
        [{"name": "h1", "ssh": "deploy@h1"}],
    )

    res = bridge.copy_ssh_id("h1", "   ")

    assert res["ok"] is False
    assert "password required" in res["message"].lower()
    assert FakeSSHClient.instances == []


def test_password_not_persisted(tmp_path):
    key = tmp_path / "id"
    _write_key(key)
    bridge = _StubBridge(
        {"ssh": {"identityFile": str(key)}},
        [{"name": "h1", "ssh": "deploy@h1"}],
        probe_result=True,
    )

    bridge.copy_ssh_id("h1", "topsecret")

    assert "topsecret" not in str(bridge._cfg)
    assert "topsecret" not in str(bridge._connected_cache)
