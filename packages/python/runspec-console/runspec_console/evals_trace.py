"""evals_trace.py — emit one OpenTelemetry trace per scored eval case.

This is the opt-in bridge between the pure eval harness (:mod:`runspec_console.evals`,
which never imports observability) and the console's OTLP side-channel
(:mod:`runspec_console.observability`). Given a scored case it emits the same span
shape a live agent turn produces — ``turn → llm call → tool call`` — plus the
eval verdict under ``runspec.eval.*`` attributes, so any OTLP backend (Phoenix,
LangSmith, Langfuse, Tempo, Jaeger) becomes the eval dashboard: per-case traces
you can filter and aggregate into pass-rate-over-time, with the chosen tool + args
already on the tool span.

Every call is **best-effort and a complete no-op when tracing is off** —
``observability`` handles that internally, so callers (``record_evals.py``, a
future ``bridge_evals.py``) invoke this unconditionally without importing
opentelemetry or guarding on the extra. The ``runspec.eval.*`` attributes
deliberately reuse the ``improve.classify_bounce`` category vocabulary, so
proactive eval failures and reactive Improve incidents land under one taxonomy in
the same backend.

The trace mirrors ``observability.send_test_trace`` (the shipped "does telemetry
work" probe), so the eval spans render identically to a real turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from . import observability
from .evals import CaseResult, EvalCase


@dataclass
class EmittedSpans:
    """The ids of the trace :func:`emit_case` produced. ``tool_span_id`` is the
    span the eval verdict rides — the id a native exporter (``eval_export``)
    annotates in Phoenix. Both are ``""`` when tracing is off."""

    trace_id: str = ""
    tool_span_id: str = ""

    def __bool__(self) -> bool:  # truthy when a trace was actually emitted
        return bool(self.trace_id)


def eval_attributes(case: EvalCase, result: CaseResult) -> dict[str, Any]:
    """The ``runspec.eval.*`` span attributes for a scored case. Pure — returned
    as a plain dict so it is unit-testable without a tracer and reusable by a
    backend-native exporter (LangSmith Experiments / Phoenix Datasets) later."""
    attrs: dict[str, Any] = {
        "runspec.eval.case_id": case.id,
        "runspec.eval.pass": result.passed,
        "runspec.eval.category": result.category,
        "runspec.eval.pass_rate": result.pass_rate,
        "runspec.eval.samples": result.samples,
        "runspec.eval.expect_no_run": case.expect_no_run,
        "runspec.eval.reason": result.reason,
    }
    if case.expect_tool:
        attrs["runspec.eval.expected_tool"] = case.expect_tool
    if result.target:
        attrs["runspec.eval.observed_tool"] = result.target
    if case.expect_host is not None:
        attrs["runspec.eval.expected_host"] = case.expect_host
    return attrs


def emit_case(case: EvalCase, result: CaseResult, *, model: str = "") -> EmittedSpans:
    """Emit one ``turn → llm → tool`` trace for a scored eval case and return the
    trace + tool-span ids (empty :class:`EmittedSpans` when tracing is off / on any
    failure — never raises).

    The tool span carries the model's chosen tool + args (``add_tool_arguments``
    honours the ``capture_content`` privacy gate + secret scrubbing); the eval
    verdict rides ``runspec.eval.*`` on that span, and its span id is returned so a
    native exporter can annotate it. Content (the prompt / the model's final text)
    is attached only when ``capture_content`` is on."""
    if not observability.enabled():
        return EmittedSpans()
    out = EmittedSpans()
    handle = observability.begin_turn("eval", case.id, model=model)
    try:
        try:
            out.trace_id = format(handle.span.get_span_context().trace_id, "032x")
        except Exception:  # noqa: BLE001 — id is decorative
            pass

        with observability.llm_span(provider="eval", model=model) as llm:
            observability.add_prompt([{"role": "user", "content": case.prompt}])
            # No token usage in a dry run; record only the (synthetic) stop.
            observability.set_llm_response(
                llm,
                SimpleNamespace(
                    stop_reason="tool_use" if result.terminal else "end_turn"
                ),
                {},
            )
            if result.final_text:
                observability.add_completion(result.final_text)

        term = result.terminal
        with observability.tool_span(
            term.name if term else "(no tool)",
            case.id,
            decision="eval",
        ) as tool:
            if term is not None:
                observability.add_tool_arguments(tool, term.input)
            observability.set_attributes(**eval_attributes(case, result))
            try:
                out.tool_span_id = format(tool.get_span_context().span_id, "016x")
            except Exception:  # noqa: BLE001 — id is decorative
                pass
    except Exception:  # noqa: BLE001 — telemetry must never break a run
        pass
    finally:
        observability.end_turn(handle)
    return out
