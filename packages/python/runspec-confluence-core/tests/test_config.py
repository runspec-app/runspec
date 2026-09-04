import sys

import pytest

from runspec_confluence_core import config as config_mod
from runspec_confluence_core.config import ConfluenceConfig, config_from_env, detect_verify


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
    assert cfg.proxies is None
    assert cfg.retry is True


def test_retry_default_true_from_env(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_URL", "https://acme.atlassian.net/wiki")
    monkeypatch.setenv("CONFLUENCE_PERSONAL_TOKEN", "p")
    assert config_from_env().retry is True


def test_retry_disabled_from_env(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_URL", "https://acme.atlassian.net/wiki")
    monkeypatch.setenv("CONFLUENCE_PERSONAL_TOKEN", "p")
    monkeypatch.setenv("CONFLUENCE_RETRY", "false")
    assert config_from_env().retry is False


def test_retry_caps_default_on_dataclass():
    from runspec_confluence_core import DEFAULT_MAX_BACKOFF_RETRIES, DEFAULT_MAX_BACKOFF_SECONDS

    cfg = ConfluenceConfig(url="https://x", cloud=True)
    assert cfg.max_backoff_retries == DEFAULT_MAX_BACKOFF_RETRIES == 5
    assert cfg.max_backoff_seconds == DEFAULT_MAX_BACKOFF_SECONDS == 30


def test_retry_caps_from_env(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_URL", "https://acme.atlassian.net/wiki")
    monkeypatch.setenv("CONFLUENCE_PERSONAL_TOKEN", "p")
    monkeypatch.setenv("CONFLUENCE_MAX_BACKOFF_RETRIES", "8")
    monkeypatch.setenv("CONFLUENCE_MAX_BACKOFF_SECONDS", "90")
    cfg = config_from_env()
    assert cfg.max_backoff_retries == 8
    assert cfg.max_backoff_seconds == 90


def test_retry_caps_env_non_numeric_falls_back(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_URL", "https://acme.atlassian.net/wiki")
    monkeypatch.setenv("CONFLUENCE_PERSONAL_TOKEN", "p")
    monkeypatch.setenv("CONFLUENCE_MAX_BACKOFF_RETRIES", "lots")
    assert config_from_env().max_backoff_retries == 5


def test_resolve_verify_bool():
    assert ConfluenceConfig(url="x", cloud=True, verify_ssl=True).resolve_verify() is True
    assert ConfluenceConfig(url="x", cloud=True, verify_ssl=False).resolve_verify() is False


def test_resolve_verify_path():
    cfg = ConfluenceConfig(url="x", cloud=True, verify_ssl="/etc/ssl/corp-ca.pem")
    assert cfg.resolve_verify() == "/etc/ssl/corp-ca.pem"


def test_resolve_verify_empty_string_defaults_true():
    assert ConfluenceConfig(url="x", cloud=True, verify_ssl="").resolve_verify() is True


def test_proxy_url_from_env(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_URL", "https://acme.atlassian.net/wiki")
    monkeypatch.setenv("CONFLUENCE_PERSONAL_TOKEN", "p")
    monkeypatch.setenv("CONFLUENCE_PROXY_URL", "http://proxy.corp:8080")
    cfg = config_from_env()
    assert cfg.proxies == {"http": "http://proxy.corp:8080", "https": "http://proxy.corp:8080"}


def test_no_proxy_url_leaves_none(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_URL", "https://acme.atlassian.net/wiki")
    monkeypatch.setenv("CONFLUENCE_PERSONAL_TOKEN", "p")
    assert config_from_env().proxies is None


def test_verify_ssl_falsey_from_env(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_URL", "https://acme.atlassian.net/wiki")
    monkeypatch.setenv("CONFLUENCE_PERSONAL_TOKEN", "p")
    monkeypatch.setenv("CONFLUENCE_VERIFY_SSL", "false")
    assert config_from_env().verify_ssl is False


def test_verify_ssl_path_from_env(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_URL", "https://acme.atlassian.net/wiki")
    monkeypatch.setenv("CONFLUENCE_PERSONAL_TOKEN", "p")
    monkeypatch.setenv("CONFLUENCE_VERIFY_SSL", "/etc/ssl/corp-ca.pem")
    cfg = config_from_env()
    assert cfg.verify_ssl == "/etc/ssl/corp-ca.pem"
    assert cfg.resolve_verify() == "/etc/ssl/corp-ca.pem"


def test_detect_verify_never_false():
    result = detect_verify()
    assert result is not False
    assert result is True or isinstance(result, str)


def test_detect_verify_returns_system_bundle(monkeypatch, tmp_path):
    # Force the no-truststore path, then point the probe at a real file.
    monkeypatch.setitem(sys.modules, "truststore", None)
    bundle = tmp_path / "ca-bundle.crt"
    bundle.write_text("-----BEGIN CERTIFICATE-----")
    monkeypatch.setattr(config_mod, "_SYSTEM_CA_BUNDLES", (str(tmp_path / "missing.pem"), str(bundle)))
    assert detect_verify() == str(bundle)


def test_detect_verify_falls_back_to_true(monkeypatch):
    monkeypatch.setitem(sys.modules, "truststore", None)
    monkeypatch.setattr(config_mod, "_SYSTEM_CA_BUNDLES", ("/nope/a.pem", "/nope/b.pem"))
    assert detect_verify() is True
