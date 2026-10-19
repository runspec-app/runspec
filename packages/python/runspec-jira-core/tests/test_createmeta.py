"""Create metadata on every Jira version: the paged ``issue/createmeta/{project}/…``
endpoints (Data Center 8.4+, required from 9.0 where the old one was removed, and
Cloud), with a fallback to the old ``issue/createmeta?projectKeys=…`` on a 404."""

from __future__ import annotations

from typing import Any

import pytest
import requests

from runspec_jira_core import JiraConfig, JiraError, get_create_fields, get_project_issue_types

DC = JiraConfig(url="https://jira.corp", cloud=False, personal_token="pat")
CLOUD = JiraConfig(url="https://x.atlassian.net", cloud=True, username="u@x", api_token="t")

META = "rest/api/2/issue/createmeta"
BUG = {"id": "1", "name": "Bug", "subtask": False}
TASK = {"id": "3", "name": "Task", "subtask": False}
SUMMARY = {"fieldId": "summary", "name": "Summary", "required": True, "schema": {"type": "string"}}
PRIORITY = {"fieldId": "priority", "name": "Priority", "required": False, "allowedValues": [{"name": "High"}]}


class _Resp:
    def __init__(self, status: int):
        self.status_code = status
        self.text = "not found"

    def json(self) -> Any:
        return {"errorMessages": ["Not found"]}


def _404(path: str, kwargs: dict[str, Any]) -> Any:
    raise requests.HTTPError("404", response=_Resp(404))  # type: ignore[arg-type]


def _dc_page(values: list[Any], *, start: int = 0, total: int | None = None) -> dict[str, Any]:
    total = len(values) if total is None else total
    return {"startAt": start, "maxResults": 50, "total": total, "isLast": start + len(values) >= total, "values": values}


# ── new endpoints (Data Center 9+, Cloud) ────────────────────────────────────


def test_issue_types_uses_new_endpoint_and_returns_a_list(rest) -> None:
    fake = rest({("get", f"{META}/PROJ/issuetypes"): _dc_page([BUG, TASK])})
    assert get_project_issue_types(DC, "PROJ") == [BUG, TASK]
    assert [p for p, _ in fake.sent("get")] == [f"{META}/PROJ/issuetypes"]


def test_issue_types_reads_cloud_page_key(rest) -> None:
    rest({("get", f"{META}/PROJ/issuetypes"): {"startAt": 0, "maxResults": 50, "total": 1, "issueTypes": [BUG]}})
    assert get_project_issue_types(CLOUD, "PROJ") == [BUG]


def test_issue_types_follows_pages(rest) -> None:
    def pages(path: str, kwargs: dict[str, Any]) -> Any:
        start = kwargs["params"]["startAt"]
        return _dc_page([BUG], start=0, total=2) if start == 0 else _dc_page([TASK], start=1, total=2)

    fake = rest({("get", f"{META}/PROJ/issuetypes"): pages})
    assert get_project_issue_types(DC, "PROJ") == [BUG, TASK]
    assert [kw["params"]["startAt"] for _, kw in fake.sent("get")] == [0, 1]


def test_create_fields_builds_classic_shape_from_new_endpoints(rest) -> None:
    fake = rest(
        {
            ("get", f"{META}/PROJ/issuetypes"): _dc_page([BUG, TASK]),
            ("get", f"{META}/PROJ/issuetypes/1"): _dc_page([SUMMARY, PRIORITY]),
            ("get", f"{META}/PROJ/issuetypes/3"): _dc_page([SUMMARY]),
        }
    )
    result = get_create_fields(DC, "PROJ")
    (project,) = result["projects"]
    assert project["key"] == "PROJ"
    bug, task = project["issuetypes"]
    assert bug["name"] == "Bug" and set(bug["fields"]) == {"summary", "priority"}
    assert bug["fields"]["priority"]["allowedValues"] == [{"name": "High"}]
    assert list(task["fields"]) == ["summary"]
    assert META not in [p for p, _ in fake.sent("get")]  # the removed endpoint is never called


def test_create_fields_one_issue_type_is_one_fields_request(rest) -> None:
    fake = rest(
        {
            ("get", f"{META}/PROJ/issuetypes"): _dc_page([BUG, TASK]),
            ("get", f"{META}/PROJ/issuetypes/3"): {"startAt": 0, "maxResults": 50, "total": 1, "fields": [SUMMARY]},
        }
    )
    result = get_create_fields(CLOUD, "PROJ", issue_type="task")
    assert [t["name"] for t in result["projects"][0]["issuetypes"]] == ["Task"]
    assert f"{META}/PROJ/issuetypes/1" not in [p for p, _ in fake.sent("get")]


def test_create_fields_unknown_issue_type_lists_the_choices(rest) -> None:
    rest({("get", f"{META}/PROJ/issuetypes"): _dc_page([BUG, TASK])})
    with pytest.raises(JiraError, match="issue types: Bug, Task"):
        get_create_fields(DC, "PROJ", issue_type="Epic")


# ── old endpoint (Jira before 8.4) ───────────────────────────────────────────


def _legacy(path: str, kwargs: dict[str, Any]) -> Any:
    bug = {**BUG, "fields": {"summary": SUMMARY}} if "expand" in kwargs["params"] else BUG
    task = {**TASK, "fields": {"summary": SUMMARY}} if "expand" in kwargs["params"] else TASK
    return {"projects": [{"id": "10000", "key": "PROJ", "issuetypes": [bug, task]}]}


def test_issue_types_falls_back_to_old_endpoint_on_404(rest) -> None:
    fake = rest({("get", f"{META}/PROJ/issuetypes"): _404, ("get", META): _legacy})
    assert get_project_issue_types(DC, "PROJ") == [BUG, TASK]
    assert fake.sent("get")[-1][1]["params"] == {"projectKeys": "PROJ"}


def test_create_fields_falls_back_to_old_endpoint_and_filters(rest) -> None:
    fake = rest({("get", f"{META}/PROJ/issuetypes"): _404, ("get", META): _legacy})
    result = get_create_fields(DC, "PROJ", issue_type="1")
    (project,) = result["projects"]
    assert [t["name"] for t in project["issuetypes"]] == ["Bug"]
    assert project["issuetypes"][0]["fields"] == {"summary": SUMMARY}
    assert fake.sent("get")[-1][1]["params"]["expand"] == "projects.issuetypes.fields"


def test_old_endpoint_empty_projects_is_a_clear_error(rest) -> None:
    rest({("get", f"{META}/NOPE/issuetypes"): _404, ("get", META): {"projects": []}})
    with pytest.raises(JiraError, match="'NOPE' not found"):
        get_project_issue_types(CLOUD, "NOPE")


def test_both_endpoints_404_reports_the_new_endpoint_error(rest) -> None:
    rest({("get", f"{META}/NOPE/issuetypes"): _404, ("get", META): _404})
    with pytest.raises(JiraError, match="Jira 404: Not found"):
        get_create_fields(DC, "NOPE")


def test_non_404_errors_do_not_fall_back(rest) -> None:
    def forbidden(path: str, kwargs: dict[str, Any]) -> Any:
        raise requests.HTTPError("403", response=_Resp(403))  # type: ignore[arg-type]

    fake = rest({("get", f"{META}/PROJ/issuetypes"): forbidden})
    with pytest.raises(JiraError):
        get_project_issue_types(DC, "PROJ")
    assert len(fake.sent("get")) == 1
