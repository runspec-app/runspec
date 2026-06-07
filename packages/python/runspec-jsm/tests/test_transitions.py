import pytest

from runspec_jsm.client import JsmError
from runspec_jsm.transitions import resolve_transition_id

PAYLOAD = {
    "transitions": [
        {"id": "11", "name": "Start Progress", "to": {"name": "In Progress"}},
        {"id": "21", "name": "Resolve Issue", "to": {"name": "Resolved"}},
    ]
}


def test_resolve_by_id():
    assert resolve_transition_id(PAYLOAD, "21") == "21"


def test_resolve_by_name_case_insensitive():
    assert resolve_transition_id(PAYLOAD, "start progress") == "11"


def test_resolve_accepts_bare_list():
    assert resolve_transition_id(PAYLOAD["transitions"], "Resolve Issue") == "21"


def test_id_match_takes_precedence_over_name():
    # A transition literally named "11" must not shadow the real id 11.
    payload = {"transitions": [{"id": "11", "name": "Start"}, {"id": "99", "name": "11"}]}
    assert resolve_transition_id(payload, "11") == "11"


def test_unknown_transition_lists_available():
    with pytest.raises(JsmError, match="Start Progress .id 11."):
        resolve_transition_id(PAYLOAD, "Close")
