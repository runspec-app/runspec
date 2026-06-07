import base64

import httpx
import pytest

from runspec_jsm import client as client_mod
from runspec_jsm.client import (
    JsmConfig,
    JsmError,
    _auth_header,
    config_from_env,
    jsm_request,
)

# Every JSM_* var any test might set — cleared up front so the host environment
# never leaks in.
_ALL_VARS = [
    "JSM_ENV",
    "JSM_BASE_URL",
    "JSM_PAT",
    "JSM_USER",
    "JSM_PASSWORD",
    "JSM_SERVICE_DESK_ID",
    "JSM_PROJECT_KEY",
]
for _env in ("DEV", "UAT", "PROD"):
    _ALL_VARS += [f"JSM_{_env}_{suffix}" for suffix in ("BASE_URL", "PAT", "USER", "PASSWORD", "SERVICE_DESK_ID", "PROJECT_KEY")]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)


def test_config_from_env_pat_unprefixed(monkeypatch):
    monkeypatch.setenv("JSM_BASE_URL", "https://jira.example.com/")
    monkeypatch.setenv("JSM_PAT", "tok123")
    cfg = config_from_env()
    assert cfg.base_url == "https://jira.example.com"  # trailing slash stripped
    assert cfg.pat == "tok123"
    assert cfg.env is None


def test_pat_wins_over_basic(monkeypatch):
    monkeypatch.setenv("JSM_BASE_URL", "https://jira.example.com")
    monkeypatch.setenv("JSM_PAT", "tok")
    monkeypatch.setenv("JSM_USER", "alice")
    monkeypatch.setenv("JSM_PASSWORD", "pw")
    cfg = config_from_env()
    assert _auth_header(cfg) == {"Authorization": "Bearer tok"}


def test_basic_auth_when_no_pat(monkeypatch):
    monkeypatch.setenv("JSM_BASE_URL", "https://jira.example.com")
    monkeypatch.setenv("JSM_USER", "alice")
    monkeypatch.setenv("JSM_PASSWORD", "s3cret")
    cfg = config_from_env()
    expected = "Basic " + base64.b64encode(b"alice:s3cret").decode()
    assert _auth_header(cfg) == {"Authorization": expected}


def test_env_prefixed_resolution(monkeypatch):
    monkeypatch.setenv("JSM_UAT_BASE_URL", "https://uat.example.com")
    monkeypatch.setenv("JSM_UAT_PAT", "uat-tok")
    monkeypatch.setenv("JSM_UAT_SERVICE_DESK_ID", "17")
    cfg = config_from_env(env="uat")
    assert cfg.base_url == "https://uat.example.com"
    assert cfg.pat == "uat-tok"
    assert cfg.service_desk_id == "17"
    assert cfg.env == "uat"


def test_env_prefixed_falls_back_to_unprefixed(monkeypatch):
    # Prefixed base URL present, but PAT only set unprefixed → falls back.
    monkeypatch.setenv("JSM_PROD_BASE_URL", "https://prod.example.com")
    monkeypatch.setenv("JSM_PAT", "shared-tok")
    cfg = config_from_env(env="prod")
    assert cfg.base_url == "https://prod.example.com"
    assert cfg.pat == "shared-tok"


def test_env_from_jsm_env_var(monkeypatch):
    monkeypatch.setenv("JSM_ENV", "dev")
    monkeypatch.setenv("JSM_DEV_BASE_URL", "https://dev.example.com")
    monkeypatch.setenv("JSM_DEV_PAT", "dev-tok")
    cfg = config_from_env()
    assert cfg.env == "dev"
    assert cfg.base_url == "https://dev.example.com"


def test_invalid_env_raises(monkeypatch):
    with pytest.raises(JsmError, match="Invalid env"):
        config_from_env(env="staging")


def test_missing_base_url_raises(monkeypatch):
    monkeypatch.setenv("JSM_PAT", "tok")
    with pytest.raises(JsmError, match="No base URL"):
        config_from_env()


def test_missing_credentials_raises(monkeypatch):
    monkeypatch.setenv("JSM_BASE_URL", "https://jira.example.com")
    with pytest.raises(JsmError, match="No credentials"):
        config_from_env()


def _mock_request(monkeypatch, handler):
    """Route runspec_jsm.client's httpx.request through a MockTransport."""
    transport = httpx.MockTransport(handler)

    def fake_request(method, url, **kwargs):
        with httpx.Client(transport=transport) as c:
            return c.request(method, url, **kwargs)

    monkeypatch.setattr(client_mod.httpx, "request", fake_request)


def test_jsm_request_builds_url_auth_and_body(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    _mock_request(monkeypatch, handler)
    cfg = JsmConfig(base_url="https://jira.example.com", pat="tok")
    result = jsm_request("POST", "/rest/api/2/issue/ABC-1/comment", config=cfg, json_body={"body": "hi"})

    assert result == {"ok": True}
    assert seen["url"] == "https://jira.example.com/rest/api/2/issue/ABC-1/comment"
    assert seen["method"] == "POST"
    assert seen["auth"] == "Bearer tok"
    assert b'"body"' in seen["body"]


def test_jsm_request_returns_none_on_204(monkeypatch):
    _mock_request(monkeypatch, lambda req: httpx.Response(204))
    cfg = JsmConfig(base_url="https://jira.example.com", pat="tok")
    assert jsm_request("POST", "/rest/api/2/issue/ABC-1/transitions", config=cfg, json_body={"x": 1}) is None


def test_jsm_request_raises_with_server_message(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"errorMessages": ["Field 'summary' is required"], "errors": {}})

    _mock_request(monkeypatch, handler)
    cfg = JsmConfig(base_url="https://jira.example.com", pat="tok")
    with pytest.raises(JsmError, match="summary"):
        jsm_request("POST", "/rest/api/2/issue", config=cfg, json_body={})


def test_servicedeskapi_error_message(monkeypatch):
    _mock_request(monkeypatch, lambda req: httpx.Response(403, json={"errorMessage": "Permission denied"}))
    cfg = JsmConfig(base_url="https://jira.example.com", pat="tok")
    with pytest.raises(JsmError, match="Permission denied"):
        jsm_request("GET", "/rest/servicedeskapi/request/ABC-1", config=cfg)
