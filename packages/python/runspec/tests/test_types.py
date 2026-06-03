"""
Tests for types.py — type registry and built-in Python coercers.
"""

from pathlib import Path

import pytest

from runspec.types import coerce, list_types, register_type


class TestBuiltinCoercers:
    def test_str_coercion(self):
        assert coerce("hello", {"type": "str", "name": "x"}) == "hello"

    def test_int_coercion(self):
        assert coerce("42", {"type": "int", "name": "x"}) == 42

    def test_int_coercion_from_int(self):
        assert coerce(42, {"type": "int", "name": "x"}) == 42

    def test_float_coercion(self):
        result = coerce("3.14", {"type": "float", "name": "x"})
        assert abs(result - 3.14) < 0.001

    def test_bool_true_variants(self):
        for val in ("true", "True", "TRUE", "1", "yes", "on"):
            assert coerce(val, {"type": "bool", "name": "x"}) is True

    def test_bool_false_variants(self):
        for val in ("false", "False", "FALSE", "0", "no", "off"):
            assert coerce(val, {"type": "bool", "name": "x"}) is False

    def test_flag_true(self):
        assert coerce(True, {"type": "flag", "name": "x"}) is True

    def test_flag_false(self):
        assert coerce(False, {"type": "flag", "name": "x"}) is False

    def test_path_coercion(self):
        result = coerce("/tmp", {"type": "path", "name": "x"})
        assert isinstance(result, Path)

    def test_choice_valid(self):
        spec = {"type": "choice", "name": "fmt", "options": ["json", "csv"]}
        assert coerce("json", spec) == "json"

    def test_choice_invalid(self):
        spec = {"type": "choice", "name": "fmt", "options": ["json", "csv"]}
        with pytest.raises(ValueError):
            coerce("xml", spec)

    def test_rest_passes_list_through(self):
        spec = {"type": "rest", "name": "extra"}
        assert coerce(["--foo", "bar"], spec) == ["--foo", "bar"]

    def test_rest_none_becomes_empty_list(self):
        spec = {"type": "rest", "name": "extra"}
        assert coerce(None, spec) == []

    def test_rest_coerces_items_to_strings(self):
        spec = {"type": "rest", "name": "extra"}
        assert coerce([1, "two", 3.5], spec) == ["1", "two", "3.5"]


class TestRangeValidation:
    def test_int_within_range(self):
        spec = {"type": "int", "name": "quality", "range": (1, 100)}
        assert coerce("85", spec) == 85

    def test_int_below_range(self):
        spec = {"type": "int", "name": "quality", "range": (1, 100)}
        with pytest.raises(ValueError):
            coerce("0", spec)

    def test_int_above_range(self):
        spec = {"type": "int", "name": "quality", "range": (1, 100)}
        with pytest.raises(ValueError):
            coerce("101", spec)

    def test_float_range(self):
        spec = {"type": "float", "name": "ratio", "range": (0.0, 1.0)}
        assert coerce("0.5", spec) == 0.5


class TestPatternValidation:
    def test_str_matches_pattern(self):
        spec = {"type": "str", "name": "jira-key", "pattern": "[A-Z]+-[0-9]+"}
        assert coerce("PROJ-123", spec) == "PROJ-123"

    def test_str_violates_pattern(self):
        spec = {"type": "str", "name": "jira-key", "pattern": "[A-Z]+-[0-9]+"}
        with pytest.raises(ValueError):
            coerce("proj-123", spec)

    def test_pattern_is_anchored_fullmatch(self):
        # A partial match must fail — pattern is fullmatch, not search.
        spec = {"type": "str", "name": "jira-key", "pattern": "[A-Z]+-[0-9]+"}
        with pytest.raises(ValueError):
            coerce("PROJ-123-extra", spec)

    def test_no_pattern_accepts_anything(self):
        spec = {"type": "str", "name": "free"}
        assert coerce("anything at all", spec) == "anything at all"


class TestLengthValidation:
    def test_within_bounds(self):
        spec = {"type": "str", "name": "slug", "min_length": 3, "max_length": 10}
        assert coerce("hello", spec) == "hello"

    def test_too_short(self):
        spec = {"type": "str", "name": "slug", "min_length": 3}
        with pytest.raises(ValueError):
            coerce("ab", spec)

    def test_too_long(self):
        spec = {"type": "str", "name": "slug", "max_length": 5}
        with pytest.raises(ValueError):
            coerce("abcdef", spec)

    def test_boundaries_inclusive(self):
        spec = {"type": "str", "name": "slug", "min_length": 3, "max_length": 5}
        assert coerce("abc", spec) == "abc"
        assert coerce("abcde", spec) == "abcde"


class TestMultipleCoercion:
    """multiple=true args coerce + validate each item, returning a list."""

    def test_returns_list_of_coerced_items(self):
        spec = {"type": "str", "name": "tag", "multiple": True}
        assert coerce(["a", "b", "c"], spec) == ["a", "b", "c"]

    def test_int_items_coerced_per_item(self):
        spec = {"type": "int", "name": "n", "multiple": True}
        assert coerce(["1", "2", "3"], spec) == [1, 2, 3]

    def test_pattern_applied_per_item(self):
        spec = {"type": "str", "name": "tag", "multiple": True, "pattern": "[a-z]+"}
        assert coerce(["ab", "cd"], spec) == ["ab", "cd"]

    def test_range_applied_per_item(self):
        spec = {"type": "int", "name": "n", "multiple": True, "range": [1, 10]}
        assert coerce(["1", "5", "9"], spec) == [1, 5, 9]

    def test_scalar_value_wrapped_to_single_item_list(self):
        spec = {"type": "str", "name": "tag", "multiple": True}
        assert coerce("solo", spec) == ["solo"]

    def test_collects_all_failing_items_with_index_and_value(self):
        spec = {"type": "str", "name": "tag", "multiple": True, "pattern": "[a-z]+"}
        with pytest.raises(ValueError) as exc:
            coerce(["ab", "XY", "z9", "de"], spec)
        msg = str(exc.value)
        assert "2 of 4 item(s) failed" in msg
        assert "item 2 ('XY')" in msg
        assert "item 3 ('z9')" in msg

    def test_per_item_length_failure(self):
        spec = {"type": "str", "name": "code", "multiple": True, "min_length": 2}
        with pytest.raises(ValueError) as exc:
            coerce(["ok", "x", "fine"], spec)
        assert "item 2 ('x')" in str(exc.value)

    def test_rest_type_not_treated_as_per_item(self):
        # rest manages its own list and is excluded from per-item coercion.
        spec = {"type": "rest", "name": "extra"}
        assert coerce(["--flag", "value"], spec) == ["--flag", "value"]


class TestCustomTypes:
    def test_register_and_use_custom_type(self):
        register_type("upper-str", lambda v, arg: str(v).upper())
        result = coerce("hello", {"type": "upper-str", "name": "x"})
        assert result == "HELLO"

    def test_unknown_type_raises(self):
        with pytest.raises(TypeError, match="Unknown type"):
            coerce("value", {"type": "nonexistent-type-xyz", "name": "x"})

    def test_list_types_includes_builtins(self):
        types = list_types()
        for t in ("str", "int", "float", "bool", "flag", "path", "choice"):
            assert t in types
