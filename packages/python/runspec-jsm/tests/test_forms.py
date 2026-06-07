import pytest

from runspec_jsm.client import JsmError
from runspec_jsm.forms import resolve_request_fields

FIELDS = {
    "requestTypeFields": [
        {"fieldId": "summary", "name": "Summary", "required": True},
        {"fieldId": "description", "name": "Description", "required": False},
        {"fieldId": "customfield_10010", "name": "Components", "required": False},
    ]
}


def test_resolve_by_name():
    out = resolve_request_fields(FIELDS, {"Summary": "Printer down", "Components": ["c1"]})
    assert out == {"summary": "Printer down", "customfield_10010": ["c1"]}


def test_resolve_by_field_id():
    out = resolve_request_fields(FIELDS, {"customfield_10010": ["c1"], "summary": "x"})
    assert out == {"customfield_10010": ["c1"], "summary": "x"}


def test_resolve_accepts_bare_list():
    out = resolve_request_fields(FIELDS["requestTypeFields"], {"summary": "x"})
    assert out == {"summary": "x"}


def test_name_matching_is_case_insensitive():
    out = resolve_request_fields(FIELDS, {"description": "d", "summary": "s"})
    assert out == {"description": "d", "summary": "s"}


def test_unknown_field_raises_with_available():
    with pytest.raises(JsmError, match="Unknown request field"):
        resolve_request_fields(FIELDS, {"summary": "s", "Priority": "High"})


def test_missing_required_field_raises():
    with pytest.raises(JsmError, match="Missing required field.*Summary"):
        resolve_request_fields(FIELDS, {"Description": "d"})
