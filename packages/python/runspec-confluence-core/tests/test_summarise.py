"""Read-side shaping: html_to_markdown + simplify_* trimming."""

from runspec_confluence_core import (
    html_to_markdown,
    simplify_attachment,
    simplify_comment,
    simplify_page,
    simplify_space,
    simplify_user,
)


def test_html_to_markdown_atx_headings_and_dash_bullets():
    # ATX (# ...) headings and a stable "-" bullet, not Setext / cycling *+-.
    md = html_to_markdown("<h1>Title</h1><p>Body</p><ul><li>a</li><li>b</li></ul>")
    assert "# Title" in md
    assert "Body" in md
    assert "- a" in md and "- b" in md


def test_html_to_markdown_link():
    md = html_to_markdown('<a href="https://x/y">link</a>')
    assert "[link](https://x/y)" in md


def test_html_to_markdown_empty():
    assert html_to_markdown("") == ""
    assert html_to_markdown(None) == ""


def test_simplify_page_trims_and_composes_url():
    raw = {
        "id": "123",
        "type": "page",
        "status": "current",
        "title": "Onboarding",
        "space": {"key": "DOCS", "name": "Docs", "id": 9, "type": "global"},
        "version": {"number": 3, "when": "2026-01-01T00:00:00Z", "by": {"displayName": "Ada"}, "message": "edit"},
        "history": {"createdDate": "2025-01-01T00:00:00Z", "createdBy": {"displayName": "Grace"}},
        "ancestors": [{"id": "1", "title": "Home", "extra": "drop"}],
        "body": {"storage": {"value": "<p>hi</p>", "representation": "storage"}, "view": {"value": "<p>rendered</p>"}},
        "_links": {"webui": "/spaces/DOCS/pages/123", "base": "https://acme.atlassian.net/wiki"},
    }
    out = simplify_page(raw)
    assert out["id"] == "123"
    assert out["title"] == "Onboarding"
    assert out["space"] == {"key": "DOCS", "name": "Docs"}
    assert out["version"] == {"number": 3, "when": "2026-01-01T00:00:00Z", "by": "Ada"}
    assert out["created"] == "2025-01-01T00:00:00Z"
    assert out["author"] == "Grace"
    assert out["ancestors"] == [{"id": "1", "title": "Home"}]
    assert out["body"] == {"value": "<p>hi</p>", "format": "storage"}
    assert out["url"] == "https://acme.atlassian.net/wiki/spaces/DOCS/pages/123"


def test_simplify_page_partial_is_safe():
    assert simplify_page({"id": "1", "title": "X"}) == {"id": "1", "title": "X"}


def test_simplify_page_body_as_markdown():
    raw = {"id": "1", "title": "T", "body": {"storage": {"value": "<h1>Hi</h1>"}}}
    out = simplify_page(raw, body_as_markdown=True)
    assert out["body"]["format"] == "markdown"
    assert "# Hi" in out["body"]["value"]
    # default leaves the raw HTML untouched
    assert simplify_page(raw)["body"] == {"value": "<h1>Hi</h1>", "format": "storage"}


def test_simplify_comment_reads_history_author_created():
    raw = {
        "id": "c1",
        "title": "Re: page",
        "body": {"view": {"value": "<p>looks good</p>"}},
        "version": {"number": 2, "by": {"displayName": "Ada"}},
        "history": {"createdDate": "2025-01-01T00:00:00Z", "createdBy": {"displayName": "Grace"}},
        "_links": {"webui": "/x"},
    }
    out = simplify_comment(raw)
    assert out["body"] == "<p>looks good</p>"
    assert out["version"] == 2
    assert out["by"] == "Ada"  # last editor
    assert out["author"] == "Grace"  # original author, from history
    assert out["created"] == "2025-01-01T00:00:00Z"


def test_simplify_space_reads_description():
    raw = {"id": 9, "key": "DOCS", "name": "Docs", "type": "global", "status": "current", "description": {"plain": {"value": "Team docs"}}, "_links": {"webui": "/spaces/DOCS"}}
    out = simplify_space(raw)
    assert out["description"] == "Team docs"
    assert out["key"] == "DOCS"


def test_simplify_user_cloud():
    raw = {"accountId": "abc", "accountType": "atlassian", "displayName": "Ada", "email": "ada@x.com"}
    out = simplify_user(raw)
    assert out["accountId"] == "abc"
    assert out["displayName"] == "Ada"
    assert out["email"] == "ada@x.com"


def test_simplify_user_server_dc():
    # Server/DC nests email + carries type/userKey/avatar.
    raw = {
        "type": "known",
        "username": "ada",
        "userKey": "ff80",
        "displayName": "Ada",
        "details": {"business": {"email": "ada@corp.example"}},
        "profilePicture": {"path": "/images/icons/profilepics/default.png"},
    }
    out = simplify_user(raw)
    assert out["type"] == "known"
    assert out["userKey"] == "ff80"
    assert out["username"] == "ada"
    assert out["email"] == "ada@corp.example"
    assert out["avatar"] == "/images/icons/profilepics/default.png"


def test_simplify_attachment():
    raw = {
        "id": "att1",
        "title": "diagram.png",
        "metadata": {"mediaType": "image/png"},
        "extensions": {"fileSize": 2048},
        "version": {"number": 1},
        "history": {"createdDate": "2025-01-01T00:00:00Z", "createdBy": {"displayName": "Grace"}},
        "_links": {"download": "/download/att1", "webui": "/x"},
    }
    out = simplify_attachment(raw)
    assert out["id"] == "att1"
    assert out["mediaType"] == "image/png"
    assert out["fileSize"] == 2048
    assert out["version"] == 1
    assert out["download"] == "/download/att1"
    assert out["created"] == "2025-01-01T00:00:00Z"
    assert out["created_by"] == "Grace"


def test_simplify_attachment_filesize_metadata_fallback():
    raw = {"id": "a", "metadata": {"mediaType": "text/plain", "fileSize": 99}}
    assert simplify_attachment(raw)["fileSize"] == 99
