"""Operation wiring: each core op calls the right SDK method with the right args,
parses JSON-string params, branches Cloud vs DC, and translates errors."""

import re

import pytest
import requests

from runspec_jira_core import (
    JiraConfig,
    JiraError,
    add_comment,
    add_worklog,
    assign_issue,
    build_client,
    create_customer_request,
    create_issue,
    create_issue_link,
    get_issue,
    get_user_profile,
    link_to_epic,
    list_request_types,
    remove_watcher,
    search_projects,
    transition_issue,
    update_issue,
)
from tests.conftest import FakeResp

CLOUD = JiraConfig(url="https://acme.atlassian.net", cloud=True, username="me@x.com", api_token="tok")
DC = JiraConfig(url="https://jira.corp", cloud=False, personal_token="pat")


# ── auth kwargs ──────────────────────────────────────────────────────────────


def test_build_client_basic_kwargs(install):
    _, _, made = install()
    build_client(CLOUD)
    assert made["jira"] == {"url": "https://acme.atlassian.net", "cloud": True, "verify_ssl": True, "username": "me@x.com", "password": "tok"}


def test_build_client_pat_kwargs(install):
    _, _, made = install()
    build_client(DC)
    assert made["jira"] == {"url": "https://jira.corp", "cloud": False, "verify_ssl": True, "token": "pat"}


# ── issues ───────────────────────────────────────────────────────────────────


def test_get_issue_joins_fields(install):
    jira, _, _ = install(returns={"get_issue": {"key": "P-1"}})
    assert get_issue(CLOUD, "P-1", fields=["summary", "status"]) == {"key": "P-1"}
    args, kwargs = jira.last("get_issue")
    assert args == ("P-1",)
    assert kwargs["fields"] == "summary,status"


def test_create_issue_merges_additional_fields(install):
    jira, _, _ = install(returns={"create_issue": {"key": "P-9"}})
    create_issue(CLOUD, "P", "Fix", "Bug", description="d", additional_fields='{"labels": ["x"]}')
    _, kwargs = jira.last("create_issue")
    assert kwargs["fields"] == {
        "project": {"key": "P"},
        "summary": "Fix",
        "issuetype": {"name": "Bug"},
        "description": "d",
        "labels": ["x"],
    }


def test_create_issue_bad_json_raises(install):
    install(returns={"create_issue": {}})
    with pytest.raises(JiraError, match="not valid JSON"):
        create_issue(CLOUD, "P", "s", "Bug", additional_fields="{oops")


def test_update_issue_sends_plain_fields(install):
    # The SDK's edit_issue wraps fields as {"update": ...}; Jira 400s on plain values.
    jira, _, _ = install()
    assert update_issue(CLOUD, "P-1", fields_json='{"labels": ["test"], "summary": "New"}') == {"key": "P-1", "updated": True}
    args, kwargs = jira.last("put")
    assert args == ("rest/api/2/issue/P-1",)
    assert kwargs == {"data": {"fields": {"labels": ["test"], "summary": "New"}}}


def test_update_issue_passes_operations_through(install):
    jira, _, _ = install()
    update_issue(CLOUD, "P-1", fields_json='{"summary": "New", "labels": [{"add": "urgent"}, {"remove": "old"}]}')
    _, kwargs = jira.last("put")
    assert kwargs == {"data": {"fields": {"summary": "New"}, "update": {"labels": [{"add": "urgent"}, {"remove": "old"}]}}}


def test_update_issue_accepts_a_full_body(install):
    jira, _, _ = install()
    update_issue(CLOUD, "P-1", fields_json='{"update": {"summary": [{"set": "New"}]}}')
    _, kwargs = jira.last("put")
    assert kwargs == {"data": {"update": {"summary": [{"set": "New"}]}}}


def test_update_issue_requires_object(install):
    install()
    with pytest.raises(JiraError, match="must be a JSON object"):
        update_issue(CLOUD, "P-1", fields_json="[1,2]")


def test_assign_issue_cloud_uses_account_id(install):
    jira, _, _ = install()
    assign_issue(CLOUD, "P-1", "acc-123")
    args, kwargs = jira.last("assign_issue")
    assert args == ("P-1",)
    assert kwargs == {"account_id": "acc-123"}


def test_assign_issue_dc_edits_name(install):
    jira, _, _ = install()
    assign_issue(DC, "P-1", "jbloggs")
    args, kwargs = jira.last("put")
    assert args == ("rest/api/2/issue/P-1",)
    assert kwargs == {"data": {"fields": {"assignee": {"name": "jbloggs"}}}}


# ── transitions / comments ───────────────────────────────────────────────────


def test_transition_issue_calls_set_status(install):
    jira, _, _ = install()
    transition_issue(CLOUD, "P-1", status="Done")
    args, kwargs = jira.last("set_issue_status")
    assert args == ("P-1", "Done")
    assert kwargs == {"fields": None}


def test_add_comment_parses_visibility(install):
    jira, _, _ = install(returns={"issue_add_comment": {"id": "1"}})
    add_comment(CLOUD, "P-1", "hi", visibility_json='{"type": "role", "value": "Admin"}')
    _, kwargs = jira.last("issue_add_comment")
    assert kwargs["visibility"] == {"type": "role", "value": "Admin"}


# ── links ────────────────────────────────────────────────────────────────────


def test_create_issue_link_builds_payload(install):
    jira, _, _ = install()
    create_issue_link(CLOUD, "Blocks", "A-1", "B-2", comment="c")
    (data,), _ = jira.last("create_issue_link")
    assert data == {
        "type": {"name": "Blocks"},
        "inwardIssue": {"key": "A-1"},
        "outwardIssue": {"key": "B-2"},
        "comment": {"body": "c"},
    }


def test_link_to_epic_cloud_uses_parent(install):
    jira, _, _ = install()
    link_to_epic(CLOUD, "P-1", "EPIC-1")
    _, kwargs = jira.last("put")
    assert kwargs == {"data": {"fields": {"parent": {"key": "EPIC-1"}}}}


def test_link_to_epic_dc_discovers_field(install):
    jira, _, _ = install(returns={"get_all_fields": [{"id": "customfield_10014", "name": "Epic Link"}]})
    link_to_epic(DC, "P-1", "EPIC-1")
    _, kwargs = jira.last("put")
    assert kwargs == {"data": {"fields": {"customfield_10014": "EPIC-1"}}}


def test_link_to_epic_dc_missing_field_raises(install):
    install(returns={"get_all_fields": [{"id": "summary", "name": "Summary"}]})
    with pytest.raises(JiraError, match="Epic Link"):
        link_to_epic(DC, "P-1", "EPIC-1")


# ── worklog / projects / users / watchers ────────────────────────────────────


def test_add_worklog_needs_time_or_json(install):
    install()
    with pytest.raises(JiraError, match="time-sec"):
        add_worklog(CLOUD, "P-1")


def test_add_worklog_defaults_started_to_now(install):
    jira, _, _ = install(returns={"issue_worklog": {"id": "1"}})
    add_worklog(CLOUD, "P-1", time_sec=1800)
    args, _ = jira.last("issue_worklog")
    assert args[0] == "P-1" and args[2] == 1800
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.000[+-]\d{4}", args[1])


def test_search_projects_filters(install):
    projects = [{"key": "ABC", "name": "Alpha"}, {"key": "XYZ", "name": "Beta"}]
    install(returns={"get_all_projects": projects})
    result = search_projects(CLOUD, "alph")
    assert result == [{"key": "ABC", "name": "Alpha"}]


def test_get_user_profile_cloud_requires_account_id(install):
    install()
    with pytest.raises(JiraError, match="account-id"):
        get_user_profile(CLOUD)


def test_remove_watcher_dc_requires_user(install):
    install()
    with pytest.raises(JiraError, match="--user"):
        remove_watcher(DC, "P-1")


# ── service desk ─────────────────────────────────────────────────────────────


def test_list_request_types_falls_back_to_config_sd_id(install):
    cfg = JiraConfig(url="https://x.atlassian.net", cloud=True, username="u@x", api_token="t", service_desk_id="5")
    _, sd, _ = install(sd_returns={"get_request_types": [{"id": "1"}]})
    list_request_types(cfg)
    (args, _) = sd.last("get_request_types")
    assert args == ("5",)


def test_create_customer_request_requires_sd_id(install):
    install()
    with pytest.raises(JiraError, match="service desk id"):
        create_customer_request(CLOUD, "rt-1", "{}")


# ── error translation ────────────────────────────────────────────────────────


def test_http_error_becomes_jira_error(install):
    err = requests.HTTPError("boom")
    err.response = FakeResp(400, {"errorMessages": ["Field 'x' is required"]})
    install(returns={"get_issue": err})
    with pytest.raises(JiraError, match="Field 'x' is required"):
        get_issue(CLOUD, "P-1")


def test_html_error_page_becomes_one_line(install):
    page = "\n\n<html>\n<head>\n<title> (null)</title>\n<script>var contextPath = '';</script>" + "x" * 5000
    resp = FakeResp(403, None)
    resp.text = page
    resp.url = "https://acme.atlassian.net/rest/servicedeskapi/servicedesk"

    def no_json():
        raise ValueError("not JSON")

    resp.json = no_json
    err = requests.HTTPError("boom")
    err.response = resp
    install(returns={"get_issue": err})
    with pytest.raises(JiraError) as info:
        get_issue(CLOUD, "P-1")
    message = str(info.value)
    assert message.startswith("Jira 403: got an HTML page from https://acme.atlassian.net/rest/servicedeskapi/servicedesk")
    assert "<html>" not in message and "(null)" not in message
    assert len(message) < 400


def test_servicedesk_error_message_shape(install):
    err = requests.HTTPError("boom")
    err.response = FakeResp(404, {"errorMessage": "No such request type"})
    install(sd_returns={"get_request_types": err})
    cfg = JiraConfig(url="https://x.atlassian.net", cloud=True, username="u@x", api_token="t", service_desk_id="5")
    with pytest.raises(JiraError, match="No such request type"):
        list_request_types(cfg)
