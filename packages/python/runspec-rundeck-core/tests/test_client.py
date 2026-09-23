"""Unit tests for config resolution + the request helper."""

from __future__ import annotations

import httpx
import pytest

from runspec_rundeck_core import (
    DEFAULT_API_VERSION,
    RundeckConfig,
    RundeckError,
    config_from_env,
    rundeck_request,
)

from .conftest import mock_request


def test_config_from_env_reads_unprefixed(monkeypatch):
    monkeypatch.setenv("RUNDECK_URL", "https://rd.example.com/")
    monkeypatch.setenv("RUNDECK_TOKEN", "tok")
    monkeypatch.setenv("RUNDECK_PROJECT", "ops")
    cfg = config_from_env()
    assert cfg.url == "https://rd.example.com"  # trailing slash stripped
    assert cfg.token == "tok"
    assert cfg.project == "ops"
    assert cfg.api_version == DEFAULT_API_VERSION
    assert cfg.env is None


def test_config_from_env_prefix_wins(monkeypatch):
    monkeypatch.setenv("RUNDECK_URL", "https://base")
    monkeypatch.setenv("RUNDECK_TOKEN", "base-tok")
    monkeypatch.setenv("RUNDECK_PROD_EU_URL", "https://eu")
    monkeypatch.setenv("RUNDECK_PROD_EU_TOKEN", "eu-tok")
    monkeypatch.setenv("RUNDECK_PROD_EU_PROJECT", "eu-ops")
    cfg = config_from_env("prod-eu")  # hyphen → underscore in the prefix
    assert cfg.url == "https://eu"
    assert cfg.token == "eu-tok"
    assert cfg.project == "eu-ops"
    assert cfg.env == "prod-eu"


def test_config_from_env_falls_back_to_unprefixed(monkeypatch):
    monkeypatch.setenv("RUNDECK_URL", "https://base")
    monkeypatch.setenv("RUNDECK_TOKEN", "base-tok")
    # No RUNDECK_PROD_* — the label still resolves via the unprefixed vars.
    cfg = config_from_env("prod")
    assert cfg.url == "https://base"
    assert cfg.token == "base-tok"


def test_config_from_env_missing_url_raises(monkeypatch):
    monkeypatch.setenv("RUNDECK_TOKEN", "tok")
    with pytest.raises(RundeckError, match="URL"):
        config_from_env()


def test_config_from_env_missing_token_raises(monkeypatch):
    monkeypatch.setenv("RUNDECK_URL", "https://rd")
    with pytest.raises(RundeckError, match="token"):
        config_from_env()


def test_config_from_env_bad_api_version(monkeypatch):
    monkeypatch.setenv("RUNDECK_URL", "https://rd")
    monkeypatch.setenv("RUNDECK_TOKEN", "tok")
    monkeypatch.setenv("RUNDECK_API_VERSION", "notanint")
    with pytest.raises(RundeckError, match="API_VERSION"):
        config_from_env()


def test_config_from_env_overrides_win(monkeypatch):
    monkeypatch.setenv("RUNDECK_URL", "https://env")
    monkeypatch.setenv("RUNDECK_TOKEN", "env-tok")
    cfg = config_from_env(url="https://arg", token="arg-tok", project="p", api_version=44, tls_verify=False)
    assert cfg.url == "https://arg"
    assert cfg.token == "arg-tok"
    assert cfg.project == "p"
    assert cfg.api_version == 44
    assert cfg.tls_verify is False


def test_config_from_env_override_supplies_missing(monkeypatch):
    # No env vars at all — overrides alone satisfy the required url/token.
    cfg = config_from_env(url="https://arg", token="arg-tok")
    assert cfg.url == "https://arg"
    assert cfg.token == "arg-tok"


def _cfg(**over):
    base = dict(url="https://rd", token="tok", project="ops", api_version=41)
    base.update(over)
    return RundeckConfig(**base)


def test_api_url_built():
    cfg = _cfg()
    assert cfg.api("/project/ops/jobs") == "https://rd/api/41/project/ops/jobs"


def test_require_project_raises_when_unset():
    cfg = _cfg(project=None)
    with pytest.raises(RundeckError, match="project"):
        cfg.require_project()
    assert cfg.require_project("explicit") == "explicit"


def test_request_sends_token_and_parses_json(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("X-Rundeck-Auth-Token")
        seen["accept"] = request.headers.get("Accept")
        seen["path"] = request.url.path
        return httpx.Response(200, json={"ok": True})

    mock_request(monkeypatch, handler)
    result = rundeck_request("GET", "/project/ops/jobs", config=_cfg())
    assert result == {"ok": True}
    assert seen["token"] == "tok"
    assert seen["accept"] == "application/json"
    assert seen["path"] == "/api/41/project/ops/jobs"


def test_request_drops_none_params_and_renders_bools(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    mock_request(monkeypatch, handler)
    rundeck_request("GET", "/x", config=_cfg(), params={"a": None, "b": True, "c": "v"})
    assert seen["query"] == {"b": "true", "c": "v"}


def test_request_raises_on_error_with_message(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "not authorized"})

    mock_request(monkeypatch, handler)
    with pytest.raises(RundeckError, match="403.*not authorized"):
        rundeck_request("GET", "/x", config=_cfg())


def test_request_returns_none_on_204(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    mock_request(monkeypatch, handler)
    assert rundeck_request("DELETE", "/job/abc", config=_cfg()) is None
