"""
Tests for runspec.schemacheck — the stdlib-only draft-2020-12 validity check that
lets `runspec serve` (and the runspec-mcp gateway) drop just a tool with a bad
schema instead of letting it 400 the whole published list.
"""

from __future__ import annotations

from runspec.schemacheck import drop_invalid_tools, schema_issue


def _obj(prop):
    return {"type": "object", "properties": {"x": prop}}


def test_valid_schema_has_no_issue():
    assert schema_issue({"type": "object", "properties": {}}) == ""
    assert schema_issue(_obj({"type": "integer", "minimum": 1, "maximum": 9})) == ""
    assert schema_issue(_obj({"type": "string", "enum": ["a", "b"]})) == ""
    assert schema_issue(_obj({"type": "array", "items": {"type": "string"}})) == ""


def test_string_numeric_bound_is_flagged():
    # The concrete draft-2020-12 rejection: minimum/maximum must be numbers.
    assert "minimum must be a number" in schema_issue(_obj({"type": "integer", "minimum": "9"}))
    assert "maximum must be a number" in schema_issue(_obj({"type": "integer", "maximum": "9"}))


def test_non_integer_length_is_flagged():
    assert "minLength must be a non-negative integer" in schema_issue(_obj({"type": "string", "minLength": "3"}))
    assert "maxItems must be a non-negative integer" in schema_issue(_obj({"type": "array", "maxItems": -1}))


def test_bad_pattern_is_flagged():
    assert "pattern is not a valid regex" in schema_issue(_obj({"type": "string", "pattern": "[a-z"}))
    # A valid pattern passes.
    assert schema_issue(_obj({"type": "string", "pattern": "[a-z][a-z0-9-]+"})) == ""


def test_duplicate_required_is_flagged():
    # draft 2020-12 requires unique `required` entries; the Anthropic API rejects a
    # duplicate (`["host","host"] has non-unique elements`) — the runnable-style
    # host/command-selector collision bug.
    assert "required has duplicate entries" in schema_issue({"type": "object", "properties": {"host": {"type": "string"}}, "required": ["host", "host"]})
    # a unique required list is fine
    assert schema_issue({"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "required": ["a", "b"]}) == ""


def test_tuple_items_is_flagged():
    # draft-04-style tuple `items` (an array) is invalid in 2020-12.
    assert "items must be a schema" in schema_issue(_obj({"type": "array", "items": [{"type": "string"}]}))


def test_bad_type_is_flagged():
    assert "is not a JSON Schema type" in schema_issue(_obj({"type": "choice"}))


def test_nested_property_issue_is_found():
    # A `multiple` arg wraps its property in {type: array, items: <prop>} — an issue
    # inside items must still be caught.
    assert "items.pattern is not a valid regex" in schema_issue(_obj({"type": "array", "items": {"type": "string", "pattern": "("}}))


def test_drop_invalid_tools_partitions():
    tools = {
        "good": {"inputSchema": _obj({"type": "integer", "minimum": 1, "maximum": 9})},
        "bad": {"inputSchema": _obj({"type": "integer", "minimum": "9"})},
        "noschema": {},  # tolerated — nothing to invalidate
    }
    keep, dropped = drop_invalid_tools(tools)
    assert set(keep) == {"good", "noschema"}
    assert len(dropped) == 1 and dropped[0][0] == "bad"
    assert "minimum must be a number" in dropped[0][1]
