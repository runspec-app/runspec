import pytest

from runspec_snow._cli import flag, opt, parse_field_pairs
from runspec_snow.client import SnowError


class _Arg:
    """Minimal stand-in for a runspec Arg wrapper."""

    def __init__(self, value):
        self.value = value


def test_opt_returns_none_for_unset():
    assert opt(_Arg(None)) is None
    assert opt(_Arg("")) is None


def test_opt_returns_string():
    assert opt(_Arg("hello")) == "hello"
    assert opt(_Arg(42)) == "42"


def test_flag():
    assert flag(_Arg(True)) is True
    assert flag(_Arg(False)) is False
    assert flag(_Arg(None)) is False


def test_parse_field_pairs_empty():
    assert parse_field_pairs(_Arg(None)) == {}
    assert parse_field_pairs(_Arg([])) == {}


def test_parse_field_pairs_splits_on_first_equals():
    result = parse_field_pairs(_Arg(["short_description=disk full", "urgency=1", "note=a=b"]))
    assert result == {"short_description": "disk full", "urgency": "1", "note": "a=b"}


def test_parse_field_pairs_rejects_missing_equals():
    with pytest.raises(SnowError, match="expected field=value"):
        parse_field_pairs(_Arg(["bad_token"]))
