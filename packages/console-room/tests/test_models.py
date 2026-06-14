"""
test_models.py — the wire schema: message stamping, frame shapes, hello parsing.
Pure functions, no I/O.
"""

from __future__ import annotations

from console_room.models import (
    CATALOG_TOOLS_CAP,
    TEXT_CAP,
    Message,
    normalize_status,
    parse_catalog,
    parse_hello,
    presence_frame,
    request_text,
)


def test_message_new_stamps_id_and_ts():
    m = Message.new("ops", "alice@web01", "hi")
    assert m.room == "ops" and m.sender == "alice@web01" and m.text == "hi"
    assert m.id and isinstance(m.ts, float) and m.ts > 0


def test_message_text_is_capped():
    m = Message.new("ops", "a", "x" * (TEXT_CAP + 50))
    assert len(m.text) == TEXT_CAP + 1 and m.text.endswith("…")


def test_message_to_frame_shape():
    m = Message(room="ops", sender="a", text="hi", id="ID1", ts=1.5)
    assert m.to_frame() == {
        "type": "message",
        "room": "ops",
        "sender": "a",
        "text": "hi",
        "id": "ID1",
        "ts": 1.5,
        "kind": "chat",
    }


def test_request_message_frame_carries_payload():
    m = Message.new(
        "helpdesk/it/alice@corp",
        "alice@corp",
        "Requested: reset-password",
        kind="request",
        data={"tool": "reset-password", "args": {"user": "alice"}},
    )
    frame = m.to_frame()
    assert frame["kind"] == "request"
    assert frame["data"] == {"tool": "reset-password", "args": {"user": "alice"}}
    # Plain chat frames carry no data key at all.
    assert "data" not in Message.new("ops", "a", "hi").to_frame()


def test_presence_frame_normalises_status():
    assert presence_frame("ops", "a", "available")["status"] == "available"
    assert presence_frame("ops", "a", "whatever")["status"] == "away"
    assert presence_frame("ops", "a", "away") == {
        "type": "presence",
        "room": "ops",
        "user": "a",
        "status": "away",
    }


def test_normalize_status():
    assert normalize_status("available") == "available"
    assert normalize_status("away") == "away"
    assert normalize_status(None) == "away"


def test_parse_hello_ok():
    h = parse_hello({"type": "hello", "room": "ops", "user": "alice@web01"})
    assert h is not None and h.room == "ops" and h.user == "alice@web01"


def test_parse_hello_blank_user_falls_back():
    h = parse_hello({"type": "hello", "room": "ops", "user": "  "})
    assert h is not None and h.user == "unknown"


def test_parse_hello_rejects_missing_room_and_wrong_type():
    assert parse_hello({"type": "hello", "user": "a"}) is None
    assert parse_hello({"type": "hello", "room": "", "user": "a"}) is None
    assert parse_hello({"type": "message", "room": "ops"}) is None
    assert parse_hello("nope") is None


def test_parse_hello_namespace_means_agent():
    h = parse_hello({"type": "hello", "namespace": "helpdesk/it", "user": "agent"})
    assert h is not None and h.is_agent and h.namespace == "helpdesk/it"
    member = parse_hello({"type": "hello", "room": "ops", "user": "a"})
    assert member is not None and not member.is_agent


# ── catalog parsing ────────────────────────────────────────────────────────────


def test_parse_catalog_keeps_named_tools_and_passthrough_fields():
    tools = parse_catalog(
        {
            "type": "catalog",
            "tools": [
                {
                    "name": "reset-password",
                    "description": "Reset it",
                    "inputSchema": {"type": "object"},
                },
                {"description": "no name — dropped"},
                "not a dict",
                {"name": "  "},  # blank name — dropped
            ],
        }
    )
    assert tools is not None and len(tools) == 1
    assert tools[0]["name"] == "reset-password"
    assert tools[0]["inputSchema"] == {"type": "object"}  # passes through untouched


def test_parse_catalog_empty_list_is_valid_clear():
    assert parse_catalog({"type": "catalog", "tools": []}) == []


def test_parse_catalog_rejects_non_catalog_shapes():
    assert parse_catalog({"type": "catalog"}) is None  # no tools list
    assert parse_catalog({"type": "catalog", "tools": "nope"}) is None
    assert parse_catalog({"type": "message", "tools": []}) is None
    assert parse_catalog(None) is None


def test_parse_catalog_caps_tool_count():
    many = [{"name": f"t{i}"} for i in range(CATALOG_TOOLS_CAP + 50)]
    tools = parse_catalog({"type": "catalog", "tools": many})
    assert tools is not None and len(tools) == CATALOG_TOOLS_CAP


def test_request_text_renders_args():
    assert (
        request_text("request-vm", {"size": "large", "region": "eu"})
        == "Requested: request-vm — size=large, region=eu"
    )
    assert request_text("reset-password", {}) == "Requested: reset-password"


# ── email-verify frames ───────────────────────────────────────────────────────


def test_parse_advertise_valid():
    from console_room.models import parse_advertise

    ad = parse_advertise(
        {
            "type": "advertise",
            "queue": "it-support",
            "queue_email": "it-support@corp.com",
            "display_name": "IT Support",
        }
    )
    assert ad is not None
    assert ad.queue == "it-support"
    assert ad.queue_email == "it-support@corp.com"
    assert ad.display_name == "IT Support"


def test_parse_advertise_rejects_incomplete():
    from console_room.models import parse_advertise

    assert parse_advertise({"type": "advertise", "queue": "it"}) is None  # no email
    assert parse_advertise({"type": "advertise", "queue_email": "x@y"}) is None
    assert parse_advertise({"type": "message"}) is None
    assert parse_advertise("nope") is None


def test_send_verify_frame_shape():
    from console_room.models import SEND_VERIFY, send_verify_frame

    f = send_verify_frame(
        "n1",
        "jane@corp.com",
        "Jane",
        "it-support",
        "it@corp.com",
        "https://r/verify?token=t",
    )
    assert f["type"] == SEND_VERIFY
    assert f["nonce"] == "n1" and f["to"] == "jane@corp.com"
    assert f["queue_email"] == "it@corp.com"
    assert f["verify_url"].endswith("token=t")


def test_parse_verify_sent():
    from console_room.models import parse_verify_sent

    assert parse_verify_sent({"type": "verify_sent", "nonce": "n1", "ok": True}) == (
        "n1",
        True,
    )
    assert parse_verify_sent({"type": "verify_sent", "nonce": "n1"}) == ("n1", False)
    assert parse_verify_sent({"type": "verify_sent", "ok": True}) is None  # no nonce
    assert parse_verify_sent({"type": "message"}) is None
