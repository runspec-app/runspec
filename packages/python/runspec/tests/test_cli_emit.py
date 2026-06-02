"""
Tests for _build_schema — the function that converts a runspec runnable
definition into an emitted tool schema (MCP / OpenAI / Anthropic).
"""

from __future__ import annotations

from runspec.cli import _build_schema


def _script(output=None, **kwargs):
    base = {"autonomy": "confirm", "args": {}}
    if output is not None:
        base["output"] = output
    base.update(kwargs)
    return base


# ── x-output field ────────────────────────────────────────────────────────────


def test_x_output_defaults_to_text():
    schema = _build_schema("deploy", _script(), "mcp")
    assert schema["x-output"] == "text"


def test_x_output_text_explicit():
    schema = _build_schema("deploy", _script(output="text"), "mcp")
    assert schema["x-output"] == "text"


def test_x_output_json():
    schema = _build_schema("process", _script(output="json"), "mcp")
    assert schema["x-output"] == "json"


def test_x_output_html():
    schema = _build_schema("report", _script(output="html"), "mcp")
    assert schema["x-output"] == "html"


def test_x_output_present_in_all_formats():
    for fmt in ("mcp", "openai", "anthropic"):
        schema = _build_schema("run", _script(output="json"), fmt)
        assert schema["x-output"] == "json", f"missing x-output in {fmt} format"


# ── other schema fields unaffected ────────────────────────────────────────────


def test_schema_fields_present():
    schema = _build_schema("deploy", _script(description="Deploy app", output="json"), "mcp")
    assert schema["name"] == "deploy"
    assert schema["description"] == "Deploy app"
    assert schema["x-autonomy"] == "confirm"
    assert schema["x-output"] == "json"
    assert "inputSchema" in schema


def test_x_autonomy_reason_present_when_set():
    script = _script(output="text", autonomy_reason="irreversible")
    script["autonomy_reason"] = "irreversible"
    schema = _build_schema("deploy", script, "mcp")
    assert schema.get("x-autonomy-reason") == "irreversible"


# ── meta pass-through (x-meta on arg properties) ─────────────────────────────


def test_x_meta_emitted_when_set():
    script = _script(args={"days": {"type": "int", "meta": {"unit": "days"}}})
    schema = _build_schema("clean", script, "mcp")
    prop = schema["inputSchema"]["properties"]["days"]
    assert prop["x-meta"] == {"unit": "days"}


def test_x_meta_omitted_when_absent():
    script = _script(args={"days": {"type": "int"}})
    schema = _build_schema("clean", script, "mcp")
    prop = schema["inputSchema"]["properties"]["days"]
    assert "x-meta" not in prop


def test_x_meta_preserves_full_dict():
    """The user's meta dict is passed through unchanged — not flattened."""
    meta = {"unit": "days", "impact": "destructive", "owner": "platform"}
    script = _script(args={"days": {"type": "int", "meta": meta}})
    schema = _build_schema("clean", script, "mcp")
    prop = schema["inputSchema"]["properties"]["days"]
    assert prop["x-meta"] == meta


# ── rest type emission ───────────────────────────────────────────────────────


def test_rest_type_emits_array_of_strings():
    script = _script(args={"extra": {"type": "rest"}})
    schema = _build_schema("wrap", script, "mcp")
    prop = schema["inputSchema"]["properties"]["extra"]
    assert prop == {"type": "array", "items": {"type": "string"}}


def test_rest_type_with_description_and_meta():
    script = _script(args={"extra": {"type": "rest", "description": "Pass-through", "meta": {"shape": "list"}}})
    schema = _build_schema("wrap", script, "mcp")
    prop = schema["inputSchema"]["properties"]["extra"]
    assert prop["type"] == "array"
    assert prop["items"] == {"type": "string"}
    assert prop["description"] == "Pass-through"
    assert prop["x-meta"] == {"shape": "list"}


# ── inferred types reach the schema (args often omit explicit `type`) ──────────


def test_inferred_types_emitted_not_defaulted_to_string():
    """Args that rely on inference (no explicit `type`) must emit the inferred
    JSON Schema type — not fall back to "string"."""
    script = _script(
        args={
            "label": {"description": "title"},  # no default → required str
            "format": {"options": ["text", "json"], "default": "text"},  # choice
            "threshold": {"default": 0.85},  # float → number
            "verbose": {"default": False},  # flag → boolean
            "limit": {"default": 10},  # int → integer
        }
    )
    props = _build_schema("sysreport", script, "anthropic")["inputSchema"]["properties"]
    assert props["label"]["type"] == "string"
    assert props["format"]["type"] == "string" and props["format"]["enum"] == ["text", "json"]
    assert props["threshold"]["type"] == "number"
    assert props["verbose"]["type"] == "boolean"
    assert props["limit"]["type"] == "integer"


def test_inferred_required_listed_in_schema():
    """An arg with no default is inferred required and must appear in the
    schema's `required` list, even though the raw spec never said so."""
    script = _script(args={"label": {"description": "title"}, "limit": {"default": 10}})
    schema = _build_schema("sysreport", script, "mcp")
    assert schema["inputSchema"]["required"] == ["label"]
