"""
test_copy_ssh_id_all.py — bulk in-app ssh-copy-id (copy_ssh_id_all).

copy_ssh_id_all pushes our public key to many hosts' authorized_keys over one-off
password connections so a freshly synced fleet can switch to key auth in one shot
(initial bootstrap from a Config-Sync pull). It must: default to the disconnected
remote hosts, honour an explicit target list, try one password against each host,
report per-host success/failure (one failure doesn't abort the rest), re-probe
each host, dispatch a single hosts_updated event, and never persist the password.

Reuses the FakeSSHClient harness shape from test_copy_ssh_id.py — the bulk method
shares the same single-host push helper, so the same scripting applies.
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
    """Stand-in for paramiko.SSHClient. Per-host behaviour scripted via class
    attrs keyed on the connect hostname (so one bulk run can mix outcomes)."""

    # hostname -> exception to raise on connect (else None)
    connect_exc_by_host: dict[str, BaseException] = {}
    # hostname -> push exit status (default 0)
    exit_status_by_host: dict[str, int] = {}
    stderr: bytes = b""
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
        exc = FakeSSHClient.connect_exc_by_host.get(kwargs.get("hostname"))
        if exc is not None:
            raise exc

    def exec_command(self, command):
        self.exec_commands.append(command)
        host = (self.connect_kwargs or {}).get("hostname")
        code = FakeSSHClient.exit_status_by_host.get(host, 0)
        channel = types.SimpleNamespace(recv_exit_status=lambda: code)
        stdout = types.SimpleNamespace(channel=channel)
        stderr = types.SimpleNamespace(read=lambda: FakeSSHClient.stderr)
        return types.SimpleNamespace(), stdout, stderr

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fake_paramiko(monkeypatch):
    FakeSSHClient.connect_exc_by_host = {}
    FakeSSHClient.exit_status_by_host = {}
    FakeSSHClient.stderr = b""
    FakeSSHClient.instances = []
    monkeypatch.setattr(paramiko, "SSHClient", FakeSSHClient)
    monkeypatch.setattr(executor, "_apply_host_key_policy", lambda client, cfg: None)
    yield


class _StubBridge:
    def __init__(
        self,
        cfg: dict,
        hosts: list[dict],
        connected: dict[str, bool] | None = None,
        probe_result: bool = True,
    ) -> None:
        self._cfg = cfg
        self._hosts = hosts
        self._connected_cache: dict[str, bool] = dict(connected or {})
        self._lock = threading.Lock()
        self._probe_result = probe_result
        self.checked: list[str | None] = []
        self.dispatched: list[str] = []

    def get_config(self) -> dict:
        return self._cfg

    def _reload_hosts(self) -> None:  # hosts already populated in tests
        pass

    def _check_connected(self, host: dict) -> bool:
        self.checked.append(host.get("name"))
        return self._probe_result

    def _dispatch(self, event: str, detail: dict) -> None:
        self.dispatched.append(event)

    def _kick_refresh(self, *, force: bool = False) -> None:  # no-op in tests
        pass

    def _invalidate_discovery(self, names=None) -> None:  # no-op in tests
        pass

    def _refresh_cycle(self) -> None:  # background thread no-op in tests
        pass

    _public_key_for = Bridge._public_key_for
    _push_key_over_password = Bridge._push_key_over_password
    copy_ssh_id_all = Bridge.copy_ssh_id_all


def _write_key(path: Path) -> str:
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


def test_defaults_to_disconnected_hosts(tmp_path):
    key = tmp_path / "id"
    pub = _write_key(key)
    bridge = _StubBridge(
        {"ssh": {"identityFile": str(key)}},
        [
            {"name": "h1", "ssh": "deploy@h1"},
            {"name": "h2", "ssh": "deploy@h2"},
            {"name": "h3", "ssh": "deploy@h3"},
        ],
        connected={"h2": True},  # already authorised — must be skipped by default
        probe_result=True,
    )

    res = bridge.copy_ssh_id_all("secret")

    assert res["ok"] is True
    hosts = {h["host"] for h in res["per_host"]}
    assert hosts == {"h1", "h3"}
    assert all(h["ok"] for h in res["per_host"])
    # The public key was pushed to each targeted host.
    for client in FakeSSHClient.instances:
        assert pub in client.exec_commands[0]
    # Connectivity flips and a single hosts_updated event fires.
    assert bridge._connected_cache["h1"] is True
    assert bridge._connected_cache["h3"] is True
    assert bridge.dispatched.count("runspec:hosts_updated") == 1


def test_explicit_targets_override_connected_filter(tmp_path):
    key = tmp_path / "id"
    _write_key(key)
    bridge = _StubBridge(
        {"ssh": {"identityFile": str(key)}},
        [
            {"name": "h1", "ssh": "deploy@h1"},
            {"name": "h2", "ssh": "deploy@h2"},
        ],
        connected={"h1": True, "h2": True},
        probe_result=True,
    )

    res = bridge.copy_ssh_id_all("pw", targets=["h2"])

    assert [h["host"] for h in res["per_host"]] == ["h2"]


def test_unknown_targets_ignored(tmp_path):
    key = tmp_path / "id"
    _write_key(key)
    bridge = _StubBridge(
        {"ssh": {"identityFile": str(key)}},
        [{"name": "h1", "ssh": "deploy@h1"}],
    )

    res = bridge.copy_ssh_id_all("pw", targets=["h1", "ghost"])

    assert [h["host"] for h in res["per_host"]] == ["h1"]


def test_partial_failure_reported_and_does_not_abort(tmp_path):
    key = tmp_path / "id"
    _write_key(key)
    bridge = _StubBridge(
        {"ssh": {"identityFile": str(key)}},
        [
            {"name": "h1", "ssh": "deploy@h1"},
            {"name": "h2", "ssh": "deploy@h2"},
            {"name": "h3", "ssh": "deploy@h3"},
        ],
        probe_result=True,
    )
    # h2 fails auth; h1 and h3 still get the key.
    FakeSSHClient.connect_exc_by_host = {"h2": paramiko.AuthenticationException("no")}

    res = bridge.copy_ssh_id_all("pw")

    by = {h["host"]: h for h in res["per_host"]}
    assert by["h1"]["ok"] is True
    assert by["h3"]["ok"] is True
    assert by["h2"]["ok"] is False
    assert "authentication failed" in by["h2"]["error"].lower()
    assert res["ok"] is True  # at least one succeeded
    assert "2 of 3" in res["message"]
    # The failed host's connectivity was never marked.
    assert "h2" not in bridge._connected_cache


def test_push_exit_failure_reported(tmp_path):
    key = tmp_path / "id"
    _write_key(key)
    bridge = _StubBridge(
        {"ssh": {"identityFile": str(key)}},
        [{"name": "h1", "ssh": "deploy@h1"}],
        probe_result=True,
    )
    FakeSSHClient.exit_status_by_host = {"h1": 1}
    FakeSSHClient.stderr = b"Permission denied"

    res = bridge.copy_ssh_id_all("pw")

    h = res["per_host"][0]
    assert h["ok"] is False
    assert "authorized_keys" in h["error"]
    assert "Permission denied" in h["error"]
    assert res["ok"] is False


def test_empty_password_rejected(tmp_path):
    key = tmp_path / "id"
    _write_key(key)
    bridge = _StubBridge(
        {"ssh": {"identityFile": str(key)}},
        [{"name": "h1", "ssh": "deploy@h1"}],
    )

    res = bridge.copy_ssh_id_all("   ")

    assert res["ok"] is False
    assert "password required" in res["message"].lower()
    assert FakeSSHClient.instances == []


def test_no_matching_hosts(tmp_path):
    key = tmp_path / "id"
    _write_key(key)
    bridge = _StubBridge(
        {"ssh": {"identityFile": str(key)}},
        [{"name": "h1", "ssh": "deploy@h1"}],
        connected={"h1": True},  # only host already connected → nothing to do
    )

    res = bridge.copy_ssh_id_all("pw")

    assert res["ok"] is False
    assert "no matching remote hosts" in res["message"].lower()
    assert FakeSSHClient.instances == []


def test_no_key_configured_fails_every_host(tmp_path):
    bridge = _StubBridge(
        {"ssh": {"identityFile": ""}},
        [{"name": "h1", "ssh": "deploy@h1"}],
    )

    res = bridge.copy_ssh_id_all("pw")

    assert res["ok"] is False
    assert res["per_host"][0]["ok"] is False
    assert "generate" in res["per_host"][0]["error"].lower()
    assert FakeSSHClient.instances == []  # never connected without a key


def test_password_not_persisted(tmp_path):
    key = tmp_path / "id"
    _write_key(key)
    bridge = _StubBridge(
        {"ssh": {"identityFile": str(key)}},
        [{"name": "h1", "ssh": "deploy@h1"}],
        probe_result=True,
    )

    bridge.copy_ssh_id_all("topsecret")

    assert "topsecret" not in str(bridge._cfg)
    assert "topsecret" not in str(bridge._connected_cache)
