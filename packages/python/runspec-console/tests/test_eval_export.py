"""
test_eval_export.py — native eval-result export (``eval_export.py``).

The builders are pure (no network); the poster is exercised against a monkeypatched
urllib opener so the payload + URL are verified without a live Phoenix.
"""

from __future__ import annotations

import json

from runspec_console import eval_export, evals


def _case_result(passed=True, category="pass", target="restart-service", rate=1.0):
    case = evals.EvalCase(
        id="restart", prompt="restart nginx", expect_tool="restart-service"
    )
    res = evals.CaseResult(
        case_id="restart",
        passed=passed,
        category=category,
        reason="tool + args as expected" if passed else "wrong tool",
        target=target,
        samples=1,
        pass_count=1 if passed else 0,
    )
    return case, res


def test_phoenix_annotation_shape():
    case, res = _case_result()
    ann = eval_export.phoenix_annotation("abc123", case, res)
    assert ann["span_id"] == "abc123"
    assert ann["name"] == "tool_selection"
    assert ann["annotator_kind"] == "CODE"
    assert ann["result"] == {
        "label": "pass",
        "score": 1.0,
        "explanation": "tool + args as expected",
    }
    assert ann["metadata"]["category"] == "pass"
    assert ann["metadata"]["observed_tool"] == "restart-service"
    # stable identifier so a re-run updates in place
    assert ann["identifier"] == "tool_selection:restart"


def test_phoenix_annotation_fail_label_and_score():
    case, res = _case_result(
        passed=False, category="wrong_tool", target="stop-service", rate=0.0
    )
    ann = eval_export.phoenix_annotation("s1", case, res)
    assert ann["result"]["label"] == "fail"
    assert ann["result"]["score"] == 0.0
    assert ann["metadata"]["observed_tool"] == "stop-service"


def test_phoenix_payload_drops_spanless_entries():
    case, res = _case_result()
    payload = eval_export.phoenix_payload([("s1", case, res), ("", case, res)])
    assert len(payload["data"]) == 1
    assert payload["data"][0]["span_id"] == "s1"


def test_annotations_url_derivation():
    assert (
        eval_export.annotations_url("http://127.0.0.1:6006/v1/traces")
        == "http://127.0.0.1:6006/v1/span_annotations"
    )
    # non-standard endpoint → append the path
    assert (
        eval_export.annotations_url("https://phoenix.example.com")
        == "https://phoenix.example.com/v1/span_annotations"
    )


def test_post_is_noop_with_no_items():
    ok, detail = eval_export.post_phoenix_annotations("http://x/v1/traces", [])
    assert ok is True and "no annotations" in detail


def test_post_sends_the_payload_to_the_annotations_url(monkeypatch):
    captured = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:
        def open(self, req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = json.loads(req.data.decode())
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            return _Resp()

    monkeypatch.setattr(
        eval_export, "_build_opener", lambda *a, **k: _Opener(), raising=False
    )
    # _build_opener is imported inside the function from .webfetch; patch there.
    from runspec_console import webfetch

    monkeypatch.setattr(webfetch, "_build_opener", lambda *a, **k: _Opener())

    case, res = _case_result()
    ok, detail = eval_export.post_phoenix_annotations(
        "http://127.0.0.1:6006/v1/traces",
        [("span-42", case, res)],
        headers={"x-api-key": "secret"},
    )
    assert ok is True and "posted 1" in detail
    assert captured["url"] == "http://127.0.0.1:6006/v1/span_annotations?sync=true"
    assert captured["method"] == "POST"
    assert captured["body"]["data"][0]["span_id"] == "span-42"
    assert captured["headers"]["x-api-key"] == "secret"
