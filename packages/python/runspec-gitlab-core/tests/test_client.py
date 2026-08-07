import httpx
import pytest

from runspec_gitlab_core.client import (
    DEFAULT_URL,
    GitlabConfig,
    GitlabError,
    config_from_env,
    encode_project,
    gitlab_request,
)
from tests.conftest import mock_request

# ── config_from_env ─────────────────────────────────────────────────────────


def test_config_from_env_defaults_url(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "tok123")
    cfg = config_from_env()
    assert cfg.base_url == DEFAULT_URL
    assert cfg.token == "tok123"
    assert cfg.env is None


def test_config_from_env_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com/")
    assert config_from_env().base_url == "https://gitlab.example.com"


def test_env_prefixed_resolution(monkeypatch):
    monkeypatch.setenv("GITLAB_UAT_URL", "https://uat.example.com")
    monkeypatch.setenv("GITLAB_UAT_TOKEN", "uat-tok")
    monkeypatch.setenv("GITLAB_UAT_PROJECT", "grp/app")
    cfg = config_from_env(env="uat")
    assert cfg.base_url == "https://uat.example.com"
    assert cfg.token == "uat-tok"
    assert cfg.project == "grp/app"
    assert cfg.env == "uat"


def test_env_prefixed_falls_back_to_unprefixed(monkeypatch):
    monkeypatch.setenv("GITLAB_PROD_URL", "https://prod.example.com")
    monkeypatch.setenv("GITLAB_TOKEN", "shared-tok")
    cfg = config_from_env(env="prod")
    assert cfg.base_url == "https://prod.example.com"
    assert cfg.token == "shared-tok"


def test_env_from_gitlab_env_var(monkeypatch):
    monkeypatch.setenv("GITLAB_ENV", "dev")
    monkeypatch.setenv("GITLAB_DEV_TOKEN", "dev-tok")
    cfg = config_from_env()
    assert cfg.env == "dev"
    assert cfg.token == "dev-tok"


def test_invalid_env_raises(monkeypatch):
    with pytest.raises(GitlabError, match="Invalid env"):
        config_from_env(env="staging")


def test_missing_token_raises(monkeypatch):
    with pytest.raises(GitlabError, match="No token"):
        config_from_env()


# ── encode_project ──────────────────────────────────────────────────────────


def test_encode_project_path():
    assert encode_project("group/sub/app") == "group%2Fsub%2Fapp"


def test_encode_project_numeric():
    assert encode_project("42") == "42"


# ── gitlab_request ──────────────────────────────────────────────────────────


def test_gitlab_request_builds_url_auth_and_params(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["token"] = request.headers.get("PRIVATE-TOKEN")
        return httpx.Response(200, json=[{"id": 1}])

    mock_request(monkeypatch, handler)
    cfg = GitlabConfig(base_url="https://gitlab.example.com", token="tok")
    result = gitlab_request("GET", "/projects/7/issues", config=cfg, params={"state": "opened"})

    assert result == [{"id": 1}]
    assert seen["url"] == "https://gitlab.example.com/api/v4/projects/7/issues?state=opened"
    assert seen["method"] == "GET"
    assert seen["token"] == "tok"


def test_gitlab_request_drops_none_and_renders_bools(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    mock_request(monkeypatch, handler)
    cfg = GitlabConfig(base_url="https://gl", token="tok")
    gitlab_request("GET", "/x", config=cfg, params={"recursive": True, "membership": False, "ref": None})

    assert seen["query"] == {"recursive": "true", "membership": "false"}


def test_gitlab_request_raw_returns_text(monkeypatch):
    mock_request(monkeypatch, lambda req: httpx.Response(200, text="line1\nline2\n"))
    cfg = GitlabConfig(base_url="https://gl", token="tok")
    assert gitlab_request("GET", "/jobs/1/trace", config=cfg, raw=True) == "line1\nline2\n"


def test_gitlab_request_none_on_204(monkeypatch):
    mock_request(monkeypatch, lambda req: httpx.Response(204))
    cfg = GitlabConfig(base_url="https://gl", token="tok")
    assert gitlab_request("GET", "/x", config=cfg) is None


def test_gitlab_request_raises_string_message(monkeypatch):
    mock_request(monkeypatch, lambda req: httpx.Response(404, json={"message": "404 Project Not Found"}))
    cfg = GitlabConfig(base_url="https://gl", token="tok")
    with pytest.raises(GitlabError, match="Project Not Found"):
        gitlab_request("GET", "/projects/nope", config=cfg)


def test_gitlab_request_raises_dict_message(monkeypatch):
    mock_request(monkeypatch, lambda req: httpx.Response(400, json={"message": {"labels": ["is invalid"]}}))
    cfg = GitlabConfig(base_url="https://gl", token="tok")
    with pytest.raises(GitlabError, match="labels: is invalid"):
        gitlab_request("GET", "/x", config=cfg)


def test_gitlab_request_raises_oauth_error(monkeypatch):
    mock_request(monkeypatch, lambda req: httpx.Response(401, json={"error": "invalid_token", "error_description": "token expired"}))
    cfg = GitlabConfig(base_url="https://gl", token="tok")
    with pytest.raises(GitlabError, match="token expired"):
        gitlab_request("GET", "/user", config=cfg)
