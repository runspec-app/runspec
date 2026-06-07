import pytest

from runspec_snow.client import SnowError
from runspec_snow.states import resolve_state_value

_STATES = [
    {"label": "New", "value": "1", "sequence": "0"},
    {"label": "In Progress", "value": "2", "sequence": "1"},
    {"label": "Resolved", "value": "6", "sequence": "2"},
    {"label": "Closed", "value": "7", "sequence": "3"},
]


def test_resolve_by_exact_value():
    assert resolve_state_value(_STATES, "6") == "6"


def test_resolve_by_label_case_insensitive():
    assert resolve_state_value(_STATES, "in progress") == "2"
    assert resolve_state_value(_STATES, "Resolved") == "6"


def test_value_match_takes_precedence_over_label():
    # "7" is a value (Closed); make sure a numeric input resolves by value.
    assert resolve_state_value(_STATES, "7") == "7"


def test_unknown_state_raises_with_available():
    with pytest.raises(SnowError, match="not available") as exc:
        resolve_state_value(_STATES, "Cancelled")
    assert "Closed (value 7)" in str(exc.value)
