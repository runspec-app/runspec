import httpx
import pytest

from runspec_snow import client as client_mod
from runspec_snow.client import SnowConfig, SnowError
from runspec_snow.records import _is_sys_id, get_record, resolve_sys_id


def _mock_request(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def fake_request(method, url, **kwargs):
        with httpx.Client(transport=transport) as c:
            return c.request(method, url, **kwargs)

    monkeypatch.setattr(client_mod.httpx, "request", fake_request)


def test_is_sys_id():
    assert _is_sys_id("a" * 32)
    assert _is_sys_id("0123456789abcdef0123456789ABCDEF")
    assert not _is_sys_id("INC0010001")
    assert not _is_sys_id("a" * 31)
    assert not _is_sys_id("z" * 32)  # 'z' is not hex


def test_get_record_by_sys_id_uses_direct_path(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"result": {"sys_id": "a" * 32, "number": "INC1"}})

    _mock_request(monkeypatch, handler)
    cfg = SnowConfig(base_url="https://acme.service-now.com", token="t")
    record = get_record(cfg, "incident", "a" * 32)
    assert record["number"] == "INC1"
    assert seen["url"] == f"https://acme.service-now.com/api/now/table/incident/{'a' * 32}"


def test_get_record_by_number_queries(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"result": [{"sys_id": "b" * 32, "number": "INC0010001"}]})

    _mock_request(monkeypatch, handler)
    cfg = SnowConfig(base_url="https://acme.service-now.com", token="t")
    record = get_record(cfg, "incident", "INC0010001")
    assert record["sys_id"] == "b" * 32
    assert "sysparm_query=number%3DINC0010001" in seen["url"] or "number=INC0010001" in seen["url"]


def test_get_record_by_number_not_found_raises(monkeypatch):
    _mock_request(monkeypatch, lambda req: httpx.Response(200, json={"result": []}))
    cfg = SnowConfig(base_url="https://acme.service-now.com", token="t")
    with pytest.raises(SnowError, match="No incident record with number"):
        get_record(cfg, "incident", "INC9999999")


def test_resolve_sys_id_passthrough_for_sys_id(monkeypatch):
    # A sys_id should resolve without any HTTP call.
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("no request expected for a sys_id")

    _mock_request(monkeypatch, handler)
    cfg = SnowConfig(base_url="https://acme.service-now.com", token="t")
    assert resolve_sys_id(cfg, "incident", "c" * 32) == "c" * 32
