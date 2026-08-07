"""personal_token_override on config_from_env (feeds the --pat secret arg)."""

import pytest

from runspec_jira_core import config_from_env

# The autouse _clean_env fixture (conftest.py) clears all JIRA_* vars first.


def test_override_without_env_creds(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://acme.atlassian.net")
    cfg = config_from_env(personal_token_override="pat-xyz")
    assert cfg.personal_token == "pat-xyz"
    assert cfg.username is None and cfg.api_token is None
    assert cfg.cloud is True


def test_override_beats_env_basic(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://acme.atlassian.net")
    monkeypatch.setenv("JIRA_USERNAME", "me@x")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    cfg = config_from_env(personal_token_override="pat-xyz")
    assert cfg.personal_token == "pat-xyz"
    assert cfg.username is None and cfg.api_token is None


def test_override_still_requires_url(monkeypatch):
    with pytest.raises(Exception, match="No base URL"):
        config_from_env(personal_token_override="pat")


def test_empty_override_falls_through_to_env(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://acme.atlassian.net")
    monkeypatch.setenv("JIRA_PERSONAL_TOKEN", "envpat")
    assert config_from_env(personal_token_override=None).personal_token == "envpat"
