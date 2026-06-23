"""
test_credential_env.py — name-derived credential env vars.

A credential applied to a run (the form's credential section, or an agent invoke)
is injected as plain env vars whose names derive from its label + kind by a strict
convention (label "Jira" + userpass → JIRA_USERNAME/JIRA_PASSWORD). This covers
the pure derivation helpers in ``credentials`` and the ``Bridge._credential_env``
resolver (explicit-id select and in-scope select). The keyring is faked.
"""

from __future__ import annotations

import pytest

import runspec_console.credentials as cred_mod
from runspec_console.bridge import Bridge
from runspec_console.credentials import (
    KEYRING_SERVICE,
    env_prefix,
    env_var_names,
    env_vars_for,
)


class _FakeKR:
    def __init__(self, secrets: dict[str, str]):
        self._secrets = secrets

    def get_password(self, service, name):
        assert service == KEYRING_SERVICE
        return self._secrets.get(name)


# ── pure derivation ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "label,expected",
    [
        ("Jira", "JIRA"),
        ("Jira Dev", "JIRA_DEV"),
        ("my-svc.acct", "MY_SVC_ACCT"),
        ("  spaced  ", "SPACED"),
        ("__weird__", "WEIRD"),
        ("", ""),
        ("!!!", ""),
    ],
)
def test_env_prefix(label, expected):
    assert env_prefix(label) == expected


def test_env_vars_for_userpass():
    entry = {"label": "Jira", "kind": "userpass", "username": "alice"}
    assert env_vars_for(entry, "s3cret") == {
        "JIRA_USERNAME": "alice",
        "JIRA_PASSWORD": "s3cret",
    }


def test_env_vars_for_userpass_two_words():
    entry = {"label": "Jira Dev", "kind": "userpass", "username": "alice"}
    assert env_vars_for(entry, "s3cret") == {
        "JIRA_DEV_USERNAME": "alice",
        "JIRA_DEV_PASSWORD": "s3cret",
    }


def test_env_vars_for_password_and_token():
    assert env_vars_for({"label": "DB", "kind": "password"}, "pw") == {
        "DB_PASSWORD": "pw"
    }
    assert env_vars_for({"label": "CI", "kind": "token"}, "tok") == {"CI_TOKEN": "tok"}


def test_env_vars_for_ssh_key_emits_nothing():
    assert env_vars_for({"label": "Repo", "kind": "ssh-key"}, "pp") == {}


def test_env_vars_for_skips_empty_values_and_prefix():
    # No username → only the secret var; no secret → only the username var.
    assert env_vars_for({"label": "Jira", "kind": "userpass", "username": ""}, "s") == {
        "JIRA_PASSWORD": "s"
    }
    assert env_vars_for({"label": "Jira", "kind": "userpass", "username": "u"}, "") == {
        "JIRA_USERNAME": "u"
    }
    # Empty prefix → nothing at all.
    assert env_vars_for({"label": "!!!", "kind": "password"}, "s") == {}


def test_env_var_names_preview_has_no_values():
    assert env_var_names("Jira", "userpass") == ["JIRA_USERNAME", "JIRA_PASSWORD"]
    assert env_var_names("Deploy", "token") == ["DEPLOY_TOKEN"]
    assert env_var_names("Repo", "ssh-key") == []
    assert env_var_names("", "userpass") == []


# ── bridge resolver ─────────────────────────────────────────────────────────────


class _EnvBridge:
    _credential_env = Bridge._credential_env
    _scoped_credentials = Bridge._scoped_credentials
    _scope_matches = staticmethod(Bridge._scope_matches)
    _credential_entry = Bridge._credential_entry

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
        "scope_hosts": [],
        "scope_runnables": [],
    }
    base.update(kw)
    return base


@pytest.fixture
def kr(monkeypatch):
    secrets = {"jira": "jpw", "snow": "spw"}
    monkeypatch.setattr(cred_mod, "_keyring", lambda: _FakeKR(secrets))
    return secrets


def test_explicit_ids_inject_env(kr):
    creds = [
        _cred("jira", label="Jira", username="alice"),
        _cred("snow", label="Snow", kind="token"),
    ]
    b = _EnvBridge(creds)
    env, applied = b._credential_env("any-host", "any-runnable", ["jira", "snow"])
    assert env == {
        "JIRA_USERNAME": "alice",
        "JIRA_PASSWORD": "jpw",
        "SNOW_TOKEN": "spw",
    }
    assert sorted(applied) == ["jira", "snow"]


def test_unknown_id_is_ignored(kr):
    b = _EnvBridge([_cred("jira", label="Jira", username="alice")])
    env, applied = b._credential_env("h", "r", ["jira", "nope"])
    assert env == {"JIRA_USERNAME": "alice", "JIRA_PASSWORD": "jpw"}
    assert applied == ["jira"]


def test_ids_none_uses_in_scope(kr):
    creds = [
        _cred("jira", label="Jira", username="alice", scope_runnables=["deploy"]),
        _cred("snow", label="Snow", kind="token", scope_runnables=["other"]),
    ]
    b = _EnvBridge(creds)
    env, applied = b._credential_env("prod-1", "deploy", None)
    assert env == {"JIRA_USERNAME": "alice", "JIRA_PASSWORD": "jpw"}
    assert applied == ["jira"]
