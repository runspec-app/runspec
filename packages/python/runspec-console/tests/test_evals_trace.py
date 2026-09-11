"""
test_evals_trace.py — the eval→OTLP bridge (``evals_trace.py``).

Two things matter: (1) ``eval_attributes`` is pure and buckets the verdict under
``runspec.eval.*`` (no tracer needed); (2) ``emit_case`` produces the same
``turn → llm → tool`` span tree a live turn does, with the eval verdict on the
tool span — verified network-free with an in-memory exporter. When the `otel`
extra is absent, ``emit_case`` is a silent no-op (the disabled-path contract).
"""

from __future__ import annotations

import pytest

from runspec_console import evals, evals_trace
from runspec_console import observability as obs

_OK, _ = obs.available()
otel_only = pytest.mark.skipif(not _OK, reason="opentelemetry SDK not installed")


def _case_and_pass_result():
    case = evals.EvalCase(
        id="restart",
        prompt="restart nginx",
        expect_tool="restart-service",
        expect_args={"name": "nginx"},
    )
    traj = evals.Trajectory(
        terminal=evals.ToolInvocation("restart-service", {"name": "nginx"}),
        final_text="",
    )
    result = evals.score(
        case,
        traj,
        resolve_target=lambda inv: inv.name,
        resolve_args=lambda inv: inv.input,
    )
    return case, result


# ── pure attribute mapping (no SDK) ───────────────────────────────────────────
def test_eval_attributes_buckets_a_pass():
    case, result = _case_and_pass_result()
    attrs = evals_trace.eval_attributes(case, result)
    assert attrs["runspec.eval.pass"] is True
    assert attrs["runspec.eval.category"] == evals.CAT_PASS
    assert attrs["runspec.eval.expected_tool"] == "restart-service"
    assert attrs["runspec.eval.observed_tool"] == "restart-service"
    assert attrs["runspec.eval.pass_rate"] == 1.0


def test_eval_attributes_buckets_a_wrong_tool():
    case = evals.EvalCase(id="c", prompt="restart nginx", expect_tool="restart-service")
    traj = evals.Trajectory(terminal=evals.ToolInvocation("stop-service", {}))
    result = evals.score(case, traj, resolve_target=lambda inv: inv.name)
    attrs = evals_trace.eval_attributes(case, result)
    assert attrs["runspec.eval.pass"] is False
    assert attrs["runspec.eval.category"] == evals.CAT_WRONG_TOOL
    assert attrs["runspec.eval.observed_tool"] == "stop-service"


def test_eval_attributes_for_a_clarify_case_has_no_observed_tool():
    case = evals.EvalCase(id="c", prompt="delete it", expect_no_run=True)
    traj = evals.Trajectory(terminal=None, final_text="Which host?")
    result = evals.score(case, traj)
    attrs = evals_trace.eval_attributes(case, result)
    assert attrs["runspec.eval.pass"] is True
    assert attrs["runspec.eval.expect_no_run"] is True
    assert "runspec.eval.observed_tool" not in attrs


# ── disabled-path contract (works with or without the SDK) ────────────────────
def test_emit_case_is_a_noop_when_tracing_is_off():
    obs.reset_for_test() if _OK else None
    case, result = _case_and_pass_result()
    # Tracing not initialised → empty ids, falsy, no raise.
    out = evals_trace.emit_case(case, result, model="m")
    assert out.trace_id == "" and not out


# ── the span tree, network-free ───────────────────────────────────────────────
@otel_only
def test_emit_case_produces_turn_llm_tool_with_eval_attrs():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    obs.init_tracing_for_test(exporter, capture_content=True)
    try:
        case, result = _case_and_pass_result()
        emitted = evals_trace.emit_case(case, result, model="test-model")
        trace_id = emitted.trace_id
        obs.flush()
    finally:
        obs.reset_for_test()

    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert "runspec.agent.turn" in names
    assert any(s.name.startswith("execute_tool") for s in spans)

    turn = next(s for s in spans if s.name == "runspec.agent.turn")
    assert turn.attributes.get("runspec.turn.kind") == "eval"
    assert trace_id  # non-empty when active

    tool = next(s for s in spans if s.name.startswith("execute_tool"))
    assert tool.attributes.get("runspec.eval.pass") is True
    assert tool.attributes.get("runspec.eval.category") == evals.CAT_PASS
    assert tool.attributes.get("runspec.eval.observed_tool") == "restart-service"
    # all spans share the one trace
    assert len({s.context.trace_id for s in spans}) == 1
    # the returned tool-span id matches the emitted tool span (the id a native
    # exporter annotates in Phoenix)
    assert emitted.tool_span_id == format(tool.context.span_id, "016x")

    # ── dual-emit: OpenInference alongside gen_ai, so Phoenix (which doesn't read
    # gen_ai.*) classifies each span and renders the LLM/tool cards ──
    llm = next(s for s in spans if s.name.startswith("chat"))
    assert turn.attributes.get("openinference.span.kind") == "AGENT"
    assert turn.attributes.get("gen_ai.operation.name") == "invoke_agent"
    assert llm.attributes.get("openinference.span.kind") == "LLM"
    assert llm.attributes.get("llm.model_name") == "test-model"
    assert tool.attributes.get("openinference.span.kind") == "TOOL"
    assert tool.attributes.get("tool.name") == "restart-service"
    # capture_content on → the prompt rides input.value (OpenInference) + gen_ai.prompt
    assert llm.attributes.get("input.value")
    assert "gen_ai.prompt" in llm.attributes
    # the chosen tool's args ride both the OpenInference + gen_ai keys
    assert tool.attributes.get("tool_call.function.arguments")
    assert "gen_ai.tool.call.arguments" in tool.attributes
