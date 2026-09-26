"""Request-type signatures, value coercion, one-call create/update, and codegen."""

import json

import pytest
import tomllib

from runspec_jira_core import (
    JiraConfig,
    JiraError,
    RequestTypeSig,
    build_field_values,
    create_request,
    describe_request_types,
    manifest_arg_names,
    manifest_signatures,
    run_generated_command,
    update_request,
)
from runspec_jira_core.codegen import build_manifest, generate, render_commands, splice
from runspec_jira_core.requesttypes import build_signature
from tests.rest_fake import page

DC = JiraConfig(url="https://jira.corp", cloud=False, personal_token="pat")
DESK = {"id": "7", "projectId": "100", "projectKey": "HR", "projectName": "Human Resources"}
RTS = page(
    [
        {"id": "45", "name": "New starter", "description": "Onboard a <b>new</b> colleague"},
        {"id": "46", "name": "Payslip query", "description": ""},
    ]
)
FIELDS_45 = {
    "canRaiseOnBehalfOf": True,
    "requestTypeFields": [
        {"fieldId": "summary", "name": "Summary", "required": True, "jiraSchema": {"type": "string", "system": "summary"}, "validValues": []},
        {
            "fieldId": "customfield_100",
            "name": "Office",
            "required": True,
            "jiraSchema": {"type": "option", "custom": "com.atlassian.jira.plugin.system.customfieldtypes:select"},
            "validValues": [{"value": "1", "label": "London"}, {"value": "2", "label": "Leeds"}],
        },
        {"fieldId": "customfield_101", "name": "Start date", "required": False, "jiraSchema": {"type": "date"}, "validValues": []},
        {
            "fieldId": "customfield_102",
            "name": "Equipment",
            "required": False,
            "jiraSchema": {"type": "array", "items": "option"},
            "validValues": [{"value": "10", "label": "Laptop"}, {"value": "11", "label": "Monitor"}],
        },
        {
            "fieldId": "customfield_103",
            "name": "Location",
            "required": False,
            "jiraSchema": {"type": "option-with-child"},
            "validValues": [{"value": "20", "label": "UK", "children": [{"value": "21", "label": "Leeds"}]}],
        },
        {"fieldId": "customfield_104", "name": "Line manager", "required": False, "jiraSchema": {"type": "user"}, "validValues": []},
        {"fieldId": "customfield_105", "name": "Env", "required": False, "jiraSchema": {"type": "string"}, "validValues": []},
        {"fieldId": "attachment", "name": "Attachment", "required": False, "jiraSchema": {"type": "array", "items": "attachment"}, "validValues": []},
    ],
}
FIELDS_46 = {"requestTypeFields": [{"fieldId": "summary", "name": "Summary", "required": True, "jiraSchema": {"type": "string"}}]}


def _routes(extra=None):
    routes = {
        ("get", "rest/servicedeskapi/servicedesk"): page([DESK]),
        ("get", "rest/servicedeskapi/servicedesk/7/requesttype"): RTS,
        ("get", "rest/servicedeskapi/servicedesk/7/requesttype/45/field"): FIELDS_45,
        ("get", "rest/servicedeskapi/servicedesk/7/requesttype/46/field"): FIELDS_46,
        ("post", "rest/servicedeskapi/request"): {"issueKey": "HR-9", "issueId": "900", "_links": {"web": "https://jira.corp/servicedesk/customer/portal/7/HR-9"}},
    }
    routes.update(extra or {})
    return routes


def _sig() -> RequestTypeSig:
    return build_signature(DESK, RTS["values"][0], FIELDS_45)


# ── signatures ───────────────────────────────────────────────────────────────


def test_build_signature_kinds_names_and_reserved():
    sig = _sig()
    by_name = {f.name: f for f in sig.fields}
    assert sig.slug == "new-starter" and sig.description == "Onboard a new colleague"
    assert {n: f.kind for n, f in by_name.items()} == {
        "summary": "str",
        "office": "select",
        "start-date": "date",
        "equipment": "multiselect",
        "location": "cascade",
        "line-manager": "user",
        "env-field": "str",  # "env" collides with the runnable's --env
        "attachment": "attachment",
    }
    assert [o.label for o in by_name["office"].options] == ["London", "Leeds"]


def test_signature_round_trips_through_dict():
    sig = _sig()
    assert RequestTypeSig.from_dict(json.loads(json.dumps(sig.to_dict()))) == sig


def test_json_schema_has_enums_and_required():
    schema = _sig().json_schema()
    assert schema["required"] == ["summary", "office"]
    assert schema["properties"]["office"]["enum"] == ["London", "Leeds"]
    assert schema["properties"]["equipment"]["type"] == "array"
    assert "attachment" not in schema["properties"]


def test_describe_only_fetches_fields_for_matching_types(rest):
    fake = rest(_routes())
    sigs = describe_request_types(DC, ["Human Resources"], request_types=["new*"])
    assert [s.name for s in sigs] == ["New starter"]
    fetched = [p for v, p, _ in fake.calls if p.endswith("/field")]
    assert fetched == ["rest/servicedeskapi/servicedesk/7/requesttype/45/field"]


# ── coercion ─────────────────────────────────────────────────────────────────


def test_build_field_values_resolves_names_labels_and_shapes():
    values = build_field_values(
        _sig(),
        {
            "summary": "Welcome Sam",
            "Office": "leeds",  # label key, case-insensitive option
            "start-date": "2026-10-01T09:00:00",
            "equipment": "Laptop, Monitor",
            "location": "UK > Leeds",
            "Line manager": "alice",
            "env-field": "prod",
            "customfield_999": "raw",  # hidden field escape hatch
            "customfield_101": None,  # empty values dropped
        },
    )
    assert values == {
        "summary": "Welcome Sam",
        "customfield_100": {"id": "2"},
        "customfield_101": "2026-10-01",
        "customfield_102": [{"id": "10"}, {"id": "11"}],
        "customfield_103": {"id": "20", "child": {"id": "21"}},
        "customfield_104": {"name": "alice"},
        "customfield_105": "prod",
        "customfield_999": "raw",
    }


def test_cloud_users_use_account_id():
    assert build_field_values(_sig(), {"summary": "x", "office": "1", "line-manager": "acc-1"}, cloud=True)["customfield_104"] == {"accountId": "acc-1"}


def test_bad_option_error_carries_suggestion_and_signature():
    with pytest.raises(JiraError) as err:
        build_field_values(_sig(), {"summary": "x", "office": "Leds"})
    message = str(err.value)
    assert "did you mean 'Leeds'" in message
    assert "office ('Office'): select, required, one of London|Leeds" in message


def test_unknown_field_and_missing_required():
    with pytest.raises(JiraError, match="Unknown field 'ofice'.*did you mean 'office'"):
        build_field_values(_sig(), {"summary": "x", "ofice": "Leeds"})
    with pytest.raises(JiraError, match="Missing required field.*office"):
        build_field_values(_sig(), {"summary": "x"})
    assert build_field_values(_sig(), {"office": "Leeds"}, require_all=False) == {"customfield_100": {"id": "2"}}


def test_attachment_field_is_refused():
    with pytest.raises(JiraError, match="add-attachment"):
        build_field_values(_sig(), {"summary": "x", "office": "Leeds", "attachment": "/tmp/f"})


# ── one-call create / update ─────────────────────────────────────────────────


def test_create_request_by_name_posts_once(rest):
    fake = rest(_routes())
    result = create_request(DC, "hr", "New starter", '{"summary": "Welcome Sam", "office": "London"}', on_behalf_of="sam")
    assert result == {"issueKey": "HR-9", "issueId": "900", "project": "HR", "requestType": "New starter", "web": "https://jira.corp/servicedesk/customer/portal/7/HR-9"}
    (path, kw), *rest_posts = fake.sent("post")
    assert not rest_posts and path == "rest/servicedeskapi/request"
    assert kw["data"] == {
        "serviceDeskId": "7",
        "requestTypeId": "45",
        "requestFieldValues": {"summary": "Welcome Sam", "customfield_100": {"id": "1"}},
        "raiseOnBehalfOf": "sam",
    }
    assert not any(p.endswith("46/field") for _, p, _ in fake.calls)


def test_create_request_unknown_type_suggests(rest):
    rest(_routes())
    with pytest.raises(JiraError, match="did you mean 'New starter'"):
        create_request(DC, "HR", "New startr", {"summary": "x"})


def test_create_request_with_signatures_makes_no_lookups(rest):
    fake = rest(_routes())
    create_request(DC, "HR", "new-starter", {"summary": "x", "office": "Leeds"}, signatures=[_sig()])
    assert [v for v, _, _ in fake.calls] == ["post"]


def test_update_request_resolves_type_and_puts(rest):
    fake = rest(_routes({("get", "rest/servicedeskapi/request/HR-9"): {"issueKey": "HR-9", "requestTypeId": "45", "serviceDeskId": "7"}}))
    result = update_request(DC, "HR-9", {"Office": "Leeds", "start-date": "2026-11-02"})
    assert result == {"issueKey": "HR-9", "requestType": "New starter", "updated": ["customfield_100", "customfield_101"]}
    assert fake.sent("put") == [("rest/api/2/issue/HR-9", {"data": {"fields": {"customfield_100": {"id": "2"}, "customfield_101": "2026-11-02"}}})]


def test_update_request_rejects_non_request(rest):
    rest({("get", "rest/servicedeskapi/request/X-1"): {}})
    with pytest.raises(JiraError, match="not a service desk request"):
        update_request(DC, "X-1", {"summary": "x"})


# ── codegen ──────────────────────────────────────────────────────────────────


def _parsed(block: str) -> dict:
    return tomllib.loads(block)["dept"]["commands"]


def test_render_commands_is_valid_toml_with_typed_args():
    sig46 = build_signature(DESK, RTS["values"][1], FIELDS_46)
    commands = _parsed(render_commands("dept", [_sig(), sig46]))
    assert set(commands) == {"raise-hr-new-starter", "raise-hr-payslip-query"}
    verb = commands["raise-hr-new-starter"]
    assert verb["autonomy"] == "confirm"
    assert verb["meta"] == {"jsm": {"project": "HR", "service_desk_id": "7", "request_type_id": "45"}}
    args = verb["args"]
    assert args["office"] == {"type": "choice", "options": ["London", "Leeds"], "required": True, "description": "Office"}
    assert args["equipment"]["multiple"] is True and args["equipment"]["type"] == "choice"
    assert args["start-date"]["ui"] == "date"
    assert "attachment" not in args and "on-behalf-of" in args
    assert "on-behalf-of" not in commands["raise-hr-payslip-query"]["args"]


def test_render_commands_large_option_lists_become_free_text():
    args = _parsed(render_commands("dept", [_sig()], max_options=1))["raise-hr-new-starter"]["args"]
    assert args["office"]["type"] == "str" and "one of: London, Leeds" in args["office"]["description"]


def test_splice_replaces_only_the_generated_block():
    hand = '[dept]\ndescription = "x"\n'
    first = splice(hand, render_commands("dept", [_sig()]), "dept")
    assert first.startswith(hand)
    again = splice(first + '\n[dept.commands.mine]\ndescription = "kept"\n', render_commands("dept", []), "dept")
    assert "raise-hr-new-starter" not in again and "kept" in again and again.startswith(hand)


def test_generate_writes_then_check_is_clean(rest, tmp_path):
    rest(_routes())
    toml_path, manifest_path = tmp_path / "runspec.toml", tmp_path / "m.json"
    toml_path.write_text('[dept]\ndescription = "Dept JSM"\n')
    kw = {"runnable": "dept", "projects": ["HR"], "toml_path": toml_path, "manifest_path": manifest_path}
    first = generate(DC, **kw)
    assert first["verbs"] == ["raise-hr-new-starter", "raise-hr-payslip-query"] and len(first["written"]) == 2
    assert generate(DC, check=True, **kw)["stale"] == []
    manifest_path.write_text("{}")
    assert generate(DC, check=True, **kw)["stale"] == [str(manifest_path)]


def test_run_generated_command_same_instance_posts_directly(rest):
    fake = rest(_routes())
    manifest = build_manifest("dept", [_sig()], url="https://jira.corp")
    assert manifest_arg_names(manifest, "raise-hr-new-starter")[:2] == ["summary", "office"]
    assert manifest_signatures(manifest) == [_sig()]
    run_generated_command(DC, manifest, "raise-hr-new-starter", {"summary": "x", "office": "Leeds", "equipment": []})
    assert [v for v, _, _ in fake.calls] == ["post"]


def test_run_generated_command_other_instance_resolves_by_name(rest):
    fake = rest(_routes())
    manifest = build_manifest("dept", [_sig()], url="https://jira-uat.corp")
    run_generated_command(DC, manifest, "raise-hr-new-starter", {"summary": "x", "office": "Leeds"})
    assert ("get", "rest/servicedeskapi/servicedesk/7/requesttype") in [(v, p) for v, p, _ in fake.calls]
