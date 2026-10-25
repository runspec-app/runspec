"""Guards against the SDK shape changes atlassian-python-api 5.x brought: a
Cloud client without the v1 methods, lazy generators, and HTML pages where
JSON was expected (seen live on a Cloud site, 2026-10-25)."""

import pytest
import requests

from runspec_confluence_core import ConfluenceConfig, ConfluenceError, build_client, get_space, list_spaces, search
from runspec_confluence_core import client as client_mod

CLOUD = ConfluenceConfig(url="https://acme.atlassian.net/wiki", cloud=True, username="me@x.com", api_token="tok")

_HTML = "<!DOCTYPE html><html><head><title>Confluence</title><script>" + "x" * 50_000 + "</script></head></html>"


class _HtmlResp:
    status_code = 404
    url = "https://acme.atlassian.net/wiki/content/search"
    text = _HTML

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


def test_client_without_v1_methods_is_refused(monkeypatch):
    class V2OnlyClient:
        def get(self, *a, **kw):
            return {}

    monkeypatch.setattr(client_mod, "Confluence", lambda **kw: V2OnlyClient())
    with pytest.raises(ConfluenceError, match="atlassian-python-api<5"):
        build_client(CLOUD)


def test_installed_sdk_has_the_v1_cloud_client():
    # The real SDK, not a fake: every op calls v1 methods like get_page_by_id.
    client = client_mod.Confluence(url=CLOUD.url, cloud=True, username="u", password="p")
    for name in ("get_page_by_id", "create_page", "update_page", "add_comment", "cql", "get_all_spaces"):
        assert hasattr(client, name), name


def test_list_spaces_reads_a_generator_into_one_page(install):
    install(returns={"get_all_spaces": lambda: ({"key": f"S{i}"} for i in range(500))})
    result = list_spaces(CLOUD, limit=3)
    assert result == {"results": [{"key": "S0"}, {"key": "S1"}, {"key": "S2"}]}


def test_html_body_on_success_is_an_error_not_the_page(install):
    install(returns={"get_space": _HTML})
    with pytest.raises(ConfluenceError) as exc:
        get_space(CLOUD, "~me")
    message = str(exc.value)
    assert "HTML page" in message and "Confluence" in message
    assert len(message) < 400


def test_html_error_body_is_one_line(install):
    err = requests.HTTPError("404")
    err.response = _HtmlResp()
    install(returns={"cql": err})
    with pytest.raises(ConfluenceError) as exc:
        search(CLOUD, "type=page")
    message = str(exc.value)
    assert message.startswith("Confluence 404: got an HTML page from https://acme.atlassian.net/wiki/content/search")
    assert "title: 'Confluence'" in message
    assert "<script" not in message and len(message) < 400
