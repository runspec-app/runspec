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


# ── 1b. config.read_phoenix() — local Phoenix server settings ──────────────────


def test_read_phoenix_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "read_config", lambda: {})
    monkeypatch.setattr(config_mod, "app_dir", lambda: tmp_path)
    p = config_mod.read_phoenix()
    assert p["port"] == 6006
    assert p["auto_start"] is False
    assert p["working_dir"] == str(tmp_path / "phoenix")
    assert p["assistant"] is False  # off by default — keeps the feature local
    assert p["startup_timeout"] == 60.0  # default; slow hosts can raise it
    assert p["env"] == {}


def test_read_phoenix_reads_and_clamps(monkeypatch):
    monkeypatch.setattr(
        config_mod,
        "read_config",
        lambda: {
            "phoenix": {
                "port": 99999,
                "auto_start": True,
                "working_dir": " /srv/px ",
                "assistant": True,
                "startup_timeout": 5000,  # over-range → clamped to 600
                # Only PHOENIX_* keys survive; others are dropped as out of scope.
                "env": {"PHOENIX_ENABLE_MCP_SERVER": "false", "PATH": "/evil"},
            }
        },
    )
    p = config_mod.read_phoenix()
    assert p["port"] == 65535  # clamped to a legal port
    assert p["auto_start"] is True
    assert p["working_dir"] == "/srv/px"
    assert p["assistant"] is True
    assert p["startup_timeout"] == 600.0  # clamped 5–600
    assert p["env"] == {"PHOENIX_ENABLE_MCP_SERVER": "false"}


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
    """reconfigure() is the runtime re-init seam the local-Phoenix route uses:
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
    s._chat_lock = threading.Lock()
    s._chat_history = []
    s._history_turn_ids = []
    s._excluded_turns = set()
    s._chat_cancels = {}
    s._lock = threading.Lock()
    s._run_origin = {}
    s._config = {"llm": {"max_continuations": 3}}
    s.tokens = []
    s.tool_runs = []

    s.get_config = lambda: s._config
    s._runnables_to_tools = lambda *a, **k: []
    s._tool_mode = lambda: "dedup"
    s._chat_index_text = lambda: ""
    s._fold_catalog_index = lambda history, index_text: list(history)
    s._messages_chars = lambda _msgs: 0
    s._trace = lambda *a, **k: None
    s._trim_chat_history = lambda: None
    s._play_sound = lambda *a, **k: None
    s._window_is_foreground = lambda: True
    s._dispatch = lambda event, payload: (
        s.tokens.append(payload["token"]) if event == "runspec:token" else None
    )
    s._persist_conversation_turn = lambda *a, **k: None

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

    # _usage_from_response is a @staticmethod — assign the plain function (no self).
    s._usage_from_response = Bridge._usage_from_response
    for meth in (
        "_agentic_chat_turn",
        "_sync_history_tags",
        "_visible_history",
        "_max_continuations",
        "_max_tool_rounds",
        "_gated_run_tool_async",
    ):
        setattr(s, meth, types.MethodType(getattr(Bridge, meth), s))
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
