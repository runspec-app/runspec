"""0.5.0 additions: proxy/verify wiring, spaces, users, search helpers, page-children."""

from runspec_confluence_core import (
    ConfluenceConfig,
    build_client,
    cql_from_text,
    get_current_user,
    get_page_children,
    get_space,
    get_space_pages,
    list_spaces,
    search,
    search_by_label,
)

CLOUD = ConfluenceConfig(url="https://acme.atlassian.net/wiki", cloud=True, personal_token="p")


def test_build_client_forwards_proxies_and_verify(install):
    cfg = ConfluenceConfig(
        url="https://wiki.corp",
        cloud=False,
        personal_token="pat",
        verify_ssl="/etc/ssl/corp-ca.pem",
        proxies={"http": "http://proxy:8080", "https": "http://proxy:8080"},
    )
    _, made = install()
    build_client(cfg)
    assert made["kw"]["verify_ssl"] == "/etc/ssl/corp-ca.pem"
    assert made["kw"]["proxies"] == {"http": "http://proxy:8080", "https": "http://proxy:8080"}


def test_build_client_omits_proxies_when_unset(install):
    _, made = install()
    build_client(CLOUD)
    assert "proxies" not in made["kw"]
    assert made["kw"]["verify_ssl"] is True


def test_list_spaces(install):
    client, _ = install(returns={"get_all_spaces": {"results": []}})
    list_spaces(CLOUD, space_type="global", start=5, limit=10)
    _, kwargs = client.last("get_all_spaces")
    assert kwargs == {"space_type": "global", "start": 5, "limit": 10}


def test_list_spaces_caps_limit(install):
    client, _ = install(returns={"get_all_spaces": {}})
    list_spaces(CLOUD, limit=500)
    _, kwargs = client.last("get_all_spaces")
    assert kwargs["limit"] == 100


def test_get_space_and_pages_still_importable(install):
    client, _ = install(returns={"get_space": {"key": "DOCS"}, "get_all_pages_from_space": {"results": []}})
    assert get_space(CLOUD, "DOCS") == {"key": "DOCS"}
    get_space_pages(CLOUD, "DOCS", start=0, limit=10)
    args, _ = client.last("get_all_pages_from_space")
    assert args == ("DOCS",)


def test_get_current_user(install):
    client, _ = install(returns={"get_current_user": {"displayName": "Me"}})
    assert get_current_user(CLOUD) == {"displayName": "Me"}
    client.last("get_current_user")


def test_get_page_children_typed(install):
    client, _ = install(returns={"get_page_child_by_type": {"results": []}})
    get_page_children(CLOUD, "9", child_type="comment", start=2, limit=5)
    args, kwargs = client.last("get_page_child_by_type")
    assert args == ("9",)
    assert kwargs == {"type": "comment", "start": 2, "limit": 5}


def test_cql_from_text_wraps_plain_text():
    assert cql_from_text("onboarding docs") == 'siteSearch ~ "onboarding docs"'


def test_cql_from_text_passes_cql_through():
    assert cql_from_text('type = page AND text ~ "x"') == 'type = page AND text ~ "x"'


def test_cql_from_text_adds_space_and_type():
    out = cql_from_text("onboarding", space_key="DOCS", content_type="page")
    assert out == 'siteSearch ~ "onboarding" AND type = "page" AND space = "DOCS"'


def test_cql_from_text_escapes_quotes():
    assert cql_from_text('say "hi"') == 'siteSearch ~ "say \\"hi\\""'


def test_search_by_label(install):
    client, _ = install(returns={"cql": {"results": []}})
    search_by_label(CLOUD, "runbook", space_key="DOCS", limit=10)
    args, kwargs = client.last("cql")
    assert args == ('label = "runbook" AND space = "DOCS"',)
    assert kwargs["limit"] == 10


def test_search_caps_limit(install):
    client, _ = install(returns={"cql": {}})
    search(CLOUD, "type = page", limit=999)
    _, kwargs = client.last("cql")
    assert kwargs["limit"] == 100
