"""
test_credentials.py — credential metadata store + bridge CRUD.

The secret value must live only in the OS keyring (faked here), never in the
``runspec_credentials.toml`` metadata file. The bridge CRUD methods are bound
onto a light harness; ``credentials_path`` is redirected at a tmp file and the
keyring is an in-memory fake.
"""

from __future__ import annotations

import pytest

import runspec_console.bridge as bridge_mod
import runspec_console.credentials as cred_mod
from runspec_console.bridge import Bridge
from runspec_console.credentials import (
    KEYRING_SERVICE,
    load_credentials,
    save_credentials,
)


class _FakeKeyring:
    """Minimal in-memory stand-in for the ``keyring`` module."""

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def get_keyring(self):
        return self  # type name != "Keyring" → treated as a usable backend

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def get_password(self, service, name):
        return self.store.get((service, name))

    def delete_password(self, service, name):
        if (service, name) in self.store:
            del self.store[(service, name)]
        else:
            raise KeyError(name)


class _CredBridge:
    credentials_backend = Bridge.credentials_backend
    list_credentials = Bridge.list_credentials
    credentials_for_runnable = Bridge.credentials_for_runnable
    save_credential = Bridge.save_credential
    delete_credential = Bridge.delete_credential
    _reload_credentials = Bridge._reload_credentials
    _credential_entry = Bridge._credential_entry
    _scoped_credentials = Bridge._scoped_credentials
    _scope_matches = staticmethod(Bridge._scope_matches)

    def __init__(self):
        self._credentials: list = []


@pytest.fixture
def kr(monkeypatch):
    fake = _FakeKeyring()
    monkeypatch.setattr(cred_mod, "_keyring", lambda: fake)
    return fake


@pytest.fixture
def b(monkeypatch, tmp_path, kr):
    monkeypatch.setattr(bridge_mod, "credentials_path", lambda: tmp_path / "creds.toml")
    bridge = _CredBridge()
    bridge._tmp = tmp_path  # for assertions on the file
    return bridge


# ── metadata store round-trip ──────────────────────────────────────────────────


def test_store_roundtrip(tmp_path):
    path = tmp_path / "creds.toml"
    entries = [
        {
            "id": "svc-prod",
            "label": "Service account (prod)",
            "kind": "userpass",
            "username": "svc_runner",
            "username_arg": "service-username",
            "secret_arg": "user-password",
            "scope_hosts": ["prod-*"],
            "scope_runnables": ["deploy", "db-*"],
        }
    ]
    save_credentials(path, entries)
    loaded = load_credentials(path)
    assert loaded == entries
    # Metadata carries arg *names* (secret_arg) but never a secret value field.
    assert "\nsecret =" not in path.read_text()


def test_load_missing_file_is_empty(tmp_path):
    assert load_credentials(tmp_path / "nope.toml") == []


# ── bridge CRUD ─────────────────────────────────────────────────────────────────


def test_save_credential_stores_secret_in_keyring_only(b, kr):
    res = b.save_credential(
        {
            "label": "Prod svc",
            "kind": "userpass",
            "username": "svc",
            "username_arg": "service-username",
            "secret_arg": "user-password",
            "secret": "s3cret",
            "scope_hosts": ["prod-*"],
        }
    )
    cid = res["id"]
    assert kr.store[(KEYRING_SERVICE, cid)] == "s3cret"
    # metadata file holds no secret value
    assert "s3cret" not in (b._tmp / "creds.toml").read_text()

    listed = b.list_credentials()
    assert len(listed) == 1
    entry = listed[0]
    assert entry["id"] == cid
    assert entry["has_secret"] is True
    assert entry["scope_hosts"] == ["prod-*"]
    assert "secret" not in entry  # never surfaced


def test_blank_secret_preserves_existing(b, kr):
    cid = b.save_credential({"label": "X", "secret": "keep-me"})["id"]
    # Update label only, no secret field → stored secret untouched.
    b.save_credential({"id": cid, "label": "X renamed"})
    assert kr.store[(KEYRING_SERVICE, cid)] == "keep-me"
    assert b.list_credentials()[0]["label"] == "X renamed"


def test_update_replaces_secret(b, kr):
    cid = b.save_credential({"label": "X", "secret": "old"})["id"]
    b.save_credential({"id": cid, "label": "X", "secret": "new"})
    assert kr.store[(KEYRING_SERVICE, cid)] == "new"


def test_delete_removes_metadata_and_secret(b, kr):
    cid = b.save_credential({"label": "X", "secret": "s"})["id"]
    b.delete_credential(cid)
    assert b.list_credentials() == []
    assert (KEYRING_SERVICE, cid) not in kr.store


def test_missing_label_errors(b):
    assert b.save_credential({"label": "  "}) == {"error": "a label is required"}


def test_invalid_kind_errors(b):
    res = b.save_credential({"label": "X", "kind": "bogus"})
    assert res == {"error": "unknown kind: bogus"}


# ── ssh-key credentials ─────────────────────────────────────────────────────────


def test_ssh_key_credential_stores_paths_not_key(b, kr):
    res = b.save_credential(
        {
            "label": "Config repo deploy key",
            "kind": "ssh-key",
            "username": "git",
            "private_key": "~/.ssh/runspec_ed25519",
            "public_key": "~/.ssh/runspec_ed25519.pub",
        }
    )
    cid = res["id"]
    entry = b.list_credentials()[0]
    assert entry["kind"] == "ssh-key"
    assert entry["private_key"] == "~/.ssh/runspec_ed25519"
    assert entry["public_key"] == "~/.ssh/runspec_ed25519.pub"
    # No passphrase was given → nothing in the keyring.
    assert (KEYRING_SERVICE, cid) not in kr.store
    # Paths are metadata; the key file itself is never read or stored.
    text = (b._tmp / "creds.toml").read_text()
    assert 'kind = "ssh-key"' in text


def test_ssh_key_passphrase_goes_to_keyring(b, kr):
    cid = b.save_credential(
        {
            "label": "Encrypted key",
            "kind": "ssh-key",
            "private_key": "/keys/id_ed25519",
            "secret": "pa55phrase",
        }
    )["id"]
    assert kr.store[(KEYRING_SERVICE, cid)] == "pa55phrase"
    assert "pa55phrase" not in (b._tmp / "creds.toml").read_text()


def test_ssh_key_requires_private_key(b):
    res = b.save_credential({"label": "No key", "kind": "ssh-key"})
    assert res == {"error": "an SSH-key credential needs a private key path"}


def test_credentials_for_runnable_filters_by_scope(b):
    b.save_credential(
        {
            "label": "Prod",
            "secret": "s",
            "secret_arg": "password",
            "scope_runnables": ["deploy"],
        }
    )
    b.save_credential(
        {"label": "Other", "secret": "s", "scope_runnables": ["something-else"]}
    )
    scoped = b.credentials_for_runnable("prod-1", "deploy")
    assert [c["label"] for c in scoped] == ["Prod"]
    assert scoped[0]["has_secret"] is True
    assert "secret" not in scoped[0]
    # The bound arg names are surfaced so the run form can treat a required arg
    # the credential fills as satisfied (and not block submission).
    assert scoped[0]["secret_arg"] == "password"
    assert scoped[0]["username_arg"] == ""


def test_backend_reports_available(b):
    info = b.credentials_backend()
    assert info["available"] is True
    assert info["backend"] == "_FakeKeyring"


def test_secret_save_fails_without_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(cred_mod, "_keyring", lambda: None)
    monkeypatch.setattr(bridge_mod, "credentials_path", lambda: tmp_path / "c.toml")
    bridge = _CredBridge()
    assert bridge.credentials_backend() == {"available": False, "backend": ""}
    res = bridge.save_credential({"label": "X", "secret": "s"})
    assert res == {"error": "credential storage unavailable on this system"}
    # Metadata-only save (no secret) still works without a backend.
    assert "id" in bridge.save_credential({"label": "meta only"})
