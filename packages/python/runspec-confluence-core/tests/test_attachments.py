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


def test_upload_new_attachment_posts_to_child_attachment(install, tmp_path):
    f = tmp_path / "f.pdf"
    f.write_bytes(b"%PDF-1.4 data")
    client, _ = install(returns={"get_attachments_from_content": {"results": []}, "post": {"id": "att1"}})
    upload_attachment(CFG, "9", str(f), content_type="application/pdf", comment="v1")
    _, kwargs = client.last("post")
    # New attachment → the create endpoint, not the /data update endpoint.
    assert kwargs["path"] == "rest/api/content/9/child/attachment"
    assert kwargs["headers"]["X-Atlassian-Token"] == "no-check"
    assert kwargs["data"]["comment"] == "v1"
    name, handle, ctype = kwargs["files"]["file"]
    assert name == "f.pdf"
    assert ctype == "application/pdf"


def test_upload_existing_attachment_posts_to_data_endpoint(install, tmp_path):
    # This is the 415 case: the file already exists, so update its data.
    f = tmp_path / "f.pdf"
    f.write_bytes(b"%PDF-1.4 newer")
    client, _ = install(returns={"get_attachments_from_content": {"results": [{"id": "att42", "title": "f.pdf"}]}, "post": {"id": "att42"}})
    upload_attachment(CFG, "9", str(f))
    _, kwargs = client.last("post")
    assert kwargs["path"] == "rest/api/content/9/child/attachment/att42/data"


def test_upload_guesses_content_type_and_name(install, tmp_path):
    f = tmp_path / "diagram.png"
    f.write_bytes(b"\x89PNG data")
    client, _ = install(returns={"get_attachments_from_content": {"results": []}, "post": {}})
    upload_attachment(CFG, "9", str(f))
    _, kwargs = client.last("post")
    name, _handle, ctype = kwargs["files"]["file"]
    assert name == "diagram.png"
    assert ctype == "image/png"


def test_upload_lookup_failure_falls_back_to_create(install, tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hi")

    def boom():
        raise RuntimeError("listing unsupported")

    client, _ = install(returns={"get_attachments_from_content": boom, "post": {}})
    upload_attachment(CFG, "9", str(f))
    _, kwargs = client.last("post")
    assert kwargs["path"] == "rest/api/content/9/child/attachment"


def test_delete_attachment(install):
    client, _ = install()
    assert delete_attachment(CFG, "9", "f.pdf") == {"page_id": "9", "filename": "f.pdf", "deleted": True}
    args, _ = client.last("delete_attachment")
    assert args == ("9", "f.pdf")
