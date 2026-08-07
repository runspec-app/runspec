"""Attachment op wiring."""

from runspec_confluence_core import (
    ConfluenceConfig,
    delete_attachment,
    download_attachments,
    get_attachments,
    upload_attachment,
)

CFG = ConfluenceConfig(url="https://acme.atlassian.net/wiki", cloud=True, personal_token="p")


def test_get_attachments(install):
    client, _ = install(returns={"get_attachments_from_content": {"results": []}})
    get_attachments(CFG, "9", limit=10, filename="a.png")
    _, kwargs = client.last("get_attachments_from_content")
    assert kwargs["limit"] == 10
    assert kwargs["filename"] == "a.png"


def test_download_attachments(install):
    client, _ = install(returns={"download_attachments_from_page": "/tmp/out"})
    result = download_attachments(CFG, "9", "/tmp/out")
    assert result == {"page_id": "9", "path": "/tmp/out", "downloaded": True}
    _, kwargs = client.last("download_attachments_from_page")
    assert kwargs["path"] == "/tmp/out"


def test_upload_attachment(install):
    client, _ = install(returns={"attach_file": {"id": "att1"}})
    upload_attachment(CFG, "9", "/tmp/f.pdf", name="f.pdf", content_type="application/pdf", comment="v1")
    args, kwargs = client.last("attach_file")
    assert args == ("/tmp/f.pdf",)
    assert kwargs["page_id"] == "9"
    assert kwargs["content_type"] == "application/pdf"
    assert kwargs["comment"] == "v1"


def test_delete_attachment(install):
    client, _ = install()
    assert delete_attachment(CFG, "9", "f.pdf") == {"page_id": "9", "filename": "f.pdf", "deleted": True}
    args, _ = client.last("delete_attachment")
    assert args == ("9", "f.pdf")
