"""Attachment op wiring."""

from runspec_jira_core import (
    JiraConfig,
    add_attachment,
    download_attachments,
    list_attachments,
    remove_attachment,
)

CLOUD = JiraConfig(url="https://acme.atlassian.net", cloud=True, personal_token="p")
DC = JiraConfig(url="https://jira.corp", cloud=False, personal_token="p")


def test_list_attachments(install):
    jira, _, _ = install(returns={"get_attachments_ids_from_issue": [{"id": "1", "filename": "a.png"}]})
    assert list_attachments(CLOUD, "P-1") == [{"id": "1", "filename": "a.png"}]
    args, _ = jira.last("get_attachments_ids_from_issue")
    assert args == ("P-1",)


def test_download_attachments_passes_cloud_and_path(install):
    jira, _, _ = install(returns={"download_attachments_from_issue": "/tmp/out"})
    result = download_attachments(CLOUD, "P-1", "/tmp/out")
    assert result == {"issue": "P-1", "path": "/tmp/out", "downloaded": True}
    _, kwargs = jira.last("download_attachments_from_issue")
    assert kwargs["path"] == "/tmp/out"
    assert kwargs["cloud"] is True


def test_download_attachments_dc_flag(install):
    jira, _, _ = install(returns={"download_attachments_from_issue": None})
    result = download_attachments(DC, "P-1", "/tmp/out")
    assert result["path"] == "/tmp/out"  # falls back to dest when SDK returns None
    _, kwargs = jira.last("download_attachments_from_issue")
    assert kwargs["cloud"] is False


def test_add_attachment(install):
    jira, _, _ = install(returns={"add_attachment": {"id": "9"}})
    assert add_attachment(CLOUD, "P-1", "/tmp/f.txt") == {"id": "9"}
    args, _ = jira.last("add_attachment")
    assert args == ("P-1", "/tmp/f.txt")


def test_remove_attachment(install):
    jira, _, _ = install()
    assert remove_attachment(CLOUD, "55") == {"attachment_id": "55", "removed": True}
    args, _ = jira.last("remove_attachment")
    assert args == ("55",)
