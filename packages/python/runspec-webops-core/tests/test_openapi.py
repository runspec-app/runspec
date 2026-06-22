"""OpenAPI / Swagger summarising — pure, against in-memory specs."""

import json

import pytest

from runspec_webops_core import SpecError, describe_operation, parse_document, summarize_spec
from runspec_webops_core.openapi import short_type

OPENAPI3 = {
    "openapi": "3.0.3",
    "info": {"title": "Orders API", "version": "2.1.0"},
    "servers": [{"url": "https://api.acme.test/v2"}],
    "paths": {
        "/orders": {
            "get": {
                "operationId": "listOrders",
                "summary": "List orders",
                "tags": ["orders"],
                "parameters": [{"name": "limit", "in": "query", "required": False, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {"type": "array", "items": {"$ref": "#/components/schemas/Order"}}}}}},
            },
            "post": {
                "operationId": "createOrder",
                "summary": "Create an order",
                "tags": ["orders"],
                "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Order"}}}},
                "responses": {"201": {"description": "created", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Order"}}}}},
            },
        },
        "/health": {"get": {"operationId": "health", "tags": ["ops"], "responses": {"200": {"description": "ok"}}}},
    },
    "components": {
        "schemas": {
            "Order": {
                "type": "object",
                "required": ["id", "total"],
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "total": {"type": "number"},
                    "lines": {"type": "array", "items": {"$ref": "#/components/schemas/Line"}},
                },
            },
            "Line": {"type": "object", "properties": {"sku": {"type": "string"}, "qty": {"type": "integer"}}},
        }
    },
}

SWAGGER2 = {
    "swagger": "2.0",
    "info": {"title": "Legacy API", "version": "1.0"},
    "host": "legacy.acme.test",
    "basePath": "/api",
    "schemes": ["https"],
    "paths": {
        "/widgets/{id}": {
            "get": {
                "operationId": "getWidget",
                "parameters": [{"name": "id", "in": "path", "required": True, "type": "string"}],
                "responses": {"200": {"description": "ok", "schema": {"$ref": "#/definitions/Widget"}}},
            }
        }
    },
    "definitions": {"Widget": {"type": "object", "properties": {"id": {"type": "string"}}}},
}


def test_parse_document_rejects_non_spec() -> None:
    with pytest.raises(SpecError):
        parse_document(json.dumps({"hello": "world"}))


def test_summarize_openapi3_overview() -> None:
    out = summarize_spec(OPENAPI3)
    assert out["title"] == "Orders API"
    assert out["spec_version"] == "3.0.3"
    assert out["servers"] == ["https://api.acme.test/v2"]
    assert out["operation_count"] == 3
    ops = {(o["method"], o["path"]) for o in out["operations"]}
    assert ("GET", "/orders") in ops and ("POST", "/orders") in ops


def test_summarize_filters_by_tag_and_path() -> None:
    assert summarize_spec(OPENAPI3, tag="ops")["operation_count"] == 1
    assert summarize_spec(OPENAPI3, path_filter="/orders")["operation_count"] == 2


def test_describe_operation_by_id_resolves_refs() -> None:
    out = describe_operation(OPENAPI3, operation_id="createOrder")
    assert out["method"] == "POST"
    assert out["request_body"]["required"] is True
    schema = out["request_body"]["schema"]
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"id", "total"}
    # Nested $ref is rendered as a short type, not expanded forever.
    assert schema["properties"]["lines"] == "array<Line>"
    assert schema["properties"]["id"] == "string(uuid)"
    assert out["responses"][0]["status"] == "201"


def test_describe_operation_by_path_and_method() -> None:
    out = describe_operation(OPENAPI3, path="/orders", method="get")
    assert out["operation_id"] == "listOrders"
    assert out["parameters"][0]["name"] == "limit"
    assert out["parameters"][0]["type"] == "integer"


def test_describe_missing_operation_raises() -> None:
    with pytest.raises(SpecError):
        describe_operation(OPENAPI3, operation_id="nope")


def test_swagger2_servers_and_params() -> None:
    overview = summarize_spec(SWAGGER2)
    assert overview["servers"] == ["https://legacy.acme.test/api"]
    detail = describe_operation(SWAGGER2, operation_id="getWidget")
    # Swagger 2 puts `type` on the parameter itself (no `schema`).
    assert detail["parameters"][0]["type"] == "string"
    assert detail["parameters"][0]["in"] == "path"
    assert detail["responses"][0]["schema"]["type"] == "object"


def test_short_type_labels() -> None:
    assert short_type(OPENAPI3, {"type": "string", "format": "date-time"}) == "string(date-time)"
    assert short_type(OPENAPI3, {"$ref": "#/components/schemas/Order"}) == "Order"
    assert short_type(OPENAPI3, {"type": "array", "items": {"$ref": "#/components/schemas/Line"}}) == "array<Line>"
