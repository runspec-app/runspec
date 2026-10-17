"""
test_delivery.py — agent-chosen output delivery (the `deliver` field on a
runnable call) and the after-the-fact reshaping tools.

Two layers:
  * pure ``workspaces`` helpers — the digest signature clustering, the
    document-field extraction, the large-string shape annotation, and the
    ``workspace_json_get`` / ``workspace_materialize`` file tools; and
  * the ``Bridge._finalize_tool_output`` orchestration — each ``deliver`` mode on
    each output shape, including the reality-overrides-intent fallbacks and the
    "envelope unchanged when deliver is absent" invariant.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from runspec_console import config as config_mod
from runspec_console import workspaces
from runspec_console.bridge import Bridge
from tests.session_stub import install_sessions

# ── pure helpers ──────────────────────────────────────────────────────────────


def test_human_size_bands():
    assert workspaces.human_size(500) == "500 B"
    assert workspaces.human_size(41984) == "41 KB"
    assert workspaces.human_size(2_000_000) == "1.9 MB"


def test_shape_annotates_a_large_string_field():
    # The one JSON shape a minified dump hides: which field is huge.
    shape = workspaces._json_shape({"body": "x" * 12_000, "id": 3, "small": "hi"})
    assert "body: str[12 KB]" in shape
    assert "id: int" in shape
    assert "small: str" in shape  # under 8 KB → bare str, no size


def test_line_digest_clusters_with_timestamps():
    log = "\n".join(
        [
            "2026-06-18T08:01:11 ERROR NullPointerException at com.acme.Foo.bar(Foo.java:42)",
            "2026-06-18T08:02:11 ERROR NullPointerException at com.acme.Foo.bar(Foo.java:42)",
            "2026-06-18T08:03:11 ERROR NullPointerException at com.acme.Foo.bar(Foo.java:42)",
            "2026-06-18T08:04:00 INFO served request 123 in 45ms",
            "2026-06-18T08:04:01 INFO served request 999 in 12ms",
        ]
    )
    dg = workspaces.line_digest(log, top=10)
    assert dg["delivered_as"] == "digest"
    assert dg["lines_scanned"] == 5
    top = {c["signature"]: c for c in dg["top"]}
    npe = top["NullPointerException@com.acme.Foo.bar"]
    assert npe["count"] == 3
    assert npe["first_ts"] == "2026-06-18T08:01:11"
    assert npe["last_ts"] == "2026-06-18T08:03:11"
    # The two INFO lines fold together — the request id + ms are masked.
    served = [c for s, c in top.items() if "served request" in s][0]
    assert served["count"] == 2


def test_line_digest_top_n_and_truncation():
    # 30 distinct signatures (a-z…) — numbers would be masked to <n> and collapse.
    log = "\n".join(f"event type {chr(65 + i % 30)} occurred" for i in range(300))
    dg = workspaces.line_digest(log, top=5)
    assert len(dg["top"]) == 5
    assert dg["distinct_signatures"] == 30
    assert dg["truncated"] is True


def test_extract_document_picks_longest_string():
    ex = workspaces.extract_document(
        {"status": 200, "body": "y" * 100, "note": "short"}, inline_max=1000
    )
    assert ex["field"] == "body"
    assert ex["fits"] is True
    assert ex["full_len"] == 100
    assert ex["text"] == "y" * 100
    assert "status: int" in ex["other_shape"]


def test_extract_document_over_cap_does_not_fit():
    ex = workspaces.extract_document({"body": "z" * 500, "id": 1}, inline_max=100)
    assert ex["fits"] is False
    assert ex["full_len"] == 500
    assert ex["text"] == "z" * 100  # capped to inline_max


def test_extract_document_none_without_string_field():
    assert workspaces.extract_document({"a": 1, "b": [1, 2]}, 100) is None
    assert workspaces.extract_document([1, 2, 3], 100) is None


# ── workspace_json_get (pure, jailed, paged) ──────────────────────────────────


def _write(ws: Path, rel: str, text: str) -> None:
    (ws / "tool-results").mkdir(parents=True, exist_ok=True)
    (ws / rel).write_text(text, encoding="utf-8")


def test_json_get_pages_a_string_field(tmp_path: Path):
    ws = tmp_path / "ws"
    body = "\n".join(f"row {i}" for i in range(50))
    _write(
        ws, "tool-results/x.json", json.dumps({"result": {"body": body}, "ok": True})
    )
    out = workspaces.json_get(
        ws, "tool-results/x.json", "result.body", offset=10, limit=5
    )
    assert out["type"] == "str"
    assert out["total_lines"] == 50
    assert out["offset"] == 10
    assert out["content"] == "\n".join(f"row {i}" for i in range(10, 15))
    assert out["truncated"] is True


def test_json_get_json_pointer_and_non_string(tmp_path: Path):
    ws = tmp_path / "ws"
    _write(ws, "tool-results/x.json", json.dumps({"a": {"b": [1, 2, 3]}}))
    out = workspaces.json_get(ws, "tool-results/x.json", "/a/b")
    assert out["type"] == "array"
    assert "array[3]" in out["shape"]
    assert json.loads(out["value"]) == [1, 2, 3]


def test_json_get_rejects_escape_and_bad_pointer(tmp_path: Path):
    ws = tmp_path / "ws"
    _write(ws, "tool-results/x.json", json.dumps({"a": 1}))
    with pytest.raises(ValueError):
        workspaces.json_get(ws, "../secrets.json", "a")
    with pytest.raises(ValueError):
        workspaces.json_get(ws, "tool-results/x.json", "nope.here")


def test_materialize_digests_a_text_spill(tmp_path: Path):
    ws = tmp_path / "ws"
    _write(ws, "tool-results/x.txt", "ERROR boom\nERROR boom\nINFO fine 1\nINFO fine 2")
    out = workspaces.materialize(ws, "tool-results/x.txt", as_="digest")
    assert out["delivered_as"] == "digest"
    assert out["source"] == "tool-results/x.txt"
    assert out["lines_scanned"] == 4


def test_materialize_refuses_json(tmp_path: Path):
    ws = tmp_path / "ws"
    _write(ws, "tool-results/x.json", json.dumps({"a": 1}))
    with pytest.raises(ValueError):
        workspaces.materialize(ws, "tool-results/x.json", as_="digest")


# ── Bridge._finalize_tool_output (orchestration) ──────────────────────────────


class _Stub:
    """Minimal Bridge for _finalize_tool_output: it needs a lock, a conversation
    id (→ workspace), and the output cap. Config + workspace_dir are monkeypatched
    per-fixture to a tmp workspace."""

    def __init__(self, cap: int = 1_000_000) -> None:
        self._lock = threading.Lock()
        install_sessions(self, "conv1")
        self._cap = cap

    def _tool_output_cap(self) -> int:
        return self._cap

    _finalize_tool_output = Bridge._finalize_tool_output
    _current_workspace_root = Bridge._current_workspace_root


@pytest.fixture()
def finalize(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    monkeypatch.setattr(config_mod, "workspace_dir", lambda sid: ws)
    monkeypatch.setattr(
        config_mod,
        "read_workspace",
        lambda: {"spill_threshold": 100, "inline_max": 200},
    )
    stub = _Stub()

    def _run(output, deliver="auto", spill_name="web-01__probe"):
        return stub._finalize_tool_output(output, deliver, spill_name, "")

    return _run, ws


def test_auto_below_threshold_is_unchanged(finalize):
    run, _ws = finalize
    small = "just a line"
    assert run(small, deliver="auto") == small
    # Envelope unchanged when deliver is absent (None → auto).
    assert run(small, deliver="") == small


def test_auto_above_threshold_spills_named_after_runnable(finalize):
    run, ws = finalize
    big = "x" * 150  # over the configured spill_threshold 100
    out = run(big, deliver="auto", spill_name="web-01__probe")
    assert "spilled to workspace" in out
    files = list((ws / "tool-results").glob("web-01__probe-*"))
    assert files, "spill file should be named after the resolved runnable"


def test_auto_honours_threshold_below_default(finalize):
    run, _ws = finalize
    # 150 chars is far under the 32 KB default but over the configured 100.
    out = run("q" * 150, deliver="auto", spill_name="web-01__probe")
    assert "spilled to workspace" in out
    assert run("q" * 100, deliver="auto") == "q" * 100


def test_inline_returns_raw_up_to_cap(finalize):
    run, _ws = finalize
    body = "y" * 150  # over spill_threshold 100 but under inline_max 200
    assert run(body, deliver="inline") == body


def test_inline_over_cap_falls_back_to_auto_with_note(finalize):
    run, ws = finalize
    body = "z" * 500  # over inline_max 200
    out = run(body, deliver="inline")
    assert out.startswith("[deliver=inline requested")
    assert "spilled to workspace" in out
    assert list((ws / "tool-results").glob("*"))


def test_digest_on_text_returns_clusters_and_keeps_raw(finalize):
    run, ws = finalize
    log = "\n".join(["ERROR boom"] * 8 + ["INFO ok 1", "INFO ok 2"])
    out = run(log, deliver="digest", spill_name="web-01__logs")
    data = json.loads(out)
    assert data["delivered_as"] == "digest"
    assert data["raw_spill"].startswith("tool-results/web-01__logs-")
    assert list((ws / "tool-results").glob("web-01__logs-*"))


def test_digest_on_json_falls_back_to_auto(finalize):
    run, _ws = finalize
    out = run(json.dumps({"a": 1}) + " " * 200, deliver="digest")
    assert out.startswith("[deliver=digest is for line-oriented text")


def test_extract_fits_returns_field_content(finalize):
    run, _ws = finalize
    payload = json.dumps({"status": 200, "body": "b" * 150})  # body < inline_max 200
    out = run(payload, deliver="extract")
    data = json.loads(out)
    assert data["delivered_as"] == "extract"
    assert data["field"] == "body"
    assert data["content"] == "b" * 150
    assert "status: int" in data["other_fields"]


def test_extract_over_cap_spills_and_points_at_json_get(finalize):
    run, ws = finalize
    payload = json.dumps({"status": 200, "body": "b" * 500})  # body > inline_max 200
    out = run(payload, deliver="extract", spill_name="web-01__doc")
    data = json.loads(out)
    assert data["field"] == "body"
    assert data["field_length"] == 500
    assert data["raw_spill"].startswith("tool-results/web-01__doc-")
    assert "workspace_json_get" in data["hint"]


def test_extract_on_non_object_falls_back(finalize):
    run, _ws = finalize
    out = run(json.dumps([1, 2, 3]) + " " * 200, deliver="extract")
    assert out.startswith("[deliver=extract is for a JSON object")


def test_unknown_deliver_value_is_treated_as_auto(finalize):
    run, _ws = finalize
    small = "hello"
    assert run(small, deliver="bogus") == small
