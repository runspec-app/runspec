"""Project roles, JSM customers/organizations and the user access check."""

import pytest

from runspec_jira_core import (
    JiraConfig,
    JiraError,
    add_customers,
    add_to_organization,
    add_to_role,
    check_user_access,
    get_role_members,
    list_project_roles,
    remove_from_role,
    resolve_service_desk,
)
from tests.rest_fake import page

DC = JiraConfig(url="https://jira.corp", cloud=False, personal_token="pat")
BASE = "https://jira.corp/rest/api/2/project/HR/role"
ROLES = {"Service Desk Team": f"{BASE}/10100", "Service Desk Customers": f"{BASE}/10200", "Administrators": f"{BASE}/10002"}
TEAM = {
    "id": 10100,
    "name": "Service Desk Team",
    "actors": [
        {"type": "atlassian-user-role-actor", "name": "alice", "displayName": "Alice"},
        {"type": "atlassian-group-role-actor", "name": "hr-agents", "displayName": "hr-agents"},
    ],
}
DESKS = page([{"id": "7", "projectId": "100", "projectKey": "HR", "projectName": "Human Resources"}])


def _role_routes(extra=None):
    routes = {
        ("get", "rest/api/2/project/HR/role"): ROLES,
        ("get", "rest/api/2/project/HR/role/10100"): TEAM,
        ("get", "rest/api/2/project/HR/role/10200"): {"id": 10200, "name": "Service Desk Customers", "actors": []},
        ("get", "rest/api/2/project/HR/role/10002"): {"id": 10002, "name": "Administrators", "actors": []},
    }
    routes.update(extra or {})
    return routes


def test_list_project_roles_parses_ids(rest):
    rest(_role_routes())
    assert {"id": "10100", "name": "Service Desk Team"} in list_project_roles(DC, "HR")


def test_get_role_members_by_name_case_insensitive(rest):
    rest(_role_routes())
    members = get_role_members(DC, "HR", "service desk team")
    assert members["users"] == [{"user": "alice", "displayName": "Alice"}]
    assert members["groups"] == ["hr-agents"]


def test_unknown_role_suggests(rest):
    rest(_role_routes())
    with pytest.raises(JiraError, match="did you mean 'Service Desk Team'"):
        get_role_members(DC, "HR", "Service Desk Teem")


def test_add_to_role_is_idempotent(rest):
    fake = rest(_role_routes())
    result = add_to_role(DC, "HR", "Service Desk Team", users=["alice", "bob"], groups=["hr-agents", "hr-leads"])
    assert result["added_users"] == ["bob"] and result["already_users"] == ["alice"]
    assert result["added_groups"] == ["hr-leads"] and result["already_groups"] == ["hr-agents"]
    assert fake.sent("post") == [
        ("rest/api/2/project/HR/role/10100", {"data": {"user": ["bob"]}}),
        ("rest/api/2/project/HR/role/10100", {"data": {"group": ["hr-leads"]}}),
    ]


def test_add_to_role_needs_someone(rest):
    rest(_role_routes())
    with pytest.raises(JiraError, match="at least one"):
        add_to_role(DC, "HR", "Service Desk Team")


def test_remove_from_role_only_deletes_members(rest):
    fake = rest(_role_routes())
    result = remove_from_role(DC, "HR", "10100", users=["alice", "zed"])
    assert result["removed_users"] == ["alice"] and result["not_member_users"] == ["zed"]
    assert fake.sent("delete") == [("rest/api/2/project/HR/role/10100", {"params": {"user": "alice"}})]


def test_resolve_service_desk_by_name(rest):
    rest({("get", "rest/servicedeskapi/servicedesk"): DESKS})
    assert resolve_service_desk(DC, "human resources")["id"] == "7"


def test_resolve_service_desk_unknown_suggests(rest):
    rest({("get", "rest/servicedeskapi/servicedesk"): DESKS})
    with pytest.raises(JiraError, match="did you mean 'HR'"):
        resolve_service_desk(DC, "HRR")


def test_add_customers_dc_uses_usernames(rest):
    fake = rest({("get", "rest/servicedeskapi/servicedesk"): DESKS})
    assert add_customers(DC, "HR", ["carol", "carol"]) == {"project": "HR", "added": ["carol"]}
    path, kw = fake.sent("post")[0]
    assert path == "rest/servicedeskapi/servicedesk/7/customer"
    assert kw["data"] == {"usernames": ["carol"]}


def test_add_to_organization_creates_and_links(rest):
    fake = rest(
        {
            ("get", "rest/servicedeskapi/servicedesk"): DESKS,
            ("get", "rest/servicedeskapi/organization"): page([{"id": 1, "name": "Finance"}]),
            ("post", "rest/servicedeskapi/organization"): {"id": 42, "name": "Payroll"},
        }
    )
    result = add_to_organization(DC, "Payroll", ["dave"], project="HR", create=True)
    assert result["created"] and result["organization"]["id"] == "42" and result["linked_project"] == "HR"
    posts = dict(fake.sent("post"))
    assert posts["rest/servicedeskapi/servicedesk/7/organization"]["data"] == {"organizationId": 42}
    assert posts["rest/servicedeskapi/organization/42/user"]["data"] == {"usernames": ["dave"]}


def test_add_to_organization_missing_without_create(rest):
    rest({("get", "rest/servicedeskapi/organization"): page([{"id": 1, "name": "Finance"}])})
    with pytest.raises(JiraError, match="pass --create"):
        add_to_organization(DC, "Payrol", ["dave"])


def _profile(groups, app_roles, active=True):
    return {
        "name": "bob",
        "displayName": "Bob",
        "active": active,
        "groups": {"items": [{"name": g} for g in groups]},
        "applicationRoles": {"items": [{"key": k} for k in app_roles]},
    }


def test_check_user_access_agent_via_group(rest):
    rest(_role_routes({("get", "rest/api/2/user"): _profile(["hr-agents"], ["jira-servicedesk"])}))
    result = check_user_access(DC, "bob", project="HR")
    assert result["project_roles"] == [{"role": "Service Desk Team", "via": "group:hr-agents"}]
    assert result["is_agent"] and not result["is_customer"] and result["findings"] == []


def test_check_user_access_flags_missing_licence(rest):
    fake = rest(_role_routes({("get", "rest/api/2/user"): _profile(["hr-agents"], ["jira-software"])}))
    result = check_user_access(DC, "bob", project="HR")
    assert not result["is_agent"]
    assert any("no Jira Service Management licence" in f for f in result["findings"])
    assert fake.calls[0][2]["params"] == {"username": "bob", "expand": "groups,applicationRoles"}


def test_check_user_access_no_project(rest):
    rest({("get", "rest/api/2/user"): _profile([], [], active=False)})
    result = check_user_access(DC, "bob")
    assert result["findings"] == ["account is inactive"] and "project_roles" not in result
