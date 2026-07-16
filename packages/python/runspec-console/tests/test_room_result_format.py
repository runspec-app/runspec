"""Unit tests for format_result_for_room — the console-side pretty-print applied
to a runnable's result before it is posted into a chat room, so JSON reads the
way the desktop console renders it rather than as a dense one-liner."""

from __future__ import annotations

import json

from runspec_console.bridge_utils import format_result_for_room


def test_compact_json_object_is_pretty_printed():
    out = format_result_for_room('{"status":"ok","count":3}')
    assert out == '{\n  "status": "ok",\n  "count": 3\n}'
    # Still valid JSON, just readable.
    assert json.loads(out) == {"status": "ok", "count": 3}


def test_json_array_is_pretty_printed():
    out = format_result_for_room('[{"host":"web01"},{"host":"web02"}]')
    assert "\n" in out and out.startswith("[")
    assert json.loads(out) == [{"host": "web01"}, {"host": "web02"}]


def test_non_ascii_is_preserved_not_escaped():
    out = format_result_for_room('{"name":"café"}')
    assert "café" in out  # ensure_ascii=False


def test_plain_text_passes_through_unchanged():
    assert format_result_for_room("done — 3 files copied") == "done — 3 files copied"


def test_failure_prefix_passes_through_unchanged():
    assert format_result_for_room("✗ command failed") == "✗ command failed"
    assert format_result_for_room("[exit 1] boom") == "[exit 1] boom"


def test_bare_scalar_left_alone():
    # A runnable that printed just `true` or a number keeps its exact output.
    assert format_result_for_room("true") == "true"
    assert format_result_for_room("42") == "42"


def test_truncated_json_is_not_mangled():
    # An unparseable (cut-off) blob is returned verbatim, never half-formatted.
    blob = '{"rows": [{"a": 1}, {"a": 2'
    assert format_result_for_room(blob) == blob


def test_empty_output_unchanged():
    assert format_result_for_room("") == ""


def test_already_pretty_json_stays_pretty():
    pretty = '{\n  "a": 1\n}'
    assert format_result_for_room(pretty) == pretty
