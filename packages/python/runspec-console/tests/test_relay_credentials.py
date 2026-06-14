"""
test_relay_credentials.py — ad-hoc relay credential resolution.

A relay (or its source browser) may reference a saved credential by id instead of
a typed password: the stored secret becomes the SSH password (and the
credential's username fills an empty user), resolved from the keyring at connect
time and never left on the source dict / in_flight / logs. The keyring is faked.
"""

from __future__ import annotations

import threading

import pytest

import runspec_console.credentials as cred_mod
from runspec_console.bridge import Bridge
from runspec_console.credentials import KEYRING_SERVICE


class _FakeKR:
    def __init__(self, secrets):
        self._secrets = secrets

    def get_password(self, service, name):
        assert service == KEYRING_SERVICE
        return self._secrets.get(name)


@pytest.fixture
def kr(monkeypatch):
    monkeypatch.setattr(cred_mod, "_keyring", lambda: _FakeKR({"svc": "s3cret"}))


CRED = {"id": "svc", "label": "svc", "username": "svc_user"}


class _ResolveBridge:
    _resolve_adhoc_credential = Bridge._resolve_adhoc_credential
    _credential_entry = Bridge._credential_entry

    def __init__(self, creds):
        self._credentials = creds

    def _reload_credentials(self):
        pass


def test_resolve_fills_user_and_password(kr):
    b = _ResolveBridge([CRED])
    user, pw = b._resolve_adhoc_credential("svc", None, None)
    assert user == "svc_user"
    assert pw == "s3cret"


def test_typed_password_wins(kr):
    b = _ResolveBridge([CRED])
    user, pw = b._resolve_adhoc_credential("svc", "joe", "typed")
    assert (user, pw) == ("joe", "typed")  # nothing resolved


def test_explicit_user_kept(kr):
    b = _ResolveBridge([CRED])
    user, pw = b._resolve_adhoc_credential("svc", "joe", None)
    assert user == "joe"  # credential username doesn't override a given user
    assert pw == "s3cret"


def test_no_credential_id_unchanged(kr):
    b = _ResolveBridge([CRED])
    assert b._resolve_adhoc_credential(None, "joe", None) == ("joe", None)


def test_unknown_id_unchanged(kr):
    b = _ResolveBridge([CRED])
    assert b._resolve_adhoc_credential("ghost", None, None) == (None, None)


# ── start_relay consumes credential_id and never leaks it ───────────────────────


class _RelayBridge:
    start_relay = Bridge.start_relay
    _resolve_adhoc_credential = Bridge._resolve_adhoc_credential
    _credential_entry = Bridge._credential_entry
    _emit_transfer_end = Bridge._emit_transfer_end
    _await_transfer = Bridge._await_transfer

    def __init__(self, creds):
        self._credentials = creds
        self._lock = threading.Lock()
        self._in_flight: dict = {}
        self._recent_transfer_results: dict = {}
        self.captured: dict = {}

    def _reload_credentials(self):
        pass

    def _host_entry(self, h):
        return {"name": h, "ssh": f"deploy@{h}"}

    def get_config(self):
        return {"ssh": {}}

    def _current_user(self):
        return "tester"

    def _dispatch(self, *a, **k):
        pass

    def _open_source_conn(self, source, password, cfg):
        self.captured["password"] = password
        self.captured["user"] = source.get("user")
        self.captured["credential_id_present"] = "credential_id" in source
        raise RuntimeError("stop")  # abort after capturing

    def _open_sftp(self, *a, **k):
        raise RuntimeError("stop")


def test_start_relay_resolves_and_strips_credential_id(kr):
    b = _RelayBridge([CRED])
    res = b.start_relay(
        {"kind": "adhoc", "host": "target", "jump": "jumpx", "credential_id": "svc"},
        ["/var/log/app.log"],
        "dest",
        "/incoming",
    )
    assert "id" in res
    b._await_transfer(res["id"], timeout=2.0)
    assert b.captured["password"] == "s3cret"  # secret pulled from keyring
    assert b.captured["user"] == "svc_user"  # username filled from credential
    assert b.captured["credential_id_present"] is False  # never left on the source
