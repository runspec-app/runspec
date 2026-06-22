"""The webops runnables parse from runspec.toml and the wrappers emit JSON + exit codes."""

import json
from pathlib import Path

import pytest
import runspec as rs

from runspec_windows import webops

TOML = Path(__file__).resolve().parent.parent / "runspec_windows" / "runspec.toml"

# Each new runnable plus a minimal valid argv (its required positional).
WEBOPS_RUNNABLES = {
    "cert-check": ["example.com"],
    "cert-chain": ["example.com"],
    "tls-info": ["example.com"],
    "http-check": ["https://example.com"],
    "http-headers": ["https://example.com"],
    "redirect-trace": ["https://example.com"],
    "dns-lookup": ["example.com"],
    "api-spec": ["/tmp/spec.json"],
}


@pytest.mark.parametrize("name", list(WEBOPS_RUNNABLES))
def test_runnable_is_defined(name: str) -> None:
    spec = rs.parse(name, WEBOPS_RUNNABLES[name], config_path=TOML)
    assert spec.runspec_runnable == name


def test_cert_check_defaults_and_positional() -> None:
    spec = rs.parse("cert-check", ["example.com"], config_path=TOML)
    assert spec.host.value == "example.com"
    assert spec.port.value == 443
    assert spec.warn_days.value == 30
    assert spec.timeout.value == 10


def test_http_check_flags() -> None:
    spec = rs.parse("http-check", ["https://x.test", "--expect-status", "200", "--insecure"], config_path=TOML)
    assert spec.url.value == "https://x.test"
    assert spec.expect_status.value == 200
    assert spec.insecure.value is True


def _fixed_parse(name: str, argv: list[str]):
    """A real parse bound to a runnable + argv, for patching ``webops.rs.parse``."""
    real = rs.parse
    return lambda *_a, **_k: real(name, argv, config_path=TOML)


def test_main_cert_check_problem_exits_1(monkeypatch, capsys) -> None:
    monkeypatch.setattr(webops.rs, "parse", _fixed_parse("cert-check", ["expired.test"]))
    monkeypatch.setattr(webops, "connect_peer", lambda *a, **k: object())
    monkeypatch.setattr(webops, "leaf_report", lambda *a, **k: {"status": "expired", "days_remaining": -3})
    with pytest.raises(SystemExit) as exc:
        webops.main_cert_check()
    assert exc.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["host"] == "expired.test"
    assert out["status"] == "expired"


def test_main_cert_check_unreachable_exits_2(monkeypatch, capsys) -> None:
    monkeypatch.setattr(webops.rs, "parse", _fixed_parse("cert-check", ["down.test"]))

    def boom(*_a, **_k):
        raise webops.ConnectError("refused")

    monkeypatch.setattr(webops, "connect_peer", boom)
    with pytest.raises(SystemExit) as exc:
        webops.main_cert_check()
    assert exc.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert "error" in out


def test_main_api_spec_overview(monkeypatch, capsys, tmp_path) -> None:
    spec_file = tmp_path / "openapi.json"
    spec_file.write_text(json.dumps({"openapi": "3.0.0", "info": {"title": "T", "version": "1"}, "paths": {"/x": {"get": {"operationId": "getX", "responses": {"200": {"description": "ok"}}}}}}))
    monkeypatch.setattr(webops.rs, "parse", _fixed_parse("api-spec", [str(spec_file)]))
    webops.main_api_spec()
    out = json.loads(capsys.readouterr().out)
    assert out["title"] == "T"
    assert out["operations"][0]["operation_id"] == "getX"


def test_main_api_spec_bad_source_exits_2(monkeypatch, capsys) -> None:
    monkeypatch.setattr(webops.rs, "parse", _fixed_parse("api-spec", ["/no/such/spec.json"]))
    with pytest.raises(SystemExit) as exc:
        webops.main_api_spec()
    assert exc.value.code == 2
    assert "error" in json.loads(capsys.readouterr().out)


def test_main_dns_lookup_resolve_error_exits_1(monkeypatch, capsys) -> None:
    monkeypatch.setattr(webops.rs, "parse", _fixed_parse("dns-lookup", ["bad.invalid"]))

    def boom(*_a, **_k):
        raise webops.ResolveError("nxdomain")

    monkeypatch.setattr(webops, "resolve", boom)
    with pytest.raises(SystemExit) as exc:
        webops.main_dns_lookup()
    assert exc.value.code == 1
    assert "error" in json.loads(capsys.readouterr().out)
