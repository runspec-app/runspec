import pytest

from runspec_confluence_core.config import ConfluenceConfig, config_from_env


def test_cloud_basic_from_env(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_URL", "https://acme.atlassian.net/wiki")
    monkeypatch.setenv("CONFLUENCE_USERNAME", "me@acme.com")
    monkeypatch.setenv("CONFLUENCE_API_TOKEN", "tok")
    cfg = config_from_env()
    assert cfg.cloud is True
    assert cfg.username == "me@acme.com"
    assert cfg.api_token == "tok"


def test_dc_pat_not_cloud_and_strips_slash(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_URL", "https://wiki.corp.example/")
    monkeypatch.setenv("CONFLUENCE_PERSONAL_TOKEN", "pat")
    cfg = config_from_env()
    assert cfg.url == "https://wiki.corp.example"
    assert cfg.cloud is False
    assert cfg.personal_token == "pat"


def test_cloud_override(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_URL", "https://wiki.corp.example")
    monkeypatch.setenv("CONFLUENCE_PERSONAL_TOKEN", "pat")
    monkeypatch.setenv("CONFLUENCE_CLOUD", "yes")
    assert config_from_env().cloud is True


def test_env_prefixed_resolution(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_UAT_URL", "https://uat.atlassian.net/wiki")
    monkeypatch.setenv("CONFLUENCE_UAT_PERSONAL_TOKEN", "uat")
    cfg = config_from_env(env="uat")
    assert cfg.personal_token == "uat"
    assert cfg.env == "uat"


def test_env_prefixed_falls_back(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_PROD_URL", "https://prod.atlassian.net/wiki")
    monkeypatch.setenv("CONFLUENCE_USERNAME", "u@x")
    monkeypatch.setenv("CONFLUENCE_API_TOKEN", "shared")
    cfg = config_from_env(env="prod")
    assert cfg.api_token == "shared"


def test_invalid_env(monkeypatch):
    with pytest.raises(Exception, match="Invalid env"):
        config_from_env(env="staging")


def test_missing_url(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_PERSONAL_TOKEN", "p")
    with pytest.raises(Exception, match="No base URL"):
        config_from_env()


def test_missing_credentials(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_URL", "https://acme.atlassian.net/wiki")
    with pytest.raises(Exception, match="No credentials"):
        config_from_env()


def test_space_default(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_URL", "https://acme.atlassian.net/wiki")
    monkeypatch.setenv("CONFLUENCE_PERSONAL_TOKEN", "p")
    monkeypatch.setenv("CONFLUENCE_SPACE", "DOCS")
    assert config_from_env().space == "DOCS"


def test_dataclass_defaults():
    cfg = ConfluenceConfig(url="https://x", cloud=True)
    assert cfg.verify_ssl is True
    assert cfg.space is None
