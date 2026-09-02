"""OpenAPI / Swagger spec reading — condense an API's current shape to bounded JSON.

The point: let an agent (or operator) see the *live* shape of an internal API —
its operations, parameters, and request/response schemas — so it can write or
update the runspec runnables that call it, and re-check when the API drifts. The
output is small and bounded (a map, not the raw spec), so it stays cheap to feed
to a model.

JSON specs (FastAPI ``/openapi.json``, springdoc ``/v3/api-docs``,
``swagger.json``) parse with the stdlib. YAML specs need the optional ``pyyaml``
extra. Both OpenAPI 3.x and Swagger 2.0 are understood. The fetch/read is the
only I/O; summarisation is pure.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from runspec_webops_core.errors import WebopsCoreError
from runspec_webops_core.http import fetch_body

# HTTP methods an OpenAPI path item may carry as operations.
_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

# Defensive caps so the summary stays bounded regardless of spec size.
MAX_OPERATIONS = 300
_MAX_PROPERTIES = 60
_MAX_ENUM = 20
_MAX_SCHEMA_DEPTH = 3


class SpecError(WebopsCoreError):
    """The spec could not be fetched, read, or parsed."""


# ── loading ───────────────────────────────────────────────────────────────────


def load_document(source: str, timeout: float = 15.0, insecure: bool = False, proxy: str | None = None, ca_bundle: str | None = None) -> dict:
    """Fetch (URL) or read (file path) a spec and parse it into a dict.

    ``proxy`` is forwarded to the fetch for URL sources (``"direct"`` forces no
    proxy; ``None`` auto-discovers); ``ca_bundle`` (a PEM path) is forwarded too
    for verifying against a private CA / TLS proxy. Both are ignored for local
    file paths.
    """
    if source.startswith(("http://", "https://")):
        status, body = fetch_body(source, timeout=timeout, insecure=insecure, proxy=proxy, ca_bundle=ca_bundle)
        if status >= 400:
            raise SpecError(f"fetching {source} returned HTTP {status}")
    else:
        path = Path(source)
        if not path.is_file():
            raise SpecError(f"no such spec file: {source}")
        body = path.read_text(encoding="utf-8", errors="replace")
    return parse_document(body)


def parse_document(text: str) -> dict:
    """Parse spec text as JSON, falling back to YAML when ``pyyaml`` is present."""
    text = text.strip()
    if not text:
        raise SpecError("spec document is empty")
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        data = _parse_yaml(text)
    if not isinstance(data, dict):
        raise SpecError("spec did not parse to an object")
    if "openapi" not in data and "swagger" not in data:
        raise SpecError("document is not an OpenAPI/Swagger spec (no 'openapi' or 'swagger' key)")
    return data


def _parse_yaml(text: str) -> Any:
    try:
        import yaml  # type: ignore[import-untyped]  # noqa: PLC0415 — optional dependency, only needed for YAML specs
    except ModuleNotFoundError as e:
        raise SpecError(
            "spec is not JSON and PyYAML is not installed — install the 'yaml' extra (pip install runspec-webops-core[yaml]) or point at the JSON spec endpoint (e.g. /openapi.json or /v3/api-docs)"
        ) from e
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SpecError(f"could not parse spec as JSON or YAML: {e}") from e


# ── $ref resolution ───────────────────────────────────────────────────────────


def _resolve_ref(doc: dict, ref: str) -> Any:
    """Resolve a local JSON-pointer ``$ref`` (``#/...``); returns None if missing."""
    if not ref.startswith("#/"):
        return None
    node: Any = doc
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _deref(doc: dict, schema: Any, depth: int = 0) -> Any:
    """Follow a single ``$ref`` (bounded), returning the referenced node."""
    if not isinstance(schema, dict) or "$ref" not in schema:
        return schema
    if depth > _MAX_SCHEMA_DEPTH + 2:
        return schema
    target = _resolve_ref(doc, schema["$ref"])
    if target is None:
        return schema
    return _deref(doc, target, depth + 1)


# ── schema summarising ────────────────────────────────────────────────────────


def _ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def short_type(doc: dict, schema: Any, depth: int = 0) -> str:
    """A one-token type label for a schema (``string``, ``array<Order>``, ``User``)."""
    if isinstance(schema, dict) and "$ref" in schema:
        return _ref_name(schema["$ref"])
    schema = _deref(doc, schema, depth)
    if not isinstance(schema, dict):
        return "any"
    kind = schema.get("type")
    if kind == "array":
        items = schema.get("items", {})
        if isinstance(items, dict) and "$ref" in items:
            return f"array<{_ref_name(items['$ref'])}>"
        return f"array<{short_type(doc, items, depth + 1)}>"
    fmt = schema.get("format")
    if isinstance(kind, str):
        return f"{kind}({fmt})" if fmt else kind
    if "properties" in schema:
        return "object"
    return "any"


def summarize_schema(doc: dict, schema: Any, depth: int = 0) -> dict:
    """A bounded structural summary of a schema (type, properties, required, …)."""
    if isinstance(schema, dict) and "$ref" in schema and depth >= _MAX_SCHEMA_DEPTH:
        return {"$ref": _ref_name(schema["$ref"])}
    resolved = _deref(doc, schema, depth)
    if not isinstance(resolved, dict):
        return {}

    out: dict = {}
    if isinstance(resolved.get("type"), str):
        out["type"] = resolved["type"]
    if "format" in resolved:
        out["format"] = resolved["format"]
    if "enum" in resolved and isinstance(resolved["enum"], list):
        out["enum"] = resolved["enum"][:_MAX_ENUM]
    if resolved.get("type") == "array" and "items" in resolved and depth < _MAX_SCHEMA_DEPTH:
        out["items"] = summarize_schema(doc, resolved["items"], depth + 1)

    props = resolved.get("properties")
    if isinstance(props, dict):
        if depth < _MAX_SCHEMA_DEPTH:
            out["properties"] = {name: short_type(doc, sub, depth + 1) for name, sub in list(props.items())[:_MAX_PROPERTIES]}
            if len(props) > _MAX_PROPERTIES:
                out["properties_truncated"] = True
        out.setdefault("type", "object")
        if isinstance(resolved.get("required"), list):
            out["required"] = resolved["required"]
    return out


# ── operations ────────────────────────────────────────────────────────────────


def _iter_operations(doc: dict) -> Iterator[tuple[str, str, dict, list]]:
    """Yield ``(path, METHOD, operation, shared_parameters)`` for every operation."""
    paths = doc.get("paths")
    if not isinstance(paths, dict):
        return
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters", []) if isinstance(item.get("parameters"), list) else []
        for method in _METHODS:
            op = item.get(method)
            if isinstance(op, dict):
                yield path, method.upper(), op, shared


def _servers(doc: dict) -> list[str]:
    if isinstance(doc.get("servers"), list):
        return [s["url"] for s in doc["servers"] if isinstance(s, dict) and isinstance(s.get("url"), str)]
    # Swagger 2.0: host + basePath + schemes.
    host = doc.get("host")
    if not host:
        return []
    base = doc.get("basePath", "") or ""
    raw_schemes = doc.get("schemes")
    schemes = raw_schemes if isinstance(raw_schemes, list) else ["https"]
    return [f"{scheme}://{host}{base}" for scheme in schemes]


def summarize_spec(doc: dict, *, tag: str | None = None, path_filter: str | None = None, max_operations: int = MAX_OPERATIONS) -> dict:
    """Overview: API info, servers, and a bounded list of operations."""
    info = doc.get("info", {}) if isinstance(doc.get("info"), dict) else {}
    operations: list[dict] = []
    total = 0
    for path, method, op, _shared in _iter_operations(doc):
        if tag is not None and tag not in (op.get("tags") or []):
            continue
        if path_filter is not None and path_filter not in path:
            continue
        total += 1
        if len(operations) < max_operations:
            summary = op.get("summary") or (op.get("description") or "")[:120] or None
            operations.append(
                {
                    "method": method,
                    "path": path,
                    "operation_id": op.get("operationId"),
                    "summary": summary,
                    "tags": op.get("tags") or [],
                }
            )
    return {
        "title": info.get("title"),
        "version": info.get("version"),
        "spec_version": doc.get("openapi") or doc.get("swagger"),
        "servers": _servers(doc),
        "operation_count": total,
        "returned": len(operations),
        "truncated": total > len(operations),
        "operations": operations,
    }


def _find_operation(doc: dict, operation_id: str | None, path: str | None, method: str | None) -> tuple[str, str, dict, list] | None:
    want_method = method.upper() if method else None
    for p, m, op, shared in _iter_operations(doc):
        if operation_id is not None and op.get("operationId") == operation_id:
            return p, m, op, shared
        if path is not None and p == path and (want_method is None or m == want_method):
            return p, m, op, shared
    return None


def _parameter(doc: dict, raw: Any) -> dict | None:
    raw = _deref(doc, raw)
    if not isinstance(raw, dict):
        return None
    # OpenAPI 3 nests the type under `schema`; Swagger 2 puts `type` on the param.
    schema = raw.get("schema")
    if schema is None and "type" in raw:
        schema = {"type": raw["type"], **({"format": raw["format"]} if "format" in raw else {})}
    return {
        "name": raw.get("name"),
        "in": raw.get("in"),
        "required": bool(raw.get("required")),
        "description": raw.get("description"),
        "type": short_type(doc, schema or {}),
        "schema": summarize_schema(doc, schema) if schema else {},
    }


def _request_body(doc: dict, op: dict) -> dict | None:
    rb = op.get("requestBody")
    if not isinstance(rb, dict):
        return None
    rb = _deref(doc, rb)
    content = rb.get("content") if isinstance(rb.get("content"), dict) else {}
    out: dict = {"required": bool(rb.get("required")), "content_types": list(content.keys()), "schema": None}
    for ctype, media in content.items():
        if isinstance(media, dict) and "schema" in media:
            out["selected_content_type"] = ctype
            out["schema"] = summarize_schema(doc, media["schema"])
            break
    return out


def _responses(doc: dict, op: dict) -> list[dict]:
    out: list[dict] = []
    raw_responses = op.get("responses")
    responses = raw_responses if isinstance(raw_responses, dict) else {}
    for code, resp in responses.items():
        resp = _deref(doc, resp) if isinstance(resp, dict) else {}
        schema = None
        content = resp.get("content")
        if isinstance(content, dict):
            for media in content.values():
                if isinstance(media, dict) and "schema" in media:
                    schema = summarize_schema(doc, media["schema"])
                    break
        elif "schema" in resp:  # Swagger 2.0
            schema = summarize_schema(doc, resp["schema"])
        out.append({"status": str(code), "description": resp.get("description"), "schema": schema})
    return out


def describe_operation(doc: dict, *, operation_id: str | None = None, path: str | None = None, method: str | None = None) -> dict:
    """Drill into one operation: parameters, request body, and responses (schemas resolved)."""
    match = _find_operation(doc, operation_id, path, method)
    if match is None:
        target = operation_id or (f"{(method or '').upper()} {path}".strip() if path else None) or "(none given)"
        raise SpecError(f"operation not found: {target}")
    p, m, op, shared = match
    parameters = [param for raw in [*shared, *(op.get("parameters") or [])] if (param := _parameter(doc, raw)) is not None]
    return {
        "method": m,
        "path": p,
        "operation_id": op.get("operationId"),
        "summary": op.get("summary"),
        "description": op.get("description"),
        "tags": op.get("tags") or [],
        "parameters": parameters,
        "request_body": _request_body(doc, op),
        "responses": _responses(doc, op),
    }
