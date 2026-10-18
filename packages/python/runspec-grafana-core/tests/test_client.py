"""Config-from-env, request/error handling, the datasource proxy fallback, and time parsing."""

import httpx
import pytest

from runspec_grafana_core import GrafanaConfig, GrafanaError, config_from_env, datasource_request, grafana_request, parse_duration, parse_time
from runspec_grafana_core.client import preview, window
from tests.conftest import route

CFG = GrafanaConfig(url="https://graf")


def test_config_defaults_to_localhost() -> None:
    cfg = config_from_env()
    assert cfg.url == "http://127.0.0.1:3000" and cfg.token is None and cfg.org_id is None


def test_config_env_prefix_and_fallback(monkeypatch) -> None:
    monkeypatch.setenv("GRAFANA_URL", "https://base/")
    monkeypatch.setenv("GRAFANA_PROD_EU_URL", "https://eu")
    monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "glsa_x")
    monkeypatch.setenv("GRAFANA_ORG_ID", "3")
    cfg = config_from_env("prod-eu")
    assert cfg.url == "https://eu" and cfg.token == "glsa_x" and cfg.org_id == "3"
    assert config_from_env().url == "https://base"


@pytest.mark.parametrize("var", ["GRAFANA_TOKEN", "GRAFANA_PAT", "GRAFANA_API_KEY"])
def test_config_token_aliases(monkeypatch, var) -> None:
    monkeypatch.setenv(var, "t")
    assert config_from_env().token == "t"


def test_config_explicit_args_win(monkeypatch) -> None:
    monkeypatch.setenv("GRAFANA_URL", "https://env")
    monkeypatch.setenv("GRAFANA_TOKEN", "env-token")
    cfg = config_from_env(url="https://arg", token="arg-token", org_id=7)
    assert cfg.url == "https://arg" and cfg.token == "arg-token" and cfg.org_id == "7"


def test_config_half_basic_auth_refused(monkeypatch) -> None:
    monkeypatch.setenv("GRAFANA_USERNAME", "admin")
    with pytest.raises(GrafanaError, match="both"):
        config_from_env()


def test_config_tls_verify_off(monkeypatch) -> None:
    monkeypatch.setenv("GRAFANA_TLS_VERIFY", "false")
    assert config_from_env().tls_verify is False


def test_request_sends_auth_and_org(monkeypatch) -> None:
    seen = route(monkeypatch, {"/api/health": {"database": "ok"}})
    grafana_request("GET", "/api/health", config=GrafanaConfig(url="https://graf", token="tok", org_id="2"))
    assert seen[0].headers["Authorization"] == "Bearer tok" and seen[0].headers["X-Grafana-Org-Id"] == "2"


def test_request_basic_auth(monkeypatch) -> None:
    seen = route(monkeypatch, {"/api/health": {}})
    grafana_request("GET", "/api/health", config=GrafanaConfig(url="https://graf", username="u", password="p"))
    assert seen[0].headers["Authorization"].startswith("Basic ")


def test_request_error_carries_message_and_hint(monkeypatch) -> None:
    route(monkeypatch, {"/api/user": httpx.Response(401, json={"message": "invalid API key"})})
    with pytest.raises(GrafanaError, match="invalid API key.*GRAFANA_SERVICE_ACCOUNT_TOKEN") as info:
        grafana_request("GET", "/api/user", config=CFG)
    assert info.value.status_code == 401


def test_datasource_request_unwraps_envelope(monkeypatch) -> None:
    route(monkeypatch, {"/api/datasources/proxy/uid/p/api/v1/labels": {"status": "success", "data": ["job"], "warnings": ["w"]}})
    assert datasource_request(CFG, "p", "/api/v1/labels") == {"data": ["job"], "warnings": ["w"]}


def test_datasource_request_falls_back_to_resources(monkeypatch) -> None:
    seen = route(
        monkeypatch,
        {
            "/api/datasources/proxy/uid/p/api/v1/labels": httpx.Response(403, json={"message": "denied"}),
            "/api/datasources/uid/p/resources/api/v1/labels": {"status": "success", "data": ["job"]},
        },
    )
    assert datasource_request(CFG, "p", "/api/v1/labels")["data"] == ["job"]
    assert [r.url.path for r in seen] == ["/api/datasources/proxy/uid/p/api/v1/labels", "/api/datasources/uid/p/resources/api/v1/labels"]


def test_datasource_request_reports_the_proxy_error_when_both_fail(monkeypatch) -> None:
    route(monkeypatch, {"/api/datasources/proxy/uid/p/x": httpx.Response(500, json={"message": "upstream down"})})
    with pytest.raises(GrafanaError, match="upstream down"):
        datasource_request(CFG, "p", "/x")


def test_datasource_request_raises_on_error_status(monkeypatch) -> None:
    route(monkeypatch, {"/api/datasources/proxy/uid/p/api/v1/query": {"status": "error", "error": "parse error"}})
    with pytest.raises(GrafanaError, match="parse error"):
        datasource_request(CFG, "p", "/api/v1/query")


def test_parse_time_forms() -> None:
    now = 1_000_000.0
    assert parse_time("now", now=now) == now
    assert parse_time("1h", now=now) == now - 3600
    assert parse_time("now-30m", now=now) == now - 1800
    assert parse_time("1733000000", now=now) == 1733000000.0
    assert parse_time("1733000000000", now=now) == 1733000000.0  # epoch ms
    assert parse_time("1970-01-01T00:01:00Z", now=now) == 60.0
    assert parse_time("", now=now) is None
    with pytest.raises(GrafanaError, match="Cannot parse"):
        parse_time("yesterday")


def test_parse_duration() -> None:
    assert parse_duration("1h30m") == 5400
    with pytest.raises(GrafanaError):
        parse_duration("soon")


def test_window_defaults_and_order() -> None:
    assert window(None, None, now=10_000.0) == (6400.0, 10_000.0)
    with pytest.raises(GrafanaError, match="before"):
        window("100", "50")


def test_preview() -> None:
    assert preview("short", 10) == ("short", None)
    text, full = preview("x" * 20, 5)
    assert text == "xxxxx…" and full == 20


def test_config_from_env_explicit_basic_auth_wins(monkeypatch) -> None:
    monkeypatch.setenv("GRAFANA_USERNAME", "env-user")
    config = config_from_env(username="arg-user", password="arg-pw")
    assert (config.username, config.password) == ("arg-user", "arg-pw")
