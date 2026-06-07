"""Tests for the Arg-wrapper-aware CLI helpers.

A runspec ``Arg`` is not a bare value: an unset optional arg has ``.value =
None`` but is itself truthy and stringifies to ``"None"``. These tests use a
minimal stand-in to lock that handling in.
"""

import pytest

from runspec_jsm._cli import flag, opt, parse_field_pairs
from runspec_jsm.client import JsmError


class FakeArg:
    """Mimics a runspec Arg: holds a value, truthy, str() -> str(value)."""

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return str(self.value)

    def __bool__(self):
        return True  # the wrapper object is always truthy, like the real Arg


def test_opt_unset_arg_is_none():
    assert opt(FakeArg(None)) is None


def test_opt_set_arg_returns_string():
    assert opt(FakeArg("uat")) == "uat"


def test_opt_handles_bare_none():
    assert opt(None) is None


def test_flag_reads_underlying_value():
    assert flag(FakeArg(False)) is False
    assert flag(FakeArg(True)) is True


def test_parse_field_pairs_unset():
    assert parse_field_pairs(FakeArg(None)) == {}


def test_parse_field_pairs_splits_once():
    assert parse_field_pairs(FakeArg(["a=1", "b=x=y"])) == {"a": "1", "b": "x=y"}


def test_parse_field_pairs_rejects_missing_equals():
    with pytest.raises(JsmError, match="expected fieldId=value"):
        parse_field_pairs(FakeArg(["bad"]))
