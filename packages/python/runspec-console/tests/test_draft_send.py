"""
test_draft_send.py — the draft-save → card Send-button path.

`_save_reply_draft` returns a structured result (so the card can offer a Send
button for the saved draft) and `send_trigger_draft` is the human-initiated send.
Both wrap Outlook COM, so the COM calls are patched; we assert the wiring/shape.
The methods only touch the module logger (not bridge state), so we bind them onto
a bare stub to avoid Bridge.__init__ side effects.
"""

from __future__ import annotations

import types
from unittest.mock import patch

from runspec_console.bridge import Bridge


class _Stub:
    pass


def _stub() -> _Stub:
    s = _Stub()
    s._save_reply_draft = types.MethodType(Bridge._save_reply_draft, s)
    s.send_trigger_draft = types.MethodType(Bridge.send_trigger_draft, s)
    return s


# ── _save_reply_draft → structured result (drives the Send button) ────────────


def test_save_reply_draft_returns_entry_id_and_message():
    info = {"to": "a@b.com", "subject": "Re: x", "entry_id": "DRAFT1"}
    with patch("runspec_console.sources.outlook.save_reply_draft", return_value=info):
        out = _stub()._save_reply_draft("ENT1", "hi")
    assert out["ok"] is True
    assert out["entry_id"] == "DRAFT1"
    assert out["to"] == "a@b.com"
    assert "Drafts" in out["message"] and "Send" in out["message"]


def test_save_reply_draft_reports_error():
    with patch(
        "runspec_console.sources.outlook.save_reply_draft",
        side_effect=RuntimeError("boom"),
    ):
        out = _stub()._save_reply_draft("ENT1", "hi")
    assert out["ok"] is False and "boom" in out["message"]


# ── send_trigger_draft → the card's Send button ───────────────────────────────


def test_send_trigger_draft_success():
    with patch(
        "runspec_console.sources.outlook.send_draft",
        return_value={"to": "a@b.com", "subject": "Re: x"},
    ):
        out = _stub().send_trigger_draft("DRAFT1")
    assert out == {"ok": True, "to": "a@b.com", "subject": "Re: x"}


def test_send_trigger_draft_empty_id_is_rejected():
    out = _stub().send_trigger_draft("")
    assert out["ok"] is False and "no draft" in out["error"]


def test_send_trigger_draft_reports_error():
    with patch(
        "runspec_console.sources.outlook.send_draft",
        side_effect=RuntimeError("send failed"),
    ):
        out = _stub().send_trigger_draft("DRAFT1")
    assert out["ok"] is False and "send failed" in out["error"]
