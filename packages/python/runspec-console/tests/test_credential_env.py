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
    # userpass emits the username under both _USERNAME and the _USER alias, so a
    # credential labelled "Jira" feeds tools that read either spelling.
    entry = {"label": "Jira", "kind": "userpass", "username": "alice"}
    assert env_vars_for(entry, "s3cret") == {
        "JIRA_USERNAME": "alice",
        "JIRA_USER": "alice",
        "JIRA_PASSWORD": "s3cret",
    }


def test_env_vars_for_userpass_two_words():
    entry = {"label": "Jira Dev", "kind": "userpass", "username": "alice"}
    assert env_vars_for(entry, "s3cret") == {
        "JIRA_DEV_USERNAME": "alice",
        "JIRA_DEV_USER": "alice",
        "JIRA_DEV_PASSWORD": "s3cret",
    }


def test_env_vars_for_jsm_and_snow_aliases():
    # The motivating case: a credential labelled to match the pack reads its
    # expected names. runspec-jsm reads JSM_USER + JSM_PAT; runspec-snow reads
    # SNOW_USER + SNOW_TOKEN. Aliases make both work without per-tool config.
    jsm = env_vars_for({"label": "JSM", "kind": "userpass", "username": "svc"}, "pw")
    assert jsm["JSM_USER"] == "svc" and jsm["JSM_PASSWORD"] == "pw"
    pat = env_vars_for({"label": "JSM", "kind": "token"}, "tok123")
    assert pat == {"JSM_TOKEN": "tok123", "JSM_PAT": "tok123"}
    snow = env_vars_for({"label": "SNOW", "kind": "userpass", "username": "svc"}, "pw")
    assert snow["SNOW_USER"] == "svc"
    assert env_vars_for({"label": "SNOW", "kind": "token"}, "t")["SNOW_TOKEN"] == "t"


def test_env_vars_for_password_and_token():
    assert env_vars_for({"label": "DB", "kind": "password"}, "pw") == {
        "DB_PASSWORD": "pw"
    }
    assert env_vars_for({"label": "CI", "kind": "token"}, "tok") == {
        "CI_TOKEN": "tok",
        "CI_PAT": "tok",
    }


def test_env_vars_for_ssh_key_emits_nothing():
    assert env_vars_for({"label": "Repo", "kind": "ssh-key"}, "pp") == {}


def test_env_vars_for_skips_empty_values_and_prefix():
    # No username → only the secret var; no secret → only the username vars.
    assert env_vars_for({"label": "Jira", "kind": "userpass", "username": ""}, "s") == {
        "JIRA_PASSWORD": "s"
    }
    assert env_vars_for({"label": "Jira", "kind": "userpass", "username": "u"}, "") == {
        "JIRA_USERNAME": "u",
        "JIRA_USER": "u",
    }
    # Empty prefix → nothing at all.
    assert env_vars_for({"label": "!!!", "kind": "password"}, "s") == {}


def test_env_var_names_preview_has_no_values():
    assert env_var_names("Jira", "userpass") == [
        "JIRA_USERNAME",
        "JIRA_USER",
        "JIRA_PASSWORD",
    ]
    assert env_var_names("Deploy", "token") == ["DEPLOY_TOKEN", "DEPLOY_PAT"]
    assert env_var_names("Repo", "ssh-key") == []
    assert env_var_names("", "userpass") == []


# ── bridge resolver ─────────────────────────────────────────────────────────────


class _EnvBridge:
    _credential_env = Bridge._credential_env
    _scoped_credentials = Bridge._scoped_credentials
    _scope_matches = staticmethod(Bridge._scope_matches)
    _has_scope = staticmethod(Bridge._has_scope)
    _declared_use = staticmethod(Bridge._declared_use)
    _declared_interface = Bridge._declared_interface
    _credential_entry = Bridge._credential_entry
    _apply_selected_credentials = Bridge._apply_selected_credentials

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
        "JIRA_USER": "alice",
        "JIRA_PASSWORD": "jpw",
        "SNOW_TOKEN": "spw",
        "SNOW_PAT": "spw",
    }
    assert sorted(applied) == ["jira", "snow"]


def test_unknown_id_is_ignored(kr):
    b = _EnvBridge([_cred("jira", label="Jira", username="alice")])
    env, applied = b._credential_env("h", "r", ["jira", "nope"])
    assert env == {
        "JIRA_USERNAME": "alice",
        "JIRA_USER": "alice",
        "JIRA_PASSWORD": "jpw",
    }
    assert applied == ["jira"]


def test_ids_none_uses_in_scope(kr):
    creds = [
        _cred("jira", label="Jira", username="alice", scope_runnables=["deploy"]),
        _cred("snow", label="Snow", kind="token", scope_runnables=["other"]),
    ]
    b = _EnvBridge(creds)
    env, applied = b._credential_env("prod-1", "deploy", None)
    assert env == {
        "JIRA_USERNAME": "alice",
        "JIRA_USER": "alice",
        "JIRA_PASSWORD": "jpw",
    }
    assert applied == ["jira"]


# ── Forms arg-binding (_apply_selected_credentials) ─────────────────────────────


def test_selected_binding_fills_username_argv_and_secret_env(kr):
    # The motivating case: a runnable with args `user` and `password`. The
    # selected credential binds username → user (argv) and secret → password (env
    # channel), so the Forms run works like the agent path does.
    c = _cred(
        "jira",
        label="Jira",
        username="alice",
        username_arg="user",
        secret_arg="password",
    )
    b = _EnvBridge([c])
    exec_args: dict = {}
    secrets: dict = {}
    applied = b._apply_selected_credentials(
        "deploy", exec_args, secrets, ["jira"], valid_args={"user", "password"}
    )
    assert exec_args == {"user": "alice"}  # username on argv (plain, visible)
    assert secrets == {"password": "jpw"}  # secret rides the env channel
    assert applied == ["jira"]


def test_selected_binding_operator_typed_value_wins(kr):
    c = _cred(
        "jira",
        label="Jira",
        username="alice",
        username_arg="user",
        secret_arg="password",
    )
    b = _EnvBridge([c])
    exec_args = {"user": "bob"}  # operator typed a username
    secrets = {"password": "typed-pw"}  # and a password
    applied = b._apply_selected_credentials(
        "deploy", exec_args, secrets, ["jira"], valid_args={"user", "password"}
    )
    assert exec_args == {"user": "bob"} and secrets == {"password": "typed-pw"}
    assert applied == []  # nothing overwritten → nothing applied


def test_selected_binding_only_declared_args(kr):
    # secret_arg not declared by the runnable → not filled (no stray env var).
    c = _cred("jira", label="Jira", username="alice", secret_arg="api_token")
    b = _EnvBridge([c])
    secrets: dict = {}
    applied = b._apply_selected_credentials(
        "deploy", {}, secrets, ["jira"], valid_args={"user"}
    )
    assert secrets == {} and applied == []


def test_selected_binding_pops_secret_arg_from_argv(kr):
    # A non-password secret_arg the operator left blank lands as "" in exec_args;
    # the binding must move it to the env channel and drop the empty argv flag
    # (else CLI would win over the env value and blank the secret).
    c = _cred("jira", label="Jira", username="alice", secret_arg="token")
    b = _EnvBridge([c])
    exec_args = {"token": ""}
    secrets: dict = {}
    b._apply_selected_credentials(
        "deploy", exec_args, secrets, ["jira"], valid_args={"token"}
    )
    assert "token" not in exec_args and secrets == {"token": "jpw"}


def test_selected_binding_ignores_unselected(kr):
    # Empty selection → nothing applied, even for an otherwise-in-scope credential.
    c = _cred("jira", label="Jira", username="alice", username_arg="user")
    b = _EnvBridge([c])
    exec_args: dict = {}
    applied = b._apply_selected_credentials(
        "deploy", exec_args, {}, [], valid_args=None
    )
    assert exec_args == {} and applied == []
