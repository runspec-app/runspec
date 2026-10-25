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
    assert kwargs == {"space_type": "global", "start": 5, "limit": 10, "expand": None}


def test_list_spaces_caps_limit(install):
    client, _ = install(returns={"get_all_spaces": {}})
    list_spaces(CLOUD, limit=500)
    _, kwargs = client.last("get_all_spaces")
    assert kwargs["limit"] == 100


def test_get_space_expand(install):
    client, _ = install(returns={"get_space": {"key": "DOCS"}})
    assert get_space(CLOUD, "DOCS", expand="description.plain") == {"key": "DOCS"}
    args, kwargs = client.last("get_space")
    assert args == ("DOCS",)
    assert kwargs == {"expand": "description.plain"}


def test_get_space_no_expand_preserves_sdk_default(install):
    # Omitting expand must NOT pass expand=None (that would clobber the SDK's
    # own default of description.plain,homepage).
    client, _ = install(returns={"get_space": {"key": "DOCS"}})
    get_space(CLOUD, "DOCS")
    args, kwargs = client.last("get_space")
    assert args == ("DOCS",)
    assert kwargs == {}


def test_get_space_pages_materialises_generator(install):
    # get_all_pages_from_space returns a generator; the helper must return a
    # JSON-serialisable {"results": [...]} page, not the generator itself.
    def gen():
        yield {"id": "1"}
        yield {"id": "2"}

    client, _ = install(returns={"get_all_pages_from_space": gen})
    result = get_space_pages(CLOUD, "DOCS", start=0, limit=10)
    assert result == {"results": [{"id": "1"}, {"id": "2"}]}
    args, kwargs = client.last("get_all_pages_from_space")
    assert args == ("DOCS",)
    assert kwargs == {"start": 0, "limit": 10, "expand": None}


def test_get_space_pages_caps_iteration(install):
    def gen():
        yield from ({"id": str(i)} for i in range(500))

    client, _ = install(returns={"get_all_pages_from_space": gen})
    result = get_space_pages(CLOUD, "DOCS", limit=3)
    # limit both caps the SDK page size and hard-stops iteration.
    assert result == {"results": [{"id": "0"}, {"id": "1"}, {"id": "2"}]}


def test_get_current_user_cloud_uses_rest_api_path(install):
    # The SDK's Cloud get_current_user() requests bare "user/current" (no REST
    # root), which returns an empty body; Cloud must name the full v1 path.
    client, _ = install(returns={"get": {"displayName": "Me"}})
    assert get_current_user(CLOUD) == {"displayName": "Me"}
    args, _ = client.last("get")
    assert args == ("rest/api/user/current",)
    assert "get_current_user" not in [c[0] for c in client.calls]


def test_get_current_user_server_uses_sdk(install):
    client, _ = install(returns={"get_current_user": {"displayName": "Me"}})
    dc = ConfluenceConfig(url="https://wiki.corp", cloud=False, personal_token="p")
    assert get_current_user(dc) == {"displayName": "Me"}
    client.last("get_current_user")


def test_get_page_children_typed(install):
    client, _ = install(returns={"get_page_child_by_type": {"results": []}})
    get_page_children(CLOUD, "9", child_type="comment", start=2, limit=5)
    args, kwargs = client.last("get_page_child_by_type")
    assert args == ("9",)
    assert kwargs == {"type": "comment", "start": 2, "limit": 5, "expand": None}


def test_get_page_children_expand(install):
    client, _ = install(returns={"get_page_child_by_type": {}})
    get_page_children(CLOUD, "9", expand="body.view")
    _, kwargs = client.last("get_page_child_by_type")
    assert kwargs["expand"] == "body.view"


def test_cql_from_text_wraps_plain_text_as_text_field():
    # Default field is "text" (works on Cloud and Server/DC), not the Cloud-only
    # siteSearch.
    assert cql_from_text("onboarding docs") == 'text ~ "onboarding docs"'


def test_cql_from_text_sitesearch_opt_in():
    assert cql_from_text("onboarding", text_field="siteSearch") == 'siteSearch ~ "onboarding"'


def test_cql_from_text_passes_cql_through():
    assert cql_from_text('type = page AND text ~ "x"') == 'type = page AND text ~ "x"'


def test_cql_from_text_parenthesises_passthrough_before_clauses():
    # An OR in the user's query must not be re-scoped by the appended AND.
    assert cql_from_text("a OR b", content_type="page") == '(a OR b) AND type = "page"'


def test_cql_from_text_adds_space_and_type():
    out = cql_from_text("onboarding", space_key="DOCS", content_type="page")
    assert out == 'text ~ "onboarding" AND type = "page" AND space = "DOCS"'


def test_cql_from_text_escapes_quotes():
    assert cql_from_text('say "hi"') == 'text ~ "say \\"hi\\""'


def test_search_by_label(install):
    client, _ = install(returns={"cql": {"results": []}})
    search_by_label(CLOUD, "runbook", space_key="DOCS", limit=10, expand="body.view")
    args, kwargs = client.last("cql")
    assert args == ('label = "runbook" AND space = "DOCS"',)
    assert kwargs["limit"] == 10
    assert kwargs["expand"] == "body.view"


def test_search_caps_limit(install):
    client, _ = install(returns={"cql": {}})
    search(CLOUD, "type = page", limit=999)
    _, kwargs = client.last("cql")
    assert kwargs["limit"] == 100
