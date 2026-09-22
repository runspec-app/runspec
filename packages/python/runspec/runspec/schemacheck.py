"""
schemacheck.py — a tiny, stdlib-only JSON Schema draft 2020-12 *validity* check.

The core library has zero runtime dependencies, so it can't lean on ``jsonschema``
the way the console does. This checks that the keywords runspec actually emits
(see :func:`runspec.cli._arg_to_json_schema` / ``_build_schema``) are well-formed,
so ``runspec serve`` (and the ``runspec-mcp`` gateway) can **drop just the one
tool** whose schema the Anthropic API would reject rather than let it take down
the whole list.

Why it matters: an MCP client (Claude Code) forwards every tool's ``inputSchema``
to the model provider, and the Anthropic API rejects the **entire** ``tools``
array if any one is invalid (``400 tools.N.custom.input_schema: JSON schema is
invalid. It must match JSON Schema draft 2020-12``). So a single malformed
runnable makes a connecting agent see *no* tools at all, with no hint which one.

This is a *validity* check (is this a legal schema for the constructs we emit?),
not an instance check, and it is deliberately conservative — it flags only things
that are unambiguously wrong (a numeric keyword that isn't a number, a length
keyword that isn't a non-negative integer, a ``pattern`` that won't compile, a
draft-04-style array ``items``). Anything it can't judge, it passes.
"""

from __future__ import annotations

import re
from typing import Any

_JSON_TYPES = frozenset({"string", "number", "integer", "boolean", "object", "array", "null"})
# Keywords whose value must be a JSON number (draft 2020-12).
_NUMERIC_KEYWORDS = (
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
)
# Keywords whose value must be a non-negative integer.
_NONNEG_INT_KEYWORDS = (
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "minProperties",
    "maxProperties",
)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_nonneg_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def schema_issue(schema: Any, path: str = "") -> str:
    """``""`` if ``schema`` is a well-formed JSON Schema (for the keywords runspec
    emits), else a one-line reason naming the offending keyword/path.

    Recurses into ``properties`` and ``items`` — the only subschema shapes runspec
    produces (a ``multiple`` arg wraps its property in ``{type: array, items:
    <prop>}``)."""
    if isinstance(schema, bool):  # a boolean schema is valid
        return ""
    if not isinstance(schema, dict):
        return f"{path or 'schema'} is not an object"

    t = schema.get("type")
    if t is not None:
        for tt in t if isinstance(t, list) else [t]:
            if tt not in _JSON_TYPES:
                return f"{path}type '{tt}' is not a JSON Schema type"

    for kw in _NUMERIC_KEYWORDS:
        if kw in schema and not _is_number(schema[kw]):
            return f"{path}{kw} must be a number, got {schema[kw]!r}"

    for kw in _NONNEG_INT_KEYWORDS:
        if kw in schema and not _is_nonneg_int(schema[kw]):
            return f"{path}{kw} must be a non-negative integer, got {schema[kw]!r}"

    if "pattern" in schema:
        p = schema["pattern"]
        if not isinstance(p, str):
            return f"{path}pattern must be a string"
        try:
            re.compile(p)
        except re.error as exc:
            return f"{path}pattern is not a valid regex ({exc})"

    if "enum" in schema and not isinstance(schema["enum"], list):
        return f"{path}enum must be an array"

    if "required" in schema:
        req = schema["required"]
        if not isinstance(req, list) or not all(isinstance(x, str) for x in req):
            return f"{path}required must be an array of strings"

    props = schema.get("properties")
    if isinstance(props, dict):
        for name, sub in props.items():
            issue = schema_issue(sub, f"{path}properties.{name}.")
            if issue:
                return issue

    items = schema.get("items")
    if isinstance(items, dict):
        issue = schema_issue(items, f"{path}items.")
        if issue:
            return issue
    elif isinstance(items, list):
        return f"{path}items must be a schema, not an array (draft 2020-12 uses prefixItems for tuples)"

    return ""


def drop_invalid_tools(
    tools: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[tuple[str, str]]]:
    """Split a ``{name: tool-schema}`` map into the publishable subset and a list
    of ``(name, reason)`` for tools dropped because their ``inputSchema`` is
    invalid draft 2020-12. Order-preserving; the reason is from
    :func:`schema_issue`."""
    keep: dict[str, dict[str, Any]] = {}
    dropped: list[tuple[str, str]] = []
    for name, tool in tools.items():
        reason = schema_issue((tool or {}).get("inputSchema") or {})
        if reason:
            dropped.append((name, reason))
        else:
            keep[name] = tool
    return keep, dropped
