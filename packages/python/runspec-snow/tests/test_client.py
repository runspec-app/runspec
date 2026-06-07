import base64

import httpx
import pytest

from runspec_snow import client as client_mod
from runspec_snow.client import (
    SnowConfig,
    SnowError,
    _auth_header,
    config_from_env,
    resolve_table,
    snow_request,
)

# Every SNOW_* var any test might set — cleared up front so the host environment
# never leaks in.
_ALL_VARS = [
    "SNOW_ENV",
    "SNOW_BASE_URL",
    "SNOW_INSTANCE",
    "SNOW_TOKEN",
    "SNOW_USER",
    "SNOW_PASSWORD",
    "SNOW_TABLE",
]
for _env in ("DEV", "UAT", "PROD"):
    _ALL_VARS += [f"SNOW_{_env}_{suffix}" for suffix in ("BASE_URL", "INSTANCE", "TOKEN", "USER", "PASSWORD", "TABLE")]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)


def test_config_from_env_token_unprefixed(monkeypatch):
    monkeypatch.setenv("SNOW_BASE_URL", "https://acme.service-now.com/")
    monkeypatch.setenv("SNOW_TOKEN", "tok123")
    cfg = config_from_env()
    assert cfg.base_url == "https://acme.service-now.com"  # trailing slash stripped
    assert cfg.token == "tok123"
    assert cfg.env is None


def test_instance_alias_for_base_url(monkeypatch):
    monkeypatch.setenv("SNOW_INSTANCE", "https://acme.service-now.com")
    monkeypatch.setenv("SNOW_TOKEN", "tok")
    cfg = config_from_env()
    assert cfg.base_url == "https://acme.service-now.com"


def test_token_wins_over_basic(monkeypatch):
    monkeypatch.setenv("SNOW_BASE_URL", "https://acme.service-now.com")
    monkeypatch.setenv("SNOW_TOKEN", "tok")
    monkeypatch.setenv("SNOW_USER", "alice")
    monkeypatch.setenv("SNOW_PASSWORD", "pw")
    cfg = config_from_env()
    assert _auth_header(cfg) == {"Authorization": "Bearer tok"}


def test_basic_auth_when_no_token(monkeypatch):
    monkeypatch.setenv("SNOW_BASE_URL", "https://acme.service-now.com")
    monkeypatch.setenv("SNOW_USER", "alice")
    monkeypatch.setenv("SNOW_PASSWORD", "s3cret")
    cfg = config_from_env()
    expected = "Basic " + base64.b64encode(b"alice:s3cret").decode()
    assert _auth_header(cfg) == {"Authorization": expected}


def test_env_prefixed_resolution(monkeypatch):
    monkeypatch.setenv("SNOW_UAT_BASE_URL", "https://uat.service-now.com")
    monkeypatch.setenv("SNOW_UAT_TOKEN", "uat-tok")
    monkeypatch.setenv("SNOW_UAT_TABLE", "sc_task")
    cfg = config_from_env(env="uat")
    assert cfg.base_url == "https://uat.service-now.com"
    assert cfg.token == "uat-tok"
    assert cfg.default_table == "sc_task"
    assert cfg.env == "uat"


def test_env_prefixed_falls_back_to_unprefixed(monkeypatch):
    monkeypatch.setenv("SNOW_PROD_BASE_URL", "https://prod.service-now.com")
    monkeypatch.setenv("SNOW_TOKEN", "shared-tok")
    cfg = config_from_env(env="prod")
    assert cfg.base_url == "https://prod.service-now.com"
    assert cfg.token == "shared-tok"


def test_env_from_snow_env_var(monkeypatch):
    monkeypatch.setenv("SNOW_ENV", "dev")
    monkeypatch.setenv("SNOW_DEV_BASE_URL", "https://dev.service-now.com")
    monkeypatch.setenv("SNOW_DEV_TOKEN", "dev-tok")
    cfg = config_from_env()
    assert cfg.env == "dev"
    assert cfg.base_url == "https://dev.service-now.com"


def test_invalid_env_raises():
    with pytest.raises(SnowError, match="Invalid env"):
        config_from_env(env="staging")


def test_missing_base_url_raises(monkeypatch):
    monkeypatch.setenv("SNOW_TOKEN", "tok")
    with pytest.raises(SnowError, match="No instance URL"):
        config_from_env()


def test_missing_credentials_raises(monkeypatch):
    monkeypatch.setenv("SNOW_BASE_URL", "https://acme.service-now.com")
    with pytest.raises(SnowError, match="No credentials"):
        config_from_env()


def test_resolve_table_falls_back_to_default():
    cfg = SnowConfig(base_url="https://x", token="t", default_table="incident")
    assert resolve_table(cfg, None) == "incident"
    assert resolve_table(cfg, "sc_task") == "sc_task"


def test_resolve_table_raises_when_missing():
    cfg = SnowConfig(base_url="https://x", token="t")
    with pytest.raises(SnowError, match="No table"):
        resolve_table(cfg, None)


def _mock_request(monkeypatch, handler):
    """Route runspec_snow.client's httpx.request through a MockTransport."""
    transport = httpx.MockTransport(handler)

    def fake_request(method, url, **kwargs):
        with httpx.Client(transport=transport) as c:
            return c.request(method, url, **kwargs)

    monkeypatch.setattr(client_mod.httpx, "request", fake_request)


def test_snow_request_builds_url_auth_and_body(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = request.content
        return httpx.Response(201, json={"result": {"sys_id": "abc"}})

    _mock_request(monkeypatch, handler)
    cfg = SnowConfig(base_url="https://acme.service-now.com", token="tok")
    result = snow_request("POST", "/api/now/table/incident", config=cfg, json_body={"short_description": "hi"})

    assert result == {"result": {"sys_id": "abc"}}
    assert seen["url"] == "https://acme.service-now.com/api/now/table/incident"
    assert seen["method"] == "POST"
    assert seen["auth"] == "Bearer tok"
    assert b'"short_description"' in seen["body"]


def test_snow_request_returns_none_on_204(monkeypatch):
    _mock_request(monkeypatch, lambda req: httpx.Response(204))
    cfg = SnowConfig(base_url="https://acme.service-now.com", token="tok")
    assert snow_request("DELETE", "/api/now/table/incident/abc", config=cfg) is None


def test_snow_request_raises_with_server_message(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Invalid table", "detail": "no such table foo"}, "status": "failure"})

    _mock_request(monkeypatch, handler)
    cfg = SnowConfig(base_url="https://acme.service-now.com", token="tok")
    with pytest.raises(SnowError, match="Invalid table; no such table foo"):
        snow_request("GET", "/api/now/table/foo", config=cfg)
