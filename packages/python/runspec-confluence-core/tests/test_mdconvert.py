"""Markdown → storage conversion, and its use in create/update page."""

from runspec_confluence_core import ConfluenceConfig, create_page, markdown_to_storage, update_page

CFG = ConfluenceConfig(url="https://acme.atlassian.net/wiki", cloud=True, personal_token="p")


def test_markdown_to_storage_headings_and_lists():
    out = markdown_to_storage("# Title\n\n- a\n- b")
    assert "<h1>Title</h1>" in out
    assert "<li>a</li>" in out
    assert "<li>b</li>" in out


def test_markdown_to_storage_xhtml_self_closes():
    # xhtml output => self-closed tags, valid Confluence storage
    assert "<hr />" in markdown_to_storage("a\n\n---\n\nb")


def test_create_page_converts_markdown_to_storage(install):
    client, _ = install(returns={"create_page": {"id": "1"}})
    create_page(CFG, "DOCS", "T", "# Hi", representation="markdown")
    args, kwargs = client.last("create_page")
    assert "<h1>Hi</h1>" in args[2]  # body converted
    assert kwargs["representation"] == "storage"  # markdown collapses to storage


def test_create_page_storage_passthrough(install):
    client, _ = install(returns={"create_page": {}})
    create_page(CFG, "DOCS", "T", "<p>x</p>", representation="storage")
    args, kwargs = client.last("create_page")
    assert args[2] == "<p>x</p>"  # untouched
    assert kwargs["representation"] == "storage"


def test_update_page_markdown(install):
    client, _ = install(returns={"update_page": {}})
    update_page(CFG, "9", "T", body="**bold**", representation="markdown")
    args, kwargs = client.last("update_page")
    assert "<strong>bold</strong>" in kwargs["body"]
    assert kwargs["representation"] == "storage"


def test_update_page_none_body_markdown_is_safe(install):
    client, _ = install(returns={"update_page": {}})
    update_page(CFG, "9", "T", body=None, representation="markdown")
    _, kwargs = client.last("update_page")
    assert kwargs["body"] is None
    assert kwargs["representation"] == "storage"
