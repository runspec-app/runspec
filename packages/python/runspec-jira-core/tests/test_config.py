import pytest

from runspec_jira_core.config import JiraConfig, config_from_env


def test_cloud_basic_from_env(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://acme.atlassian.net")
    monkeypatch.setenv("JIRA_USERNAME", "me@acme.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    cfg = config_from_env()
    assert cfg.url == "https://acme.atlassian.net"
    assert cfg.cloud is True
    assert cfg.username == "me@acme.com"
    assert cfg.api_token == "tok"
    assert cfg.personal_token is None
    assert cfg.env is None


def test_dc_pat_from_env_is_not_cloud(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://jira.corp.example/")
    monkeypatch.setenv("JIRA_PERSONAL_TOKEN", "pat123")
    cfg = config_from_env()
    assert cfg.url == "https://jira.corp.example"  # trailing slash stripped
    assert cfg.cloud is False
    assert cfg.personal_token == "pat123"


def test_cloud_override_wins(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://jira.corp.example")
    monkeypatch.setenv("JIRA_PERSONAL_TOKEN", "pat")
    monkeypatch.setenv("JIRA_CLOUD", "true")
    assert config_from_env().cloud is True


def test_env_prefixed_resolution(monkeypatch):
    monkeypatch.setenv("JIRA_UAT_URL", "https://uat.atlassian.net")
    monkeypatch.setenv("JIRA_UAT_USERNAME", "u@x.com")
    monkeypatch.setenv("JIRA_UAT_API_TOKEN", "uat-tok")
    cfg = config_from_env(env="uat")
    assert cfg.url == "https://uat.atlassian.net"
    assert cfg.api_token == "uat-tok"
    assert cfg.env == "uat"


def test_env_prefixed_falls_back_to_unprefixed(monkeypatch):
    monkeypatch.setenv("JIRA_PROD_URL", "https://prod.atlassian.net")
    monkeypatch.setenv("JIRA_USERNAME", "shared@x.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "shared-tok")
    cfg = config_from_env(env="prod")
    assert cfg.url == "https://prod.atlassian.net"
    assert cfg.api_token == "shared-tok"


def test_env_from_jira_env_var(monkeypatch):
    monkeypatch.setenv("JIRA_ENV", "dev")
    monkeypatch.setenv("JIRA_DEV_URL", "https://dev.atlassian.net")
    monkeypatch.setenv("JIRA_DEV_PERSONAL_TOKEN", "d")
    cfg = config_from_env()
    assert cfg.env == "dev"
    assert cfg.personal_token == "d"


def test_invalid_env_raises(monkeypatch):
    with pytest.raises(Exception, match="Invalid env"):
        config_from_env(env="staging")


def test_missing_url_raises(monkeypatch):
    monkeypatch.setenv("JIRA_PERSONAL_TOKEN", "p")
    with pytest.raises(Exception, match="No base URL"):
        config_from_env()


def test_missing_credentials_raises(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://acme.atlassian.net")
    with pytest.raises(Exception, match="No credentials"):
        config_from_env()


def test_username_without_token_is_insufficient(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://acme.atlassian.net")
    monkeypatch.setenv("JIRA_USERNAME", "me@acme.com")
    with pytest.raises(Exception, match="No credentials"):
        config_from_env()


def test_verify_ssl_false(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://jira.corp")
    monkeypatch.setenv("JIRA_PERSONAL_TOKEN", "p")
    monkeypatch.setenv("JIRA_VERIFY_SSL", "false")
    assert config_from_env().verify_ssl is False


def test_dataclass_defaults():
    cfg = JiraConfig(url="https://x", cloud=True)
    assert cfg.verify_ssl is True
    assert cfg.service_desk_id is None


def test_console_credential_spellings(monkeypatch):
    # A runspec-console credential labelled Jira sets JIRA_USER/JIRA_PASSWORD
    # (username + password kind) or JIRA_PAT/JIRA_TOKEN (token kind).
    monkeypatch.setenv("JIRA_URL", "https://jira.corp.example")
    monkeypatch.setenv("JIRA_USER", "ops")
    monkeypatch.setenv("JIRA_PASSWORD", "pw")
    cfg = config_from_env()
    assert (cfg.username, cfg.api_token, cfg.personal_token) == ("ops", "pw", None)
    monkeypatch.setenv("JIRA_PAT", "pat")
    assert config_from_env().personal_token == "pat"


def test_explicit_url_and_basic_auth_win(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://env.example")
    monkeypatch.setenv("JIRA_PERSONAL_TOKEN", "env-pat")
    cfg = config_from_env(url="https://arg.example/", username="me", api_token="tok")
    assert (cfg.url, cfg.username, cfg.api_token, cfg.personal_token) == ("https://arg.example", "me", "tok", None)
