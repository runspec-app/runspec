"""Config resolution, auth headers, error mapping, and time parsing."""

import httpx
import pytest

from runspec_prometheus_core import PrometheusConfig, PrometheusError, auto_step, build_info, config_from_env, parse_duration, parse_time
from runspec_prometheus_core.client import iso, truncate
from tests.conftest import capture


def test_defaults_to_local_prometheus_without_credentials() -> None:
    cfg = config_from_env()
    assert cfg.url == "http://127.0.0.1:9090"
    assert cfg.token is None and cfg.username is None


def test_env_prefix_wins_then_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("PROMETHEUS_URL", "http://base:9090/")
    monkeypatch.setenv("PROMETHEUS_PROD_EU_URL", "https://prom.eu")
    monkeypatch.setenv("PROMETHEUS_TOKEN", "t0")
    cfg = config_from_env("prod-eu")
    assert cfg.url == "https://prom.eu"
    assert cfg.token == "t0"
    assert config_from_env().url == "http://base:9090"


def test_pat_and_user_aliases(monkeypatch) -> None:
    monkeypatch.setenv("PROMETHEUS_PAT", "pat")
    assert config_from_env().token == "pat"
    monkeypatch.delenv("PROMETHEUS_PAT")
    monkeypatch.setenv("PROMETHEUS_USER", "u")
    monkeypatch.setenv("PROMETHEUS_PASSWORD", "p")
    cfg = config_from_env()
    assert (cfg.username, cfg.password) == ("u", "p")


def test_half_basic_auth_is_an_error(monkeypatch) -> None:
    monkeypatch.setenv("PROMETHEUS_USERNAME", "u")
    with pytest.raises(PrometheusError, match="both"):
        config_from_env()


def test_explicit_overrides_and_tls_flag(monkeypatch) -> None:
    monkeypatch.setenv("PROMETHEUS_TLS_VERIFY", "false")
    cfg = config_from_env(url="https://x/", token="tok")
    assert cfg.url == "https://x" and cfg.token == "tok" and cfg.tls_verify is False


def test_bearer_and_tenant_headers(monkeypatch) -> None:
    seen = capture(monkeypatch, httpx.Response(200, json={"status": "success", "data": {"version": "3.0"}}))
    assert build_info(PrometheusConfig(url="https://p", token="tok", org_id="team-a")) == {"version": "3.0"}
    assert seen["headers"]["authorization"] == "Bearer tok"
    assert seen["headers"]["x-scope-orgid"] == "team-a"


def test_basic_auth_header(monkeypatch) -> None:
    seen = capture(monkeypatch, httpx.Response(200, json={"status": "success", "data": {}}))
    build_info(PrometheusConfig(url="https://p", username="u", password="p"))
    assert seen["headers"]["authorization"].startswith("Basic ")


def test_error_envelope_carries_prometheus_message(monkeypatch) -> None:
    capture(monkeypatch, httpx.Response(400, json={"status": "error", "errorType": "bad_data", "error": "parse error at char 3"}))
    with pytest.raises(PrometheusError, match="parse error") as info:
        build_info(PrometheusConfig())
    assert info.value.status_code == 400 and info.value.error_type == "bad_data"


def test_unauthorised_hint(monkeypatch) -> None:
    capture(monkeypatch, httpx.Response(401, text="Unauthorized"))
    with pytest.raises(PrometheusError, match="PROMETHEUS_TOKEN"):
        build_info(PrometheusConfig())


def test_parse_time_forms() -> None:
    now = 1_000_000.0
    assert parse_time(None) is None and parse_time("") is None
    assert parse_time("1733000000") == 1733000000.0
    assert parse_time("now", now=now) == now
    assert parse_time("5m", now=now) == now - 300
    assert parse_time("-1h30m", now=now) == now - 5400
    assert parse_time("1970-01-02T00:00:00Z") == 86400.0
    with pytest.raises(PrometheusError, match="Cannot parse"):
        parse_time("yesterday-ish")


def test_parse_duration_and_auto_step() -> None:
    assert parse_duration("2d") == 172800
    assert parse_duration("500ms") == 0.5
    with pytest.raises(PrometheusError):
        parse_duration("5 minutes")
    assert auto_step(0, 3600) == 14
    assert auto_step(0, 10) == 1


def test_truncate_and_iso() -> None:
    assert truncate([1, 2, 3], 2) == ([1, 2], True)
    assert truncate([1, 2, 3], 0) == ([1, 2, 3], False)
    assert iso(0) == "1970-01-01T00:00:00Z"


def test_config_from_env_explicit_basic_auth_wins(monkeypatch) -> None:
    monkeypatch.setenv("PROMETHEUS_USERNAME", "env-user")
    config = config_from_env(username="arg-user", password="arg-pw")
    assert (config.username, config.password) == ("arg-user", "arg-pw")
