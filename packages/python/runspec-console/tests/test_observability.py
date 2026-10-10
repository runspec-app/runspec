"""
test_observability.py — OpenTelemetry (OTLP) agent tracing.

Covers the four things that matter for this feature:
  1. config.read_telemetry() defaults + clamping (pure, no SDK path).
  2. The "never raises / complete no-op when disabled" contract — the same
     discipline trace.py has, so a base install without the `otel` extra (or with
     telemetry off) runs the agent loop untouched.
  3. The connected span tree across the thread boundary — the failure mode this
     feature is most likely to hit: because tool execution dispatches through
     asyncio.to_thread, a span created in the worker thread must still parent to
     the turn's trace. Driven through the *real* _agentic_chat_turn +
     _gated_run_tool_async with a scripted adapter (mirrors test_agent_continuation).
  4. The privacy gate — content (prompts / completions / tool args) is absent
     unless capture_content is on, and secret-looking args are scrubbed even then.

The in-memory span exporter keeps every assertion network-free.
"""

from __future__ import annotations

import asyncio
import threading
import types

import pytest

from runspec_console import config as config_mod
from runspec_console import observability as obs
from runspec_console.adapters.base import ChatResponse, ToolCall
from runspec_console.bridge import Bridge
from tests.session_stub import agentic_shim, install_sessions

# The SDK is a dev dependency; skip the span-tree tests cleanly if it's absent.
_OK, _ = obs.available()
otel_only = pytest.mark.skipif(not _OK, reason="opentelemetry SDK not installed")


@pytest.fixture
def exporter():
    """A fresh in-memory exporter wired into observability; torn down after."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exp = InMemorySpanExporter()
    yield exp
    obs.reset_for_test()


# ── 1. config.read_telemetry() ────────────────────────────────────────────────


def test_read_telemetry_defaults(monkeypatch):
    monkeypatch.setattr(config_mod, "read_config", lambda: {})
    t = config_mod.read_telemetry()
    assert t["enabled"] is False  # opt-in — exports data externally
    assert t["capture_content"] is False  # privacy default
    assert t["endpoint"] is None
    assert t["headers"] == {}
    assert t["protocol"] == "http/protobuf"
    assert t["service_name"] == "runspec-console"
    assert t["sample_ratio"] == 1.0
    assert t["tls_verify"] is True
    assert t["max_content_chars"] == 0  # 0 = no limit (faithful traces)
    # service_name defaults to the branded app slug (runspec-console by default,
    # asserted above); label (→ service.instance.id) defaults blank (the hostname
    # is filled in at init).
    assert t["label"] == ""


def test_read_telemetry_service_name_derives_from_app_slug(monkeypatch):
    """An unset service_name follows the branded app slug (the config-dir / window-
    title source), so a white-label build self-identifies; an explicit value wins."""
    monkeypatch.setattr(config_mod, "read_config", lambda: {})
    monkeypatch.setattr(config_mod, "_app_slug", lambda: "jason-console")
    assert config_mod.read_telemetry()["service_name"] == "jason-console"
    # An explicit service_name overrides the derived slug.
    monkeypatch.setattr(
        config_mod,
        "read_config",
        lambda: {"telemetry": {"service_name": "custom-svc"}},
    )
    assert config_mod.read_telemetry()["service_name"] == "custom-svc"


def test_read_telemetry_label(monkeypatch):
    monkeypatch.setattr(
        config_mod, "read_config", lambda: {"telemetry": {"label": "  ops-console-1 "}}
    )
    assert config_mod.read_telemetry()["label"] == "ops-console-1"


def test_read_telemetry_max_content_chars(monkeypatch):
    monkeypatch.setattr(
        config_mod, "read_config", lambda: {"telemetry": {"max_content_chars": 8000}}
    )
    assert config_mod.read_telemetry()["max_content_chars"] == 8000
    # Negative / junk floors to 0 (no limit).
    monkeypatch.setattr(
        config_mod, "read_config", lambda: {"telemetry": {"max_content_chars": -5}}
    )
    assert config_mod.read_telemetry()["max_content_chars"] == 0


def test_read_telemetry_reads_and_clamps(monkeypatch):
    monkeypatch.setattr(
        config_mod,
        "read_config",
        lambda: {
            "telemetry": {
                "enabled": True,
                "endpoint": "  https://ls.internal/otel/v1/traces  ",
                "headers": {"x-api-key": "ls-secret", "Project": 42},
                "sample_ratio": 5.0,  # over-range → clamped to 1.0
                "capture_content": True,
                "tls_verify": False,
            }
        },
    )
    t = config_mod.read_telemetry()
    assert t["enabled"] is True
    assert t["endpoint"] == "https://ls.internal/otel/v1/traces"
    assert t["headers"] == {"x-api-key": "ls-secret", "Project": "42"}
    assert t["sample_ratio"] == 1.0
    assert t["capture_content"] is True
    assert t["tls_verify"] is False


def test_read_telemetry_sample_ratio_floor(monkeypatch):
    monkeypatch.setattr(
        config_mod, "read_config", lambda: {"telemetry": {"sample_ratio": -3}}
    )
    assert config_mod.read_telemetry()["sample_ratio"] == 0.0


def test_read_telemetry_api_key_command(monkeypatch):
    monkeypatch.setattr(
        config_mod,
        "read_config",
        lambda: {"telemetry": {"api_key_command": "  keyring get ls  "}},
    )
    t = config_mod.read_telemetry()
    assert t["api_key_command"] == "keyring get ls"
    assert t["api_key_header"] == "x-api-key"  # default header
    # Absent ⇒ None command, default header still present.
    monkeypatch.setattr(config_mod, "read_config", lambda: {})
    assert config_mod.read_telemetry()["api_key_command"] is None


# ── [telemetry.metrics] config (separate signal, independent endpoint) ─────────


def test_read_telemetry_metrics_defaults(monkeypatch):
    monkeypatch.setattr(config_mod, "read_config", lambda: {})
    m = config_mod.read_telemetry()["metrics"]
    assert m["enabled"] is False  # independent of trace enabled, opt-in
    assert m["endpoint"] is None
    assert m["headers"] == {}
    assert m["export_interval"] == 60
    assert m["api_key_header"] == "x-api-key"
    assert m["service_name"] == "runspec-console"


def test_read_telemetry_metrics_independent_endpoint_and_auth(monkeypatch):
    """Metrics endpoint + auth are metrics-specific and do NOT inherit the trace
    values — the split case (traces → LangSmith, metrics → Grafana) must not ship
    the traces API key to the metrics backend."""
    monkeypatch.setattr(
        config_mod,
        "read_config",
        lambda: {
            "telemetry": {
                "enabled": True,
                "endpoint": "https://ls.internal/otel/v1/traces",
                "headers": {"x-api-key": "ls-secret"},
                "api_key_command": "keyring get ls",
                "metrics": {
                    "enabled": True,
                    "endpoint": "  https://grafana.internal/otlp/v1/metrics  ",
                    "headers": {"Authorization": "Basic abc"},
                    "export_interval": 15,
                },
            }
        },
    )
    m = config_mod.read_telemetry()["metrics"]
    assert m["enabled"] is True
    assert m["endpoint"] == "https://grafana.internal/otlp/v1/metrics"
    # Auth is NOT inherited from [telemetry].
    assert m["headers"] == {"Authorization": "Basic abc"}
    assert m["api_key_command"] is None
    assert m["export_interval"] == 15


def test_read_telemetry_metrics_transport_trust_falls_back(monkeypatch):
    """ca_bundle + tls_verify (non-secret transport trust) inherit from
    [telemetry] when unset under [telemetry.metrics]; an explicit value wins."""
    monkeypatch.setattr(
        config_mod,
        "read_config",
        lambda: {
            "telemetry": {
                "ca_bundle": "/etc/pki/corp-ca.pem",
                "tls_verify": False,
                "metrics": {"enabled": True},
            }
        },
    )
    m = config_mod.read_telemetry()["metrics"]
    assert m["ca_bundle"] == "/etc/pki/corp-ca.pem"  # inherited
    assert m["tls_verify"] is False  # inherited
    # An explicit metrics ca_bundle overrides the inherited one.
    monkeypatch.setattr(
        config_mod,
        "read_config",
        lambda: {
            "telemetry": {
                "ca_bundle": "/etc/pki/corp-ca.pem",
                "metrics": {"enabled": True, "ca_bundle": "/etc/pki/metrics-ca.pem"},
            }
        },
    )
    assert (
        config_mod.read_telemetry()["metrics"]["ca_bundle"] == "/etc/pki/metrics-ca.pem"
    )


def test_read_telemetry_metrics_export_interval_floor(monkeypatch):
    monkeypatch.setattr(
        config_mod,
        "read_config",
        lambda: {"telemetry": {"metrics": {"export_interval": 0}}},
    )
    assert config_mod.read_telemetry()["metrics"]["export_interval"] == 60


# ── api_key_command → header resolution ("ship the command, not the key") ─────


def test_resolve_headers_injects_command_output():
    """The command's stdout is injected under api_key_header, merged with the
    static headers — so the config carries the command, never the key."""
    headers = obs._resolve_headers(
        {
            "headers": {"Langsmith-Project": "ops"},
            "api_key_command": "echo sk-from-cmd",
            "api_key_header": "x-api-key",
        }
    )
    assert headers == {"Langsmith-Project": "ops", "x-api-key": "sk-from-cmd"}


def test_resolve_headers_custom_header_and_no_command():
    assert obs._resolve_headers(
        {"api_key_command": "echo tok", "api_key_header": "Authorization"}
    ) == {"Authorization": "tok"}
    # No command ⇒ just the static headers, unchanged.
    assert obs._resolve_headers({"headers": {"a": "b"}}) == {"a": "b"}


def test_resolve_headers_failing_command_raises():
    """A failing command raises (so init_tracing disables tracing rather than
    exporting unauthenticated) — never silently drops the key."""
    import pytest as _pytest

    with _pytest.raises(RuntimeError):
        obs._resolve_headers({"api_key_command": "false"})


# ── 2. disabled = complete no-op ──────────────────────────────────────────────


def test_disabled_is_noop():
    """With tracing off, every helper is a no-op and nothing raises — the agent
    loop calls these unconditionally and must never be affected."""
    obs.reset_for_test()
    assert obs.enabled() is False
    assert obs.init_tracing({"enabled": False}) is False
    with obs.turn_span("chat", "c1", model="m") as t:
        assert t is None
        with obs.llm_span(provider="p", model="m") as llm:
            assert llm is None
            obs.add_prompt([{"role": "user", "content": "hi"}])
            obs.add_completion("hello")
        with obs.tool_span("tool", "c1", decision="run") as ts:
            assert ts is None
            obs.set_tool_exit(ts, "ok")
    assert obs.current_span() is None
    obs.set_attributes(**{"gen_ai.response.id": "x"})  # no-op, no raise
    handle = obs.begin_turn("chat", "c1")
    assert handle is None
    obs.end_turn(handle)  # tolerates a None handle


@otel_only
def test_reconfigure_round_trip():
    """reconfigure() is the runtime re-init seam for re-pointing the exporter:
    it must swap the active provider (init_tracing alone can't — it's idempotent)
    and leave a clean no-op when re-inited disabled."""
    obs.reset_for_test()
    assert obs.enabled() is False
    # Disabled → enabled at a local endpoint.
    assert (
        obs.reconfigure(
            {"enabled": True, "endpoint": "http://127.0.0.1:6006/v1/traces"}
        )
        is True
    )
    assert obs.enabled() is True
    first_provider = obs._PROVIDER
    # Re-point at a different endpoint → a *new* provider, still active (proves the
    # idempotent init_tracing was actually torn down and re-run).
    assert (
        obs.reconfigure(
            {"enabled": True, "endpoint": "http://127.0.0.1:7007/v1/traces"}
        )
        is True
    )
    assert obs.enabled() is True
    assert obs._PROVIDER is not first_provider
    # Reconfigure disabled → complete no-op again.
    assert obs.reconfigure({"enabled": False}) is False
    assert obs.enabled() is False
    assert obs._PROVIDER is None
    obs.reset_for_test()


# ── scrubbing (pure) ──────────────────────────────────────────────────────────


def test_scrub_args_redacts_secrets_and_caps():
    out = obs.scrub_args(
        {
            "host": "web1",
            "password": "hunter2",
            "api_key": "sk-123",
            "nested": {"token": "abc", "note": "fine"},
            "long": "x" * 9000,
        }
    )
    assert out["host"] == "web1"
    assert out["password"] == "«redacted»"
    assert out["api_key"] == "«redacted»"
    assert out["nested"]["token"] == "«redacted»"
    assert out["nested"]["note"] == "fine"
    assert out["long"].endswith("…[truncated]")


# ── 3 & 4. connected span tree + privacy, via the real chat turn ──────────────


class _ScriptedAdapter:
    """One text chunk + a done-response per call, walking a scripted list of
    (text, stop_reason[, tool_calls]) steps (mirrors test_agent_continuation)."""

    model = "scripted-model"

    def __init__(self, script):
        self._script = script
        self.calls = 0

    async def stream_with_tools(self, messages, tools):
        step = self._script[min(self.calls, len(self._script) - 1)]
        text, stop = step[0], step[1]
        tool_calls = list(step[2]) if len(step) > 2 else []
        self.calls += 1
        if text:
            yield ("text", text)

        class _Raw:
            class usage:
                input_tokens = 11
                output_tokens = 7
                cache_read_input_tokens = 0
                cache_creation_input_tokens = 0

        yield (
            "done",
            ChatResponse(
                text=None, tool_calls=tool_calls, stop_reason=stop, _raw=_Raw()
            ),
        )

    def make_tool_turn(self, response, results):
        return [
            {"role": "assistant", "content": response.text or ""},
            {
                "role": "user",
                "content": [
                    {"tool_use_id": tc.id, "content": out} for tc, out in results
                ],
            },
        ]


def _chat_stub():
    """A bare stub with the REAL _agentic_chat_turn + _gated_run_tool_async bound,
    so the tool span is created by production code at its real chokepoint."""
    s = types.SimpleNamespace()
    s._chat_cancels = {}
    s._lock = threading.Lock()
    install_sessions(s)
    s._run_origin = {}
    s._config = {"llm": {"max_continuations": 3}}
    s.tokens = []
    s.tool_runs = []

    s.get_config = lambda: s._config
    s._runnables_to_tools = lambda *a, **k: []
    s._tool_mode = lambda: "dedup"
    s._chat_index_text = lambda: ""
    s._messages_chars = lambda _msgs: 0
    s._trace = lambda *a, **k: None
    s._play_sound = lambda *a, **k: None
    s._window_is_foreground = lambda: True
    s._dispatch = lambda event, payload: (
        s.tokens.append(payload["token"]) if event == "runspec:token" else None
    )
    s._persist_conversation_turn = lambda *a, **k: None
    # Chat run_summary audit — exercised in its own test; a no-op here keeps these
    # trace-focused tests independent of the audit-log write path.
    s._audit_chat_turn = lambda *a, **k: None

    # Autonomy gate helpers: a plain runnable tool, autonomous ("run") so no
    # confirm prompt; _run_tool_sync creates a child span so we can prove the
    # asyncio.to_thread context copy keeps it in the same trace.
    s._confirm_decision = lambda name, inp: "run"
    s._confirm_card_view = lambda name, inp: (name, "")

    def run_tool_sync(name, inp, chat_id="", cancel_event=None, on_progress=None):
        s.tool_runs.append((name, inp))
        # A span created on the worker thread — the regression guard: it must
        # parent to the tool span that was active before the to_thread dispatch.
        with obs._TRACER.start_as_current_span("runnable.exec"):
            pass
        return "ok: ran"

    s._run_tool_sync = run_tool_sync

    # _usage_from_response and _context_window are @staticmethods — assign the
    # plain functions (no self). _context_window sizes the context gauge from the
    # adapter's capabilities(), falling back to the model-id window table.
    s._usage_from_response = Bridge._usage_from_response
    s._context_window = Bridge._context_window
    for meth in (
        "_max_continuations",
        "_max_tool_rounds",
        "_max_parallel_tools",
        "_parallel_safe_call",
        "_parallel_confirm_call",
        "_runnable_run_decision",
        "_max_network_retries",
        "_max_self_recoveries",
        "_gated_run_tool_async",
    ):
        setattr(s, meth, types.MethodType(getattr(Bridge, meth), s))
    s._append_recovery_prompt = Bridge._append_recovery_prompt  # @staticmethod
    s._agentic_chat_turn = agentic_shim(s)
    return s


@otel_only
def test_chat_turn_emits_connected_trace(exporter, monkeypatch):
    """turn → llm → tool → runnable.exec all land in ONE trace, correctly
    parented, with the expected GenAI attributes. Content is absent (capture off)."""
    monkeypatch.setattr(
        config_mod, "read_config", lambda: {"llm": {"provider": "anthropic"}}
    )
    obs.init_tracing_for_test(exporter, capture_content=False)

    s = _chat_stub()
    tc = ToolCall(id="t1", name="web1__deploy", input={"password": "sekret"})
    adapter = _ScriptedAdapter(
        [("thinking", "tool_use", [tc]), ("all done", "end_turn")]
    )
    asyncio.run(s._agentic_chat_turn("chat1", "deploy it", adapter))
    obs.shutdown()

    finished = exporter.get_finished_spans()
    spans = {sp.name: sp for sp in finished}
    assert "runspec.agent.turn" in spans
    assert "chat scripted-model" in spans
    assert "execute_tool web1__deploy" in spans
    assert "runnable.exec" in spans

    turn = spans["runspec.agent.turn"]
    tool = spans["execute_tool web1__deploy"]
    exec_ = spans["runnable.exec"]
    # Two model calls (tool-use iteration + closing iteration) → two LLM spans.
    llm_spans = [sp for sp in finished if sp.name == "chat scripted-model"]
    assert len(llm_spans) == 2
    llm = llm_spans[0]  # the first iteration (returned the tool call)

    # One connected trace.
    trace_ids = {sp.context.trace_id for sp in finished}
    assert len(trace_ids) == 1

    # Parenting: every llm span + the tool under the turn; the worker-thread span
    # under the tool (this is the asyncio.to_thread context-copy guard).
    assert all(sp.parent.span_id == turn.context.span_id for sp in llm_spans)
    assert tool.parent.span_id == turn.context.span_id
    assert exec_.parent.span_id == tool.context.span_id

    # GenAI attributes.
    assert llm.attributes["gen_ai.request.model"] == "scripted-model"
    assert llm.attributes["gen_ai.provider.name"] == "anthropic"
    assert llm.attributes["gen_ai.response.finish_reasons"] == ("tool_use",)
    assert llm.attributes["gen_ai.usage.input_tokens"] == 11
    assert llm.attributes["gen_ai.usage.output_tokens"] == 7
    assert tool.attributes["gen_ai.tool.name"] == "web1__deploy"
    assert tool.attributes["runspec.tool.autonomy_decision"] == "run"
    assert turn.attributes["runspec.turn.kind"] == "chat"
    # Turn-total usage is summed across both iterations.
    assert turn.attributes["gen_ai.usage.input_tokens"] == 22

    # Privacy: capture_content off ⇒ no prompt / completion / argument text.
    assert not any("gen_ai.prompt" in sp.attributes for sp in llm_spans)
    assert not any("gen_ai.completion" in sp.attributes for sp in llm_spans)
    assert "gen_ai.tool.call.arguments" not in tool.attributes


@otel_only
def test_capture_content_attaches_and_scrubs(exporter, monkeypatch):
    """With capture_content on, prompt/completion/argument text is attached — and
    a secret-looking tool argument is redacted before it leaves the machine."""
    monkeypatch.setattr(
        config_mod, "read_config", lambda: {"llm": {"provider": "anthropic"}}
    )
    obs.init_tracing_for_test(exporter, capture_content=True)

    s = _chat_stub()
    tc = ToolCall(
        id="t1", name="web1__deploy", input={"password": "sekret", "host": "web1"}
    )
    adapter = _ScriptedAdapter(
        [("here we go", "tool_use", [tc]), ("finished", "end_turn")]
    )
    asyncio.run(s._agentic_chat_turn("chat1", "deploy it", adapter))
    obs.shutdown()

    spans = {sp.name: sp for sp in exporter.get_finished_spans()}
    llm = spans["chat scripted-model"]
    tool = spans["execute_tool web1__deploy"]

    assert "gen_ai.prompt" in llm.attributes  # the outbound messages
    assert "gen_ai.completion" in llm.attributes
    args = tool.attributes["gen_ai.tool.call.arguments"]
    assert "web1" in args  # non-secret kept
    assert "sekret" not in args  # secret scrubbed
    assert "«redacted»" in args


@otel_only
def test_tool_span_names_real_tool_and_subcommand(exporter):
    """A search-mode run_runnable span is named for the real runnable + subcommand
    (display_name/command), not the wrapper — the reported "everything shows as
    run_runnable" issue. The raw wrapper stays on runspec.tool.wrapper."""
    obs.init_tracing_for_test(exporter, capture_content=False)
    with obs.tool_span(
        "run_runnable", "chat1", decision="run", display_name="cron", command="add"
    ):
        pass
    obs.shutdown()

    span = exporter.get_finished_spans()[0]
    assert span.name == "execute_tool cron add"
    assert span.attributes["gen_ai.tool.name"] == "cron"
    assert span.attributes["tool.name"] == "cron"
    assert span.attributes["runspec.tool.command"] == "add"
    assert span.attributes["runspec.tool.wrapper"] == "run_runnable"


@otel_only
def test_tool_span_falls_back_to_raw_name(exporter):
    """No display_name (a direct / per_host / dedup tool) ⇒ the span keeps the raw
    tool name and adds no wrapper attribute."""
    obs.init_tracing_for_test(exporter, capture_content=False)
    with obs.tool_span("web1__deploy", "chat1", decision="run"):
        pass
    obs.shutdown()

    span = exporter.get_finished_spans()[0]
    assert span.name == "execute_tool web1__deploy"
    assert span.attributes["gen_ai.tool.name"] == "web1__deploy"
    assert "runspec.tool.wrapper" not in span.attributes
    assert "runspec.tool.command" not in span.attributes


@otel_only
def test_capture_content_full_by_default(exporter):
    """No max_content_chars ⇒ the whole prompt/completion is captured, not a
    4000-char slice — the trace is faithful (the reported LangSmith issue)."""
    obs.init_tracing_for_test(exporter, capture_content=True)  # max_content_chars=0
    big_user = "X" * 12000  # well past the old 4000-char attribute cap
    with obs.llm_span(model="m") as _llm:
        obs.add_prompt([{"role": "user", "content": big_user}])
        obs.add_completion("Y" * 9000)
    obs.shutdown()

    llm = exporter.get_finished_spans()[0]
    assert big_user in llm.attributes["gen_ai.prompt"]  # full prompt, no cut
    assert "…[truncated]" not in llm.attributes["gen_ai.prompt"]
    assert len(llm.attributes["gen_ai.completion"]) == 9000


@otel_only
def test_capture_content_ceiling_truncates(exporter):
    """A configured max_content_chars caps each content value (with a marker)."""
    obs.init_tracing_for_test(exporter, capture_content=True, max_content_chars=500)
    with obs.llm_span(model="m") as _llm:
        obs.add_prompt([{"role": "user", "content": "Z" * 5000}])
    obs.shutdown()

    prompt = exporter.get_finished_spans()[0].attributes["gen_ai.prompt"]
    assert prompt.endswith("…[truncated]")
    assert len(prompt) == 500 + len("…[truncated]")


# ── 5. send_test_trace() — the operator's "does telemetry work" probe ─────────


def test_send_test_trace_disabled_returns_reason():
    """With tracing off it can't emit — reports ok=False with a reason, no raise."""
    obs.reset_for_test()
    r = obs.send_test_trace()
    assert r["ok"] is False
    assert "not active" in r["reason"]


@otel_only
def test_send_test_trace_emits_connected_tree(exporter):
    """send_test_trace() emits a real turn → llm → tool tree in one trace with the
    same attributes a live turn produces, and reports ok + trace_id + flushed.
    Content off ⇒ no prompt/completion/argument text."""
    obs.init_tracing_for_test(exporter, capture_content=False)
    result = obs.send_test_trace()
    assert result["ok"] is True
    assert result["flushed"] is True
    assert result["capture_content"] is False
    assert len(result["trace_id"]) == 32

    finished = exporter.get_finished_spans()
    spans = {sp.name: sp for sp in finished}
    assert "runspec.agent.turn" in spans
    assert "chat runspec-test-model" in spans
    assert "execute_tool run_runnable" in spans

    turn = spans["runspec.agent.turn"]
    llm = spans["chat runspec-test-model"]
    tool = spans["execute_tool run_runnable"]

    # One connected trace, and the reported trace_id matches the root span.
    assert len({sp.context.trace_id for sp in finished}) == 1
    assert format(turn.context.trace_id, "032x") == result["trace_id"]

    # Parenting under the turn.
    assert llm.parent.span_id == turn.context.span_id
    assert tool.parent.span_id == turn.context.span_id

    # The attributes a live turn produces.
    assert turn.attributes["runspec.turn.kind"] == "test"
    assert llm.attributes["gen_ai.request.model"] == "runspec-test-model"
    assert tool.attributes["gen_ai.tool.name"] == "run_runnable"
    assert tool.attributes["runspec.tool.autonomy_decision"] == "run"
    assert tool.attributes["runspec.tool.operator_decision"] == "approved"

    # Content off ⇒ no text attributes.
    assert "gen_ai.prompt" not in llm.attributes
    assert "gen_ai.completion" not in llm.attributes
    assert "gen_ai.tool.call.arguments" not in tool.attributes


@otel_only
def test_send_test_trace_capture_content_scrubs(exporter):
    """With capture_content on the test trace carries sample prompt/completion and
    scrubbed tool-argument text — so content rendering (and scrubbing) is verified."""
    obs.init_tracing_for_test(exporter, capture_content=True)
    result = obs.send_test_trace()
    assert result["ok"] is True and result["capture_content"] is True

    spans = {sp.name: sp for sp in exporter.get_finished_spans()}
    llm = spans["chat runspec-test-model"]
    tool = spans["execute_tool run_runnable"]
    assert "gen_ai.prompt" in llm.attributes
    assert "gen_ai.completion" in llm.attributes
    args = tool.attributes["gen_ai.tool.call.arguments"]
    assert "world" in args  # non-secret kept
    assert "hunter2" not in args  # secret scrubbed
    assert "«redacted»" in args


# ── 6. metrics — separate OTLP signal (MeterProvider + instruments) ───────────
#
# Network-free via InMemoryMetricReader. Covers the same three pillars as the
# tracing tests: the disabled no-op contract, the recording helpers producing the
# right instruments/attributes, and the reconfigure/test-emit seams.


@pytest.fixture
def metric_reader():
    """A fresh in-memory metric reader wired into observability; torn down after."""
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    yield reader
    obs.reset_metrics_for_test()


def _collect_points(reader):
    """Flatten a reader's metrics into ``{name: [(attributes, point)]}`` where a
    point is a NumberDataPoint (counter) or HistogramDataPoint."""
    out: dict[str, list] = {}
    data = reader.get_metrics_data()
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                pts = out.setdefault(metric.name, [])
                for dp in metric.data.data_points:
                    pts.append((dict(dp.attributes), dp))
    return out


def test_metrics_disabled_is_noop():
    """With metrics off, every recording helper is a no-op and nothing raises."""
    obs.reset_metrics_for_test()
    assert obs.metrics_enabled() is False
    assert obs.init_metrics({"metrics": {"enabled": False}}) is False
    obs.record_tool_run(tool="run_runnable", duration_ms=5, output="ok")
    obs.record_turn(kind="chat", duration_ms=5, usage={"input": 1, "output": 1})
    assert obs.flush_metrics() is False
    assert obs.send_test_metric() == {"ok": False, "reason": "metrics are not active"}


@otel_only
def test_record_tool_run_emits_counter_and_histogram(metric_reader):
    obs.init_metrics_for_test(metric_reader)
    obs.record_tool_run(
        tool="run_runnable",
        decision="confirm",
        operator_decision="approved",
        provenance={"trigger": "nightly", "agent": True, "runbook": ""},
        duration_ms=12.5,
        output="✗ boom",  # error status from the leading marker
        host="web1",
    )
    points = _collect_points(metric_reader)

    runs = points["runspec.tool.runs"]
    assert len(runs) == 1
    attrs, dp = runs[0]
    assert dp.value == 1
    assert attrs["runspec.tool.name"] == "run_runnable"
    assert attrs["runspec.tool.status"] == "error"
    assert attrs["runspec.tool.autonomy_decision"] == "confirm"
    assert attrs["runspec.tool.operator_decision"] == "approved"
    assert attrs["runspec.host"] == "web1"
    assert attrs["runspec.provenance.trigger"] == "nightly"
    assert attrs["runspec.provenance.agent"] is True

    dur = points["runspec.tool.duration"]
    assert len(dur) == 1
    assert dur[0][1].count == 1


@otel_only
def test_record_tool_run_status_from_output(metric_reader):
    obs.init_metrics_for_test(metric_reader)
    obs.record_tool_run(tool="a", duration_ms=1, output="all good")
    obs.record_tool_run(tool="a", duration_ms=1, output="⏹ stopped")
    statuses = sorted(
        attrs["runspec.tool.status"]
        for attrs, _ in _collect_points(metric_reader)["runspec.tool.runs"]
    )
    assert statuses == ["cancelled", "ok"]


@otel_only
def test_record_turn_emits_turn_and_tokens(metric_reader):
    obs.init_metrics_for_test(metric_reader)
    obs.record_turn(
        kind="chat",
        duration_ms=100.0,
        usage={"input": 42, "output": 17},
        status="ok",
    )
    points = _collect_points(metric_reader)

    turns = points["runspec.agent.turns"]
    assert len(turns) == 1 and turns[0][1].value == 1
    assert turns[0][0]["runspec.turn.kind"] == "chat"
    assert turns[0][0]["runspec.turn.status"] == "ok"

    assert points["runspec.agent.turn.duration"][0][1].count == 1

    tokens = {
        attrs["gen_ai.token.type"]: dp.value
        for attrs, dp in points["runspec.agent.turn.tokens"]
    }
    assert tokens == {"input": 42, "output": 17}


@otel_only
def test_reconfigure_metrics_round_trip():
    """reconfigure_metrics swaps the live MeterProvider (init_metrics alone can't —
    it's idempotent) and leaves a clean no-op when re-inited disabled."""
    obs.reset_metrics_for_test()
    assert obs.metrics_enabled() is False
    cfg1 = {
        "metrics": {
            "enabled": True,
            "endpoint": "http://127.0.0.1:4318/v1/metrics",
            "export_interval": 3600,
        }
    }
    assert obs.reconfigure_metrics(cfg1) is True
    assert obs.metrics_enabled() is True
    first = obs._METER_PROVIDER
    cfg2 = {
        "metrics": {
            "enabled": True,
            "endpoint": "http://127.0.0.1:9999/v1/metrics",
            "export_interval": 3600,
        }
    }
    assert obs.reconfigure_metrics(cfg2) is True
    assert obs._METER_PROVIDER is not first
    assert obs.reconfigure_metrics({"metrics": {"enabled": False}}) is False
    assert obs.metrics_enabled() is False
    assert obs._METER_PROVIDER is None
    obs.reset_metrics_for_test()


@otel_only
def test_send_test_metric_emits_all_instruments(metric_reader):
    obs.init_metrics_for_test(metric_reader)
    result = obs.send_test_metric()
    assert result["ok"] is True
    assert "runspec.tool.runs" in result["instruments"]
    names = set(_collect_points(metric_reader))
    assert {"runspec.tool.runs", "runspec.agent.turns"} <= names


# ── resource identity (service.name / instance.id / host / user) ──────────────


@otel_only
def test_build_resource_attributes(monkeypatch):
    """The shared resource names the app (service.name), distinguishes the instance
    (service.instance.id), and stamps host + user — so multiple consoles don't
    collide into one OTLP time series."""
    monkeypatch.setattr(obs, "_hostname", lambda: "consolebox")
    monkeypatch.setattr(obs, "_username", lambda: "alice")

    r = obs._build_resource({"service_name": "jason-console", "label": "ops-1"})
    a = dict(r.attributes)
    assert a["service.name"] == "jason-console"
    assert a["service.instance.id"] == "ops-1"  # operator label wins
    assert a["host.name"] == "consolebox"
    assert a["process.owner"] == "alice"

    # No label ⇒ instance id falls back to the hostname (still one series per box).
    r2 = obs._build_resource({"service_name": "x", "label": ""})
    assert dict(r2.attributes)["service.instance.id"] == "consolebox"


@otel_only
def test_build_resource_omits_unreadable_host_user(monkeypatch):
    """Host / user that can't be read are simply omitted; instance id still resolves."""
    monkeypatch.setattr(obs, "_hostname", lambda: "")
    monkeypatch.setattr(obs, "_username", lambda: "")
    a = dict(obs._build_resource({"service_name": "svc", "label": ""}).attributes)
    assert a["service.name"] == "svc"
    assert "host.name" not in a
    assert "process.owner" not in a
    assert a["service.instance.id"] == "runspec-console"  # last-resort fallback


# ── keychain-backed export credentials (mirrors [[mcp.servers]] credential) ────


def test_read_telemetry_credential_fields(monkeypatch):
    monkeypatch.setattr(
        config_mod,
        "read_config",
        lambda: {
            "telemetry": {
                "credential": "  langsmith  ",
                "credential_header": " x-api-key ",
                "header_credentials": [
                    {"header": "X-Extra", "credential": "e1"},
                    {"bad": 1},  # dropped
                ],
                "metrics": {
                    "credential": "grafana",
                    "header_credentials": [
                        {"header": "Authorization", "credential": "g1"}
                    ],
                },
            }
        },
    )
    t = config_mod.read_telemetry()
    assert t["credential"] == "langsmith"
    assert t["credential_header"] == "x-api-key"
    assert t["header_credentials"] == [{"header": "X-Extra", "credential": "e1"}]
    m = t["metrics"]
    assert m["credential"] == "grafana"
    assert m["header_credentials"] == [{"header": "Authorization", "credential": "g1"}]

    # Absent ⇒ blank / empty, on both signals.
    monkeypatch.setattr(config_mod, "read_config", lambda: {})
    t2 = config_mod.read_telemetry()
    assert t2["credential"] == ""
    assert t2["credential_header"] == ""
    assert t2["header_credentials"] == []
    assert t2["metrics"]["credential"] == ""
    assert t2["metrics"]["header_credentials"] == []


def _cred_bridge(creds: dict):
    """A minimal object carrying the real _resolve_telemetry_credentials bound to a
    stub _mcp_credential (id → material), for testing header resolution in
    isolation."""
    s = types.SimpleNamespace()
    s._mcp_credential = lambda cid: creds.get(cid)
    s._resolve_telemetry_credentials = types.MethodType(
        Bridge._resolve_telemetry_credentials, s
    )
    return s


def test_resolve_telemetry_credentials_folds_secrets_both_signals():
    import base64

    creds = {
        # token → raw secret under the custom header (LangSmith x-api-key)
        "ls": {"id": "ls", "kind": "token", "username": "", "secret": "sk-abc"},
        # userpass, no custom header → Authorization: Basic (Grafana Cloud)
        "grafana": {
            "id": "grafana",
            "kind": "userpass",
            "username": "12345",
            "secret": "gtok",
        },
    }
    b = _cred_bridge(creds)
    cfg = {
        "headers": {},
        "credential": "ls",
        "credential_header": "x-api-key",
        "header_credentials": [],
        "metrics": {
            "headers": {},
            "credential": "grafana",
            "credential_header": "",
            "header_credentials": [],
        },
    }
    out = b._resolve_telemetry_credentials(cfg)
    # traces: token under the operator-named header
    assert out["headers"]["x-api-key"] == "sk-abc"
    # metrics: userpass → Basic on Authorization, independent of the trace key
    expect = "Basic " + base64.b64encode(b"12345:gtok").decode()
    assert out["metrics"]["headers"]["Authorization"] == expect


def test_resolve_telemetry_credentials_explicit_header_wins_and_bad_id_skipped():
    creds = {"ls": {"id": "ls", "kind": "token", "secret": "sk-abc"}}
    b = _cred_bridge(creds)
    cfg = {
        # explicit header set → credential must not overwrite it
        "headers": {"x-api-key": "manual-key"},
        "credential": "ls",
        "credential_header": "x-api-key",
        "header_credentials": [{"header": "X-Extra", "credential": "missing"}],
        "metrics": {"headers": {}, "credential": "", "header_credentials": []},
    }
    out = b._resolve_telemetry_credentials(cfg)
    assert out["headers"]["x-api-key"] == "manual-key"  # explicit wins
    assert "X-Extra" not in out["headers"]  # unresolvable id skipped, no raise
