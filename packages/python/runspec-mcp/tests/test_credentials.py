"""Credential env derivation + the fleet's new reverse-match logic."""

from __future__ import annotations

from runspec_mcp import credentials as cred


def test_env_prefix():
    assert cred.env_prefix("Jira") == "JIRA"
    assert cred.env_prefix("Jira Dev") == "JIRA_DEV"
    assert cred.env_prefix("Windows") == "WINDOWS"
    assert cred.env_prefix("!!!") == ""


def test_env_vars_for_userpass():
    entry = {"label": "Windows", "kind": "userpass", "username": "svc"}
    out = cred.env_vars_for(entry, "pw")
    assert out == {
        "WINDOWS_USERNAME": "svc",
        "WINDOWS_USER": "svc",
        "WINDOWS_PASSWORD": "pw",
    }


def test_env_vars_for_token():
    out = cred.env_vars_for({"label": "Deploy", "kind": "token"}, "tok")
    assert out == {"DEPLOY_TOKEN": "tok", "DEPLOY_PAT": "tok"}


def test_credential_prefix_for_env_roundtrip():
    # Every name env_vars_for can emit must reverse to the original prefix.
    for label, kind in [("Windows", "userpass"), ("Deploy", "token"), ("DB", "password")]:
        prefix = cred.env_prefix(label)
        for name in cred.env_var_names(label, kind):
            assert cred.credential_prefix_for_env(name) == prefix


def test_credential_prefix_longest_suffix_wins():
    # _USERNAME must win over _USER so WINDOWS_USERNAME → WINDOWS, not WINDOWS_USER-something.
    assert cred.credential_prefix_for_env("WINDOWS_USERNAME") == "WINDOWS"
    assert cred.credential_prefix_for_env("WINDOWS_USER") == "WINDOWS"
    assert cred.credential_prefix_for_env("DEPLOY_TOKEN") == "DEPLOY"
    # A name with no known credential suffix can't have come from a credential.
    assert cred.credential_prefix_for_env("NO_KNOWN_SUFFIX_HERE") is None
    assert cred.credential_prefix_for_env("PLAINWORD") is None


def test_match_credentials_auto_by_arg_env():
    entries = [
        {"id": "Windows", "label": "Windows", "kind": "userpass"},
        {"id": "Jira", "label": "Jira", "kind": "userpass"},
    ]
    matched = cred.match_credentials(["WINDOWS_USERNAME", "WINDOWS_PASSWORD"], "prod", "deploy", entries)
    assert [m["id"] for m in matched] == ["Windows"]


def test_match_credentials_scope_glob():
    entries = [
        {"id": "JSM", "label": "JSM", "kind": "token", "scope_hosts": ["prod-*"]},
        {"id": "Other", "label": "Other", "kind": "token", "scope_hosts": ["dev-*"]},
    ]
    # No arg env declared (bare-env runnable) — scope decides.
    matched = cred.match_credentials([], "prod-1", "snow", entries)
    assert [m["id"] for m in matched] == ["JSM"]


def test_match_credentials_unscoped_not_force_injected():
    # A credential with neither an arg-env hit nor a declared scope is NOT injected
    # everywhere (that would leak every credential into every run).
    entries = [{"id": "Loose", "label": "Loose", "kind": "token"}]
    assert cred.match_credentials([], "prod", "deploy", entries) == []


def test_match_credentials_dedup_and_union():
    entries = [
        {"id": "Windows", "label": "Windows", "kind": "userpass", "scope_hosts": ["*"]},
    ]
    # Matches by BOTH arg env and scope — still appears once.
    matched = cred.match_credentials(["WINDOWS_USERNAME"], "any", "any", entries)
    assert [m["id"] for m in matched] == ["Windows"]
