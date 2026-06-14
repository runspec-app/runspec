"""
test_apply_credentials.py — the credential resolver (_apply_credentials).

In-scope credentials auto-apply (username → argv, secret → the env channel); an
explicit credential_id applies even out of scope and wins per-arg conflicts; a
caller-supplied value is never overwritten; two in-scope credentials binding the
same arg with no explicit winner is ambiguous and skipped; auto-injection is
limited to args the runnable declares. The keyring is faked.
"""

from __future__ import annotations

import pytest

import runspec_console.credentials as cred_mod
from runspec_console.bridge import Bridge
from runspec_console.credentials import KEYRING_SERVICE


class _FakeKR:
    def __init__(self, secrets: dict[str, str]):
        self._secrets = secrets

    def get_password(self, service, name):
        assert service == KEYRING_SERVICE
        return self._secrets.get(name)


class _ApplyBridge:
    _apply_credentials = Bridge._apply_credentials
    _scoped_credentials = Bridge._scoped_credentials
    _scope_matches = staticmethod(Bridge._scope_matches)
    _credential_entry = Bridge._credential_entry
    _declared_arg_names = staticmethod(Bridge._declared_arg_names)

    def __init__(self, creds):
        self._credentials = creds

    def _reload_credentials(self):
        pass  # keep the injected cache


def _cred(cid, **kw):
    base = {
        "id": cid,
        "label": cid,
        "kind": "userpass",
        "username": "",
        "username_arg": "",
        "secret_arg": "",
        "scope_hosts": [],
        "scope_runnables": [],
    }
    base.update(kw)
    return base


@pytest.fixture
def kr(monkeypatch):
    secrets = {"svc-prod": "s3cret", "svc-dev": "devpass", "other": "oo"}
    monkeypatch.setattr(cred_mod, "_keyring", lambda: _FakeKR(secrets))
    return secrets


VALID = {"service-username", "user-password"}


def test_auto_apply_in_scope(kr):
    c = _cred(
        "svc-prod",
        username="svc",
        username_arg="service-username",
        secret_arg="user-password",
        scope_hosts=["prod-*"],
        scope_runnables=["deploy"],
    )
    b = _ApplyBridge([c])
    exec_args: dict = {}
    secrets: dict = {}
    applied = b._apply_credentials(
        "prod-1", "deploy", exec_args, secrets, valid_args=VALID
    )
    assert exec_args == {"service-username": "svc"}
    assert secrets == {"user-password": "s3cret"}
    assert applied == ["svc-prod"]


def test_out_of_scope_not_applied(kr):
    c = _cred(
        "svc-prod",
        username="svc",
        username_arg="service-username",
        secret_arg="user-password",
        scope_hosts=["prod-*"],
    )
    b = _ApplyBridge([c])
    exec_args: dict = {}
    secrets: dict = {}
    applied = b._apply_credentials(
        "dev-1", "deploy", exec_args, secrets, valid_args=VALID
    )
    assert exec_args == {} and secrets == {} and applied == []


def test_explicit_id_applies_out_of_scope_and_wins(kr):
    in_scope = _cred(
        "svc-dev",
        username="dev",
        username_arg="service-username",
        secret_arg="user-password",
        scope_runnables=["deploy"],
    )
    explicit = _cred(
        "svc-prod",
        username="prod",
        username_arg="service-username",
        secret_arg="user-password",
        scope_hosts=["nowhere-*"],
    )
    b = _ApplyBridge([in_scope, explicit])
    exec_args: dict = {}
    secrets: dict = {}
    applied = b._apply_credentials(
        "host", "deploy", exec_args, secrets, explicit_id="svc-prod", valid_args=VALID
    )
    # explicit wins the shared args even though svc-dev was in scope
    assert exec_args == {"service-username": "prod"}
    assert secrets == {"user-password": "s3cret"}
    assert applied == ["svc-prod"]


def test_caller_supplied_not_overwritten(kr):
    c = _cred(
        "svc-prod",
        username="svc",
        username_arg="service-username",
        secret_arg="user-password",
    )
    b = _ApplyBridge([c])
    exec_args = {"service-username": "mine"}
    secrets = {"user-password": "minepw"}
    b._apply_credentials("h", "r", exec_args, secrets, valid_args=VALID)
    assert exec_args == {"service-username": "mine"}
    assert secrets == {"user-password": "minepw"}


def test_ambiguous_binding_skipped(kr):
    a = _cred("svc-prod", username="a", username_arg="service-username")
    b_ = _cred("svc-dev", username="b", username_arg="service-username")
    bridge = _ApplyBridge([a, b_])
    exec_args: dict = {}
    secrets: dict = {}
    applied = bridge._apply_credentials("h", "r", exec_args, secrets, valid_args=VALID)
    assert exec_args == {}  # two creds want the same arg, no explicit → skip
    assert applied == []


def test_explicit_resolves_ambiguity(kr):
    a = _cred("svc-prod", username="a", username_arg="service-username")
    b_ = _cred("svc-dev", username="b", username_arg="service-username")
    bridge = _ApplyBridge([a, b_])
    exec_args: dict = {}
    secrets: dict = {}
    bridge._apply_credentials(
        "h", "r", exec_args, secrets, explicit_id="svc-dev", valid_args=VALID
    )
    assert exec_args == {"service-username": "b"}


def test_auto_skips_undeclared_arg_but_explicit_applies(kr):
    c = _cred("svc-prod", username="svc", username_arg="nonexistent-arg")
    # auto: arg not declared → skipped
    b1 = _ApplyBridge([c])
    ea: dict = {}
    b1._apply_credentials("h", "r", ea, {}, valid_args=VALID)
    assert ea == {}
    # explicit: applied regardless of valid_args
    b2 = _ApplyBridge([c])
    ea2: dict = {}
    b2._apply_credentials("h", "r", ea2, {}, explicit_id="svc-prod", valid_args=VALID)
    assert ea2 == {"nonexistent-arg": "svc"}


def test_missing_secret_skipped(kr):
    c = _cred("no-secret-id", secret_arg="user-password")
    b = _ApplyBridge([c])
    secrets: dict = {}
    applied = b._apply_credentials("h", "r", {}, secrets, valid_args=VALID)
    assert secrets == {} and applied == []


def test_unknown_explicit_id_ignored(kr):
    b = _ApplyBridge([])
    applied = b._apply_credentials(
        "h", "r", {}, {}, explicit_id="ghost", valid_args=VALID
    )
    assert applied == []
