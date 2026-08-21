"""
test_rotate_ssh_key.py — connection-aware SSH key rotation.

Rotation must push the new key to CONNECTED hosts, skip disconnected ones
(rather than letting them block the commit), and refuse to swap the local key
when no reachable host could be verified (which would lock us out everywhere).

Binds the real bridge methods onto a light stub so we avoid Bridge.__init__'s
side effects (mirrors test_key_pubkey.py). Real cryptography generates the keys;
executor.ssh_run is monkeypatched to script per-host push/verify outcomes.
"""

from __future__ import annotations

import threading
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import runspec_console.executor as executor
from runspec_console.bridge import Bridge


class _StubBridge:
    def __init__(
        self, cfg: dict, hosts: list[dict], connected: dict[str, bool]
    ) -> None:
        self._cfg = cfg
        self._hosts = hosts
        self._connected_cache = connected
        self._lock = threading.Lock()

    def get_config(self) -> dict:
        return self._cfg

    def save_config(self, data: dict) -> None:
        self._cfg = data

    def _commit_ssh_fields(self, **fields) -> None:
        # Mirror Bridge._commit_ssh_fields' merge into [ssh], capturing in memory
        # (the real one writes config.toml atomically + re-applies the pool).
        ssh = {**self._cfg.get("ssh", {}), **fields}
        self._cfg = {**self._cfg, "ssh": ssh}

    def _reload_hosts(self) -> None:  # hosts are injected directly in tests
        pass

    _generate_keypair_to_path = Bridge._generate_keypair_to_path
    _commit_key_rotation = Bridge._commit_key_rotation
    _public_key_for = Bridge._public_key_for
    _stale_key_blobs = Bridge._stale_key_blobs
    _derive_pub_blob = Bridge._derive_pub_blob
    _rotate_emit = Bridge._rotate_emit
    _rotate_progress = Bridge._rotate_progress
    _rotate_ssh_run = Bridge._rotate_ssh_run
    _rotate_push_verify_one = Bridge._rotate_push_verify_one
    _do_rotate_ssh_key = Bridge._do_rotate_ssh_key
    rotate_ssh_key = Bridge.rotate_ssh_key


def _write_old_key(canonical: Path) -> None:
    """Write a real OpenSSH private key as the existing ('old') key."""
    canonical.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    canonical.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def _setup(tmp_path, monkeypatch, hosts, connected):
    """Point key storage at tmp_path, lay down an old key, return the stub bridge."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    key_dir = tmp_path / "runspec-console"
    canonical = key_dir / "runspec_ed25519"
    _write_old_key(canonical)
    cfg = {"ssh": {"identityFile": str(canonical)}}
    bridge = _StubBridge(cfg, hosts, connected)
    return bridge, key_dir, canonical


def _make_ssh_run(behavior: dict[str, str]):
    """Build a fake ssh_run. behavior maps ssh_target → one of:
    'ok'  — push & verify both succeed
    'push_fail' — push returns non-zero
    'verify_fail' — push ok, verify returns non-zero
    Unlisted targets default to 'ok'.
    """

    def fake_ssh_run(
        ssh_target, command, identity_file=None, global_ssh_config=None, timeout=15
    ):
        mode = behavior.get(ssh_target, "ok")
        is_verify = command.strip() == "echo ok"
        if mode == "push_fail" and not is_verify:
            return 1, "", "push: connection refused"
        if mode == "verify_fail" and is_verify:
            return 1, "", "verify: permission denied"
        if is_verify:
            return 0, "ok\n", ""
        return 0, "", ""

    return fake_ssh_run


def test_disconnected_host_skipped_commit_proceeds(tmp_path, monkeypatch):
    hosts = [
        {"name": "alive", "ssh": "user@alive"},
        {"name": "down", "ssh": "user@down"},
    ]
    connected = {"alive": True, "down": False}
    bridge, key_dir, canonical = _setup(tmp_path, monkeypatch, hosts, connected)
    monkeypatch.setattr(executor, "ssh_run", _make_ssh_run({"user@alive": "ok"}))

    old_bytes = canonical.read_bytes()
    result = bridge.rotate_ssh_key()

    assert result["committed"] is True
    by_host = {h["host"]: h for h in result["per_host"]}
    assert by_host["alive"]["verified"] is True and by_host["alive"]["skipped"] is False
    assert by_host["down"]["skipped"] is True
    assert "disconnected" in by_host["down"]["error"]
    # New key swapped in; old key backed up alongside.
    assert canonical.read_bytes() != old_bytes
    assert list(key_dir.glob("runspec_ed25519.bak.*"))
    assert bridge._cfg["ssh"]["identityFile"]
    assert bridge._cfg["ssh"]["key_created_at"]


def test_all_disconnected_does_not_commit(tmp_path, monkeypatch):
    hosts = [
        {"name": "a", "ssh": "user@a"},
        {"name": "b", "ssh": "user@b"},
    ]
    connected = {"a": False, "b": False}
    bridge, key_dir, canonical = _setup(tmp_path, monkeypatch, hosts, connected)
    monkeypatch.setattr(executor, "ssh_run", _make_ssh_run({}))

    old_bytes = canonical.read_bytes()
    result = bridge.rotate_ssh_key()

    assert result["ok"] is True
    assert result["committed"] is False
    assert "No connected host" in result["message"]
    assert all(h["skipped"] for h in result["per_host"])
    # Old key untouched, nothing committed.
    assert canonical.read_bytes() == old_bytes
    assert not list(key_dir.glob("runspec_ed25519.bak.*"))


def test_connected_host_push_fail_still_blocks(tmp_path, monkeypatch):
    hosts = [
        {"name": "alive", "ssh": "user@alive"},
        {"name": "broken", "ssh": "user@broken"},
    ]
    connected = {"alive": True, "broken": True}
    bridge, key_dir, canonical = _setup(tmp_path, monkeypatch, hosts, connected)
    monkeypatch.setattr(
        executor, "ssh_run", _make_ssh_run({"user@broken": "push_fail"})
    )

    old_bytes = canonical.read_bytes()
    result = bridge.rotate_ssh_key()

    assert result["committed"] is False
    assert "broken" in result["message"]
    by_host = {h["host"]: h for h in result["per_host"]}
    assert (
        by_host["broken"]["skipped"] is False and by_host["broken"]["verified"] is False
    )
    # A connected host failing keeps the old key active.
    assert canonical.read_bytes() == old_bytes


def test_per_host_override_skipped(tmp_path, monkeypatch):
    hosts = [
        {"name": "alive", "ssh": "user@alive"},
        {"name": "custom", "ssh": "user@custom", "identityFile": "/some/other/key"},
    ]
    connected = {"alive": True, "custom": True}
    bridge, key_dir, canonical = _setup(tmp_path, monkeypatch, hosts, connected)
    monkeypatch.setattr(executor, "ssh_run", _make_ssh_run({"user@alive": "ok"}))

    result = bridge.rotate_ssh_key()

    assert result["committed"] is True
    by_host = {h["host"]: h for h in result["per_host"]}
    assert by_host["custom"]["skipped"] is True
    assert "per-host key" in by_host["custom"]["error"]
    assert by_host["alive"]["verified"] is True


def test_no_remote_hosts_commits(tmp_path, monkeypatch):
    bridge, key_dir, canonical = _setup(tmp_path, monkeypatch, hosts=[], connected={})
    # ssh_run must never be called when there are no remotes.
    monkeypatch.setattr(executor, "ssh_run", _make_ssh_run({}))

    result = bridge.rotate_ssh_key()

    assert result["committed"] is True
    assert result["per_host"] == []
    assert list(key_dir.glob("runspec_ed25519.bak.*"))


def test_transient_push_failure_is_retried_then_commits(tmp_path, monkeypatch):
    """A transient SSH blip on push (banner/session under a bastion throttle) is
    retried rather than counted as a hard host failure — the regression behind
    '9 of 96, different hosts each run'. The host recovers and rotation commits."""
    import runspec_console.bridge_config_ssh as bcs

    monkeypatch.setattr(bcs.time, "sleep", lambda *_: None)  # no real backoff wait
    hosts = [{"name": "flaky", "ssh": "user@flaky"}]
    connected = {"flaky": True}
    bridge, key_dir, canonical = _setup(tmp_path, monkeypatch, hosts, connected)

    calls = {"push": 0}

    def flaky_ssh_run(
        ssh_target, command, identity_file=None, global_ssh_config=None, timeout=15
    ):
        is_verify = command.strip() == "echo ok"
        if is_verify:
            return 0, "ok\n", ""
        calls["push"] += 1
        if calls["push"] == 1:
            # transient connect failure — ssh_run signals it with code -1
            return -1, "", "Error reading SSH protocol banner"
        return 0, "", ""

    monkeypatch.setattr(executor, "ssh_run", flaky_ssh_run)

    result = bridge.rotate_ssh_key()

    assert calls["push"] == 2  # retried once, then succeeded
    assert result["committed"] is True
    by_host = {h["host"]: h for h in result["per_host"]}
    assert by_host["flaky"]["verified"] is True


def test_durable_push_failure_is_not_retried(tmp_path, monkeypatch):
    """A command that ran and returned non-zero (a real failure) is NOT retried."""
    import runspec_console.bridge_config_ssh as bcs

    monkeypatch.setattr(bcs.time, "sleep", lambda *_: None)
    hosts = [{"name": "broken", "ssh": "user@broken"}]
    connected = {"broken": True}
    bridge, key_dir, canonical = _setup(tmp_path, monkeypatch, hosts, connected)

    calls = {"push": 0}

    def ssh_run(
        ssh_target, command, identity_file=None, global_ssh_config=None, timeout=15
    ):
        if command.strip() == "echo ok":
            return 0, "ok\n", ""
        calls["push"] += 1
        return 1, "", "disk full"  # ran, returned non-zero — durable

    monkeypatch.setattr(executor, "ssh_run", ssh_run)

    result = bridge.rotate_ssh_key()

    assert calls["push"] == 1  # no retry on a real command failure
    assert result["committed"] is False


def test_rotation_emits_progress_and_logs(tmp_path, monkeypatch):
    """Rotation surfaces per-host progress + a Dev-tab log line while it runs
    (it used to emit nothing until it finished)."""
    hosts = [
        {"name": "a", "ssh": "user@a"},
        {"name": "b", "ssh": "user@b"},
    ]
    connected = {"a": True, "b": True}
    bridge, key_dir, canonical = _setup(tmp_path, monkeypatch, hosts, connected)
    monkeypatch.setattr(executor, "ssh_run", _make_ssh_run({}))

    events: list = []
    logs: list = []
    bridge._dispatch = lambda ev, payload=None: events.append((ev, payload))
    bridge._emit_ssh_log = lambda detail: logs.append(detail)

    result = bridge.rotate_ssh_key()

    assert result["committed"] is True
    progress = [p for ev, p in events if ev == "runspec:rotate_progress"]
    # a start frame + one per processed host
    assert any(p.get("phase") == "start" for p in progress)
    assert {p.get("host") for p in progress if p.get("host")} == {"a", "b"}
    assert progress[-1]["done"] == progress[-1]["total"] == 2
    assert any(d.get("logger") == "ssh.rotate" for d in logs)
