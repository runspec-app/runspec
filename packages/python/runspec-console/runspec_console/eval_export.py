"""eval_export.py — native eval-result export to an OTLP backend.

The eval verdict already rides each tool span as ``runspec.eval.*`` attributes
(filterable, aggregatable). This module goes one step further for **Arize
Phoenix**: it POSTs the verdict as a first-class **span annotation**
(``/v1/span_annotations``) keyed by the span id the console minted, so pass/fail
shows in Phoenix's evaluation column with a score — not just a raw attribute.

Why only Phoenix: the export must stay **dependency-free and OTLP-native** (the
console deliberately carries no backend SDK — it dropped the Phoenix client in
0.229.0 to avoid owning a backend's lifecycle). Phoenix keys annotations by the
OTLP ``span_id``, which we know, so a plain HTTP POST suffices. **LangSmith
cannot** be done this way: an OTLP span id is 8 bytes but a LangSmith run id is a
UUID, so an ingested span has no run id we can derive to attach feedback to —
native LangSmith feedback needs their SDK's client-side run-id generation, which
is out of scope for the generic-OTLP design. On LangSmith the verdict stays the
``runspec.eval.*`` attributes.

Pure + best-effort: :func:`phoenix_payload` is a stdlib-only builder (unit-tested
without a network); :func:`post_phoenix_annotations` is a lazy urllib POST reusing
``webfetch``'s proxy / CA handling, and never raises into the caller — a failed
export must never fail an eval run.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .evals import CaseResult, EvalCase

logger = logging.getLogger(__name__)

# The annotation name (Phoenix groups + charts evaluations by this).
ANNOTATION_NAME = "tool_selection"


def phoenix_annotation(
    span_id: str, case: EvalCase, result: CaseResult
) -> dict[str, Any]:
    """One Phoenix ``SpanAnnotationData`` for a scored case. ``label`` is
    pass/fail (so Phoenix's eval column reads at a glance), ``score`` is the
    pass rate (1.0 / 0.0, or the fraction across samples), and ``explanation`` +
    ``metadata`` carry the category / observed tool / reason for drill-down.
    ``annotator_kind = "CODE"`` — this is a mechanical eval, not a human/LLM judge."""
    return {
        "span_id": span_id,
        "name": ANNOTATION_NAME,
        "annotator_kind": "CODE",
        "result": {
            "label": "pass" if result.passed else "fail",
            "score": result.pass_rate,
            "explanation": result.reason,
        },
        "metadata": {
            "category": result.category,
            "case_id": case.id,
            "expected_tool": case.expect_tool or "",
            "observed_tool": result.target,
            "samples": result.samples,
        },
        # Stable per-(span, eval) id so a re-run updates the annotation in place
        # instead of stacking duplicates.
        "identifier": f"{ANNOTATION_NAME}:{case.id}",
    }


def phoenix_payload(items: list[tuple[str, EvalCase, CaseResult]]) -> dict[str, Any]:
    """The ``POST /v1/span_annotations`` body for a batch of (span_id, case,
    result) — ``{"data": [SpanAnnotationData, …]}``. Entries with no span id are
    dropped (nothing to attach to)."""
    return {
        "data": [
            phoenix_annotation(span_id, case, result)
            for (span_id, case, result) in items
            if span_id
        ]
    }


def annotations_url(traces_endpoint: str) -> str:
    """Derive the span-annotations URL from the configured OTLP *traces* endpoint
    (``…/v1/traces`` → ``…/v1/span_annotations``). Falls back to appending the
    path when the endpoint doesn't end in ``/v1/traces``."""
    ep = (traces_endpoint or "").rstrip("/")
    if ep.endswith("/v1/traces"):
        return ep[: -len("/v1/traces")] + "/v1/span_annotations"
    return ep + "/v1/span_annotations"


def post_phoenix_annotations(
    traces_endpoint: str,
    items: list[tuple[str, EvalCase, CaseResult]],
    *,
    headers: dict[str, str] | None = None,
    ca_bundle: str | None = None,
    tls_verify: bool = True,
    timeout: float = 15.0,
) -> tuple[bool, str]:
    """POST the batch to Phoenix's ``/v1/span_annotations`` (``sync=true`` so it
    lands immediately). Best-effort — returns ``(ok, detail)`` and never raises.
    Dependency-free: reuses ``webfetch``'s opener (proxy env + optional private-CA
    / TLS-intercept handling)."""
    payload = phoenix_payload(items)
    if not payload["data"]:
        return (True, "no annotations to post")
    import urllib.request

    from .webfetch import _build_opener

    url = annotations_url(traces_endpoint) + "?sync=true"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("content-type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        opener = _build_opener(ca_bundle, tls_verify)
        with opener.open(req, timeout=timeout) as resp:
            code = getattr(resp, "status", 0) or resp.getcode()
        if 200 <= int(code) < 300:
            return (True, f"posted {len(payload['data'])} annotation(s)")
        return (False, f"HTTP {code}")
    except Exception as exc:  # noqa: BLE001 — export is best-effort
        logger.debug("eval export: Phoenix annotation POST failed", exc_info=True)
        return (False, str(exc))
