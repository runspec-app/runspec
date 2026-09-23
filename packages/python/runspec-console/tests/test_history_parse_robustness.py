"""
test_history_parse_robustness.py — the History log parser must never crash on a
foreign / malformed ``*.log`` swept up by the broad ``*.log`` glob.

History fans out across every connected host (get_history_all). A brand-new host
with no runspec runs yet can still have some *other* ``.log`` in ~/logs or a venv
``logs/`` dir, and its lines are fed to ``_parse_log_text``. Those lines may be a
bare JSON scalar/array, or a run_summary-shaped object whose ``extra`` is ``null``
— both of which crashed the parser with AttributeError (``e.get("extra", {})``
returns ``None`` for an explicit ``null``, and calling ``.get`` on ``None`` /a
scalar throws). get_history swallows the per-host failure, so the symptom was a
new host silently dropping all its history + a noisy traceback in the log.
"""

from __future__ import annotations

import json

from runspec_console.bridge_utils import _parse_log_text, _record_kind


def _summary(**extra) -> str:
    base = {"event": "run_summary", "run_id": "r1", "runnable": "deploy"}
    base.update(extra)
    return json.dumps({"ts": "2026-07-22T10:00:00", "level": "INFO", "extra": base})


def test_non_object_lines_are_skipped_not_crashing():
    # A foreign log: a bare number, a string, and an array — none are records.
    log = "\n".join(["42", '"just a message"', "[1, 2, 3]", "not json at all"])
    assert _parse_log_text("foreign", log, "web-01") == []


def test_explicit_null_extra_does_not_crash():
    # A line that is an object but with extra: null (the AttributeError case).
    log = "\n".join(
        [
            json.dumps({"ts": "t", "level": "INFO", "message": "hi", "extra": None}),
            _summary(exit_code=0),
        ]
    )
    recs = _parse_log_text("deploy", log, "web-01")
    assert len(recs) == 1
    assert recs[0]["runnable"] == "deploy"


def test_missing_extra_key_still_parses():
    log = "\n".join(
        [json.dumps({"ts": "t", "level": "INFO", "message": "plain line"}), _summary()]
    )
    recs = _parse_log_text("deploy", log, "web-01")
    assert len(recs) == 1


def test_sequential_path_tolerates_bad_lines():
    # No run_id anywhere → the legacy sequential parser; still must not crash on
    # a null-extra / non-object line.
    log = "\n".join(
        [
            "true",
            json.dumps({"ts": "t", "level": "INFO", "message": "x", "extra": None}),
            json.dumps(
                {
                    "ts": "2026-07-22T10:00:00",
                    "level": "INFO",
                    "extra": {"event": "run_summary", "runnable": "legacy"},
                }
            ),
        ]
    )
    recs = _parse_log_text("legacy", log, "web-01")
    assert len(recs) == 1
    assert recs[0]["runnable"] == "legacy"


def test_kind_classification():
    assert _record_kind({}) == "runnable"
    assert _record_kind({"builtin": True, "tool": "chat"}) == "agent"
    assert _record_kind({"builtin": True, "tool": "outlook_inbox"}) == "tool"
    # A real runnable run never carries the builtin flag.
    assert _record_kind({"tool": "chat"}) == "runnable"


def test_record_carries_kind_for_runnable_and_tool():
    runnable = _parse_log_text("deploy", _summary(exit_code=0), "web-01")[0]
    assert runnable["kind"] == "runnable"
    tool = _parse_log_text(
        "tools",
        _summary(runnable="outlook_inbox", tool="outlook_inbox", builtin=True),
        "local",
    )[0]
    assert tool["kind"] == "tool"
