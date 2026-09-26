"""New verbs (request types by name, project admin) and generated department verbs.

The core's raw-REST helpers call the SDK's generic get/post/put/delete, so the
fake here routes on (verb, path). The codegen test renders a department block
and parses it with the real runspec parser — proof that what the generator
emits is a valid, validating runspec.toml.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from runspec.testing import ParseHarness
from runspec_jira_core import JiraConfig, manifest_arg_names, run_generated_command
from runspec_jira_core import client as core_client
from runspec_jira_core.codegen import build_manifest, render_commands
from runspec_jira_core.requesttypes import build_signature, clear_signature_cache

from runspec_jira import cli

SPEC = Path(__file__).parent.parent / "runspec_jira" / "runspec.toml"
DC = JiraConfig(url="https://jira.corp", cloud=False, personal_token="pat")
DESK = {"id": "7", "projectId": "100", "projectKey": "HR", "projectName": "Human Resources"}
RT = {"id": "45", "name": "New starter"}
FIELDS = {
    "canRaiseOnBehalfOf": True,
    "requestTypeFields": [
        {"fieldId": "summary", "name": "Summary", "required": True, "jiraSchema": {"type": "string"}},
        {"fieldId": "customfield_100", "name": "Office", "required": True, "jiraSchema": {"type": "option"}, "validValues": [{"value": "1", "label": "London"}, {"value": "2", "label": "Leeds"}]},
        {"fieldId": "customfield_101", "name": "Start date", "required": False, "jiraSchema": {"type": "date"}},
        {
            "fieldId": "customfield_102",
            "name": "Equipment",
            "required": False,
            "jiraSchema": {"type": "array", "items": "option"},
            "validValues": [{"value": "10", "label": "Laptop"}, {"value": "11", "label": "Monitor"}],
        },
    ],
}


def _page(values: list[Any]) -> dict[str, Any]:
    return {"values": values, "isLastPage": True}


class Rest:
    experimental_headers: dict[str, str] = {}

    def __init__(self, routes: dict[tuple[str, str], Any]):
        self.routes, self.calls = routes, []

    def _call(self, verb: str, path: str, **kw: Any) -> Any:
        self.calls.append((verb, path, kw))
        return self.routes.get((verb, path), {})

    def get(self, path: str, **kw: Any) -> Any:
        return self._call("get", path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self._call("post", path, **kw)

    def put(self, path: str, **kw: Any) -> Any:
        return self._call("put", path, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self._call("delete", path, **kw)


ROUTES = {
    ("get", "rest/servicedeskapi/servicedesk"): _page([DESK]),
    ("get", "rest/servicedeskapi/servicedesk/7/requesttype"): _page([RT]),
    ("get", "rest/servicedeskapi/servicedesk/7/requesttype/45/field"): FIELDS,
    ("post", "rest/servicedeskapi/request"): {"issueKey": "HR-9"},
    ("get", "rest/api/2/project/HR/role"): {"Service Desk Team": "https://jira.corp/rest/api/2/project/HR/role/10100"},
    ("get", "rest/api/2/project/HR/role/10100"): {"name": "Service Desk Team", "actors": []},
}


@pytest.fixture
def rest(monkeypatch) -> Rest:
    fake = Rest(dict(ROUTES))
    monkeypatch.setattr(core_client, "Jira", lambda **kw: fake)
    monkeypatch.setattr(core_client, "ServiceDesk", lambda **kw: fake)
    clear_signature_cache()
    return fake


def _run(argv: list[str]) -> Any:
    h = ParseHarness(script_name="jira", config_path=SPEC)
    spec = h.expect_ok(argv)
    try:
        return cli._dispatch(spec, DC)
    finally:
        h.close()


def test_create_request_verb_raises_by_name(rest) -> None:
    result = _run(["create-request", "--project", "HR", "--request-type", "new starter", "--values", '{"Summary": "Welcome", "Office": "Leeds"}'])
    assert result["issueKey"] == "HR-9"
    posted = [kw["data"] for verb, path, kw in rest.calls if verb == "post"]
    assert posted[0]["requestFieldValues"] == {"summary": "Welcome", "customfield_100": {"id": "2"}}


def test_describe_request_types_verb_returns_dicts(rest) -> None:
    result = _run(["describe-request-types", "--project", "HR"])
    assert result[0]["name"] == "New starter" and result[0]["fields"][1]["name"] == "office"


def test_add_to_role_verb_repeats_users(rest) -> None:
    result = _run(["add-to-role", "--project", "HR", "--role", "service desk team", "--user", "a", "--user", "b"])
    assert result["added_users"] == ["a", "b"]
    assert ("post", "rest/api/2/project/HR/role/10100", {"data": {"user": ["a", "b"]}}) in rest.calls


def test_check_user_access_verb(rest) -> None:
    rest.routes[("get", "rest/api/2/user")] = {"active": True, "groups": {"items": []}, "applicationRoles": {"items": [{"key": "jira-servicedesk"}]}}
    result = _run(["check-user-access", "--user", "bob", "--project", "HR"])
    assert result["jsm_licensed"] and not result["is_agent"]
    assert any("not in the 'Service Desk Team' role" in f for f in result["findings"])


# ── generated department verbs parse + validate under the real runspec ─────────


def _dept_spec(tmp_path: Path) -> Path:
    block = render_commands("dept", [build_signature(DESK, RT, FIELDS)])
    path = tmp_path / "runspec.toml"
    path.write_text('[config]\nautonomy-default = "confirm"\n\n[dept]\ndescription = "Department JSM"\noutput = "json"\nrequire-command = true\n\n' + block)
    return path


def test_generated_verbs_parse_and_validate(tmp_path: Path) -> None:
    h = ParseHarness(script_name="dept", config_path=_dept_spec(tmp_path))
    argv = ["raise-hr-new-starter", "--summary", "Welcome", "--office", "Leeds", "--equipment", "Laptop", "--equipment", "Monitor", "--start-date", "2026-10-01"]
    spec = h.expect_ok(argv)
    assert spec.office.value == "Leeds" and spec.equipment.value == ["Laptop", "Monitor"]
    h.expect_exit(["raise-hr-new-starter", "--summary", "x", "--office", "Paris"], contains="office")
    h.expect_exit(["raise-hr-new-starter", "--summary", "x"], contains="office")
    h.close()


def test_generated_verb_dispatch_posts_once(tmp_path: Path, rest) -> None:
    manifest = build_manifest("dept", [build_signature(DESK, RT, FIELDS)], url=DC.url)
    h = ParseHarness(script_name="dept", config_path=_dept_spec(tmp_path))
    spec = h.expect_ok(["raise-hr-new-starter", "--summary", "Welcome", "--office", "London"])
    values = {name: spec.get(name).value for name in manifest_arg_names(manifest, spec.runspec_command)}
    h.close()
    run_generated_command(DC, manifest, spec.runspec_command, values, on_behalf_of=spec.get("on-behalf-of").value)
    assert [v for v, _, _ in rest.calls] == ["post"]
    assert rest.calls[0][2]["data"]["requestFieldValues"] == {"summary": "Welcome", "customfield_100": {"id": "1"}}
