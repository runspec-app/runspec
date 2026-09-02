"""Operation wiring: each core op calls the right SDK method, resolves
space defaults, branches get-page by id vs title, and translates errors."""

import pytest
import requests

from runspec_confluence_core import (
    ConfluenceConfig,
    ConfluenceError,
    add_comment,
    add_label,
    build_client,
    create_page,
    delete_page,
    get_comments,
    get_page,
    search,
    update_page,
)
from tests.conftest import FakeResp

CLOUD = ConfluenceConfig(url="https://acme.atlassian.net/wiki", cloud=True, username="me@x.com", api_token="tok")
DC = ConfluenceConfig(url="https://wiki.corp", cloud=False, personal_token="pat")


def test_build_client_basic_kwargs(install):
    _, made = install()
    build_client(CLOUD)
    assert made["kw"] == {"url": "https://acme.atlassian.net/wiki", "cloud": True, "verify_ssl": True, "username": "me@x.com", "password": "tok"}


def test_build_client_pat_kwargs(install):
    _, made = install()
    build_client(DC)
    assert made["kw"] == {"url": "https://wiki.corp", "cloud": False, "verify_ssl": True, "token": "pat"}


def test_get_page_by_id(install):
    client, _ = install(returns={"get_page_by_id": {"id": "1"}})
    assert get_page(CLOUD, page_id="1", expand="body.storage") == {"id": "1"}
    args, kwargs = client.last("get_page_by_id")
    assert args == ("1",)
    assert kwargs["expand"] == "body.storage"


def test_get_page_by_title(install):
    client, _ = install(returns={"get_page_by_title": {"id": "2"}})
    get_page(CLOUD, space="DOCS", title="Home")
    args, _ = client.last("get_page_by_title")
    assert args == ("DOCS", "Home")


def test_get_page_uses_config_space(install):
    cfg = ConfluenceConfig(url="https://acme.atlassian.net/wiki", cloud=True, personal_token="p", space="TEAM")
    client, _ = install(returns={"get_page_by_title": {}})
    get_page(cfg, title="Runbook")
    args, _ = client.last("get_page_by_title")
    assert args == ("TEAM", "Runbook")


def test_get_page_without_locator_raises(install):
    install()
    with pytest.raises(ConfluenceError, match="page-id"):
        get_page(CLOUD)


def test_create_page_passes_representation(install):
    client, _ = install(returns={"create_page": {"id": "9"}})
    create_page(CLOUD, "DOCS", "New", "<p>hi</p>", parent_id="3", representation="storage")
    args, kwargs = client.last("create_page")
    assert args == ("DOCS", "New", "<p>hi</p>")
    assert kwargs == {"parent_id": "3", "representation": "storage"}


def test_update_page_threads_args(install):
    client, _ = install(returns={"update_page": {}})
    update_page(CLOUD, "9", "Title", body="<p>x</p>", version_comment="tweak", minor_edit=True)
    args, kwargs = client.last("update_page")
    assert args == ("9", "Title")
    assert kwargs["version_comment"] == "tweak"
    assert kwargs["minor_edit"] is True


def test_delete_page_returns_status(install):
    client, _ = install()
    assert delete_page(CLOUD, "9", recursive=True) == {"page_id": "9", "deleted": True}
    _, kwargs = client.last("remove_page")
    assert kwargs["recursive"] is True


def test_search_calls_cql(install):
    client, _ = install(returns={"cql": {"results": []}})
    search(CLOUD, "type = page", limit=5)
    args, kwargs = client.last("cql")
    assert args == ("type = page",)
    assert kwargs["limit"] == 5


def test_get_comments_expands_body(install):
    client, _ = install(returns={"get_page_comments": {"results": []}})
    get_comments(CLOUD, "9")
    _, kwargs = client.last("get_page_comments")
    assert kwargs["expand"] == "body.view"


def test_get_comments_expand_overridable(install):
    client, _ = install(returns={"get_page_comments": {"results": []}})
    get_comments(CLOUD, "9", expand="body.storage,history")
    _, kwargs = client.last("get_page_comments")
    assert kwargs["expand"] == "body.storage,history"


def test_get_attachments_expand(install):
    from runspec_confluence_core import get_attachments

    client, _ = install(returns={"get_attachments_from_content": {"results": []}})
    get_attachments(CLOUD, "9", expand="version,history")
    _, kwargs = client.last("get_attachments_from_content")
    assert kwargs["expand"] == "version,history"


def test_add_label(install):
    client, _ = install()
    add_label(CLOUD, "9", "runbook")
    args, _ = client.last("set_page_label")
    assert args == ("9", "runbook")


def test_add_comment(install):
    client, _ = install()
    add_comment(CLOUD, "9", "looks good")
    args, _ = client.last("add_comment")
    assert args == ("9", "looks good")


def test_http_error_becomes_confluence_error(install):
    err = requests.HTTPError("boom")
    err.response = FakeResp(404, {"message": "No content found with id: 9"})
    install(returns={"get_page_by_id": err})
    with pytest.raises(ConfluenceError, match="No content found"):
        get_page(CLOUD, page_id="9")
