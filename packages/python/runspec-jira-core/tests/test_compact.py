"""Compact views: the agent-sized shapes of an issue and a search page, and the
fetches that ask the API only for the fields those views need."""

import json

import pytest

from runspec_jira_core import (
    JiraConfig,
    compact_issue,
    compact_search,
    get_issue_compact,
    project_issues_compact,
    search_compact,
)
from runspec_jira_core.compact import COMMENT_LIMIT, DESCRIPTION_CHARS

CLOUD = JiraConfig(url="https://acme.atlassian.net", cloud=True, username="me@x.com", api_token="tok")

AVATARS = {"48x48": "https://x/avatar.png", "24x24": "https://x/a24.png"}


def _issue(**fields):
    base = {
        "summary": "Fix login",
        "status": {"name": "In Progress", "statusCategory": {"name": "In Progress"}, "self": "https://x"},
        "issuetype": {"name": "Bug", "iconUrl": "https://x/bug.png"},
        "priority": {"name": "High"},
        "assignee": {"displayName": "Sam Lee", "avatarUrls": AVATARS},
        "reporter": {"displayName": "Ana Diaz", "avatarUrls": AVATARS},
        "created": "2026-10-01T09:00:00.000+0000",
        "updated": "2026-10-15T12:00:00.000+0000",
        "resolution": None,
        "labels": ["auth"],
        "components": [{"name": "web", "self": "https://x"}],
        "fixVersions": [],
        "description": "Users can't log in after the upgrade.",
        "comment": {"comments": [], "total": 0},
        "issuelinks": [],
        "subtasks": [],
        "attachment": [],
        "customfield_10001": None,
        "customfield_10002": None,
    }
    base.update(fields)
    return {"key": "PROJ-1", "id": "10001", "self": "https://x/issue/10001", "fields": base}


def test_compact_issue_keeps_the_essentials_and_drops_noise():
    view = compact_issue(_issue(), base_url="https://acme.atlassian.net")
    assert view["key"] == "PROJ-1"
    assert view["url"] == "https://acme.atlassian.net/browse/PROJ-1"
    assert view["status"] == "In Progress"
    assert view["type"] == "Bug"
    assert view["assignee"] == "Sam Lee"
    assert view["components"] == ["web"]
    assert view["description"] == "Users can't log in after the upgrade."
    # Empty values and API noise are gone; counts stay, even at zero.
    assert "resolution" not in view and "fix_versions" not in view
    assert view["comment_count"] == 0 and view["attachment_count"] == 0
    blob = json.dumps(view)
    assert "avatar" not in blob and "self" not in blob and "customfield" not in blob


def test_compact_issue_is_much_smaller_than_raw():
    raw = _issue(**{f"customfield_{i}": None for i in range(200)})
    assert len(json.dumps(compact_issue(raw))) * 5 < len(json.dumps(raw))


def test_description_is_previewed_unless_included():
    long = "x" * (DESCRIPTION_CHARS * 3)
    view = compact_issue(_issue(description=long))
    assert len(view["description"]) < len(long)
    assert view["description_chars"] == len(long)
    full = compact_issue(_issue(description=long), include=["description"])
    assert full["description"] == long and "description_chars" not in full


def test_cloud_adf_description_is_flattened():
    adf = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]}]}
    assert compact_issue(_issue(description=adf))["description"] == "Hello"


def test_include_comments_keeps_the_newest():
    comments = [{"id": str(i), "author": {"displayName": "A"}, "created": f"t{i}", "body": f"c{i}"} for i in range(30)]
    view = compact_issue(_issue(comment={"comments": comments, "total": 30}), include=["comments"])
    assert view["comment_count"] == 30
    assert len(view["comments"]) == COMMENT_LIMIT
    assert view["comments"][-1] == {"id": "29", "author": "A", "created": "t29", "body": "c29"}
    assert view["comments_shown"] == COMMENT_LIMIT
    assert "comments" not in compact_issue(_issue(comment={"comments": comments, "total": 30}))


def test_include_links_subtasks_attachments_changelog():
    raw = _issue(
        issuelinks=[
            {"type": {"inward": "is blocked by", "outward": "blocks"}, "outwardIssue": {"key": "PROJ-2", "fields": {"summary": "s2", "status": {"name": "Open"}}}},
            {"type": {"inward": "is blocked by", "outward": "blocks"}, "inwardIssue": {"key": "PROJ-3", "fields": {"summary": "s3"}}},
        ],
        subtasks=[{"key": "PROJ-4", "fields": {"summary": "sub", "status": {"name": "Done"}}}],
        attachment=[{"id": "9", "filename": "log.txt", "size": 120, "author": {"displayName": "A"}, "content": "https://x"}],
    )
    raw["changelog"] = {"histories": [{"created": "t", "author": {"displayName": "A"}, "items": [{"field": "status", "fromString": "Open", "toString": "In Progress"}]}]}
    view = compact_issue(raw, include=["links", "subtasks", "attachments", "changelog"])
    assert view["links"] == [
        {"relation": "blocks", "key": "PROJ-2", "summary": "s2", "status": "Open"},
        {"relation": "is blocked by", "key": "PROJ-3", "summary": "s3"},
    ]
    assert view["subtasks"] == [{"key": "PROJ-4", "summary": "sub", "status": "Done"}]
    assert view["attachments"] == [{"id": "9", "filename": "log.txt", "size": 120, "author": "A"}]
    assert view["changelog"] == [{"when": "t", "who": "A", "field": "status", "from": "Open", "to": "In Progress"}]


def test_include_custom_names_non_empty_fields():
    raw = _issue(customfield_10003={"value": "Leeds"}, customfield_10004=[{"name": "x"}])
    raw["names"] = {"customfield_10003": "Office"}
    view = compact_issue(raw, include=["custom"])
    assert view["custom"] == {"Office": "Leeds", "customfield_10004": ["x"]}


def test_compact_search_rows_and_paging():
    raw = {"startAt": 0, "maxResults": 2, "total": 5, "issues": [_issue(), _issue()]}
    page = compact_search(raw)
    assert page["count"] == 2 and page["total"] == 5
    assert page["truncated"] is True and page["next_start"] == 2
    assert page["issues"][0] == {
        "key": "PROJ-1",
        "summary": "Fix login",
        "status": "In Progress",
        "type": "Bug",
        "priority": "High",
        "assignee": "Sam Lee",
        "updated": "2026-10-15T12:00:00.000+0000",
    }
    last = compact_search({"startAt": 4, "total": 5, "issues": [_issue()]})
    assert last["truncated"] is False and "next_start" not in last


def test_get_issue_compact_fetches_only_needed_fields(install):
    jira, _, _ = install(returns={"get_issue": _issue()})
    view = get_issue_compact(CLOUD, "PROJ-1")
    assert view["url"] == "https://acme.atlassian.net/browse/PROJ-1"
    args, kwargs = jira.last("get_issue")
    assert args == ("PROJ-1",)
    assert "summary" in kwargs["fields"] and kwargs["fields"] != "*all"
    assert kwargs["expand"] is None


def test_get_issue_compact_changelog_and_custom_expand(install):
    jira, _, _ = install(returns={"get_issue": _issue()})
    get_issue_compact(CLOUD, "PROJ-1", include=["changelog", "custom"])
    _, kwargs = jira.last("get_issue")
    assert kwargs["fields"] == "*all"
    assert kwargs["expand"] == "changelog,names"


def test_get_issue_compact_rejects_unknown_include(install):
    install(returns={"get_issue": _issue()})
    with pytest.raises(ValueError, match="unknown include"):
        get_issue_compact(CLOUD, "PROJ-1", include=["everything"])


def test_search_compact_and_project_issues(install):
    jira, _, _ = install(returns={"jql": {"startAt": 0, "total": 1, "issues": [_issue()]}})
    page = search_compact(CLOUD, "assignee = currentUser()", limit=10)
    assert page["count"] == 1
    args, kwargs = jira.last("jql")
    assert args == ("assignee = currentUser()",)
    assert kwargs == {"fields": "summary,status,assignee,priority,issuetype,updated", "start": 0, "limit": 10}
    project_issues_compact(CLOUD, "PROJ", start=25)
    args, kwargs = jira.last("jql")
    assert args == ('project = "PROJ" ORDER BY created DESC',)
    assert kwargs["start"] == 25
