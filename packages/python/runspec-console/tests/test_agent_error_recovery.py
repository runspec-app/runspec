"""
test_agent_error_recovery.py — a model-call failure must not end a turn as a bare
"Error".

Yesterday a chat turn that had been running many tool calls ended with just the
word *Error*: the streaming request raised an ``httpx.ReadTimeout`` (which
stringifies to ``""``), the loop surfaced ``⚠ Error: `` and stopped. These tests
cover the three fixes:

  * transient model-call failures (timeouts, dropped connections, 429/5xx) are
    retried in code before any text has streamed;
  * a durable / budget-exhausted failure is surfaced with a *descriptive* message
    (class name + hint), never an empty one;
  * a still-failing turn is handed back to the agent once to analyse and resume
    (the self-recovery catchall).

The pure classifiers/formatters in ``adapters.base`` are unit-tested directly; the
loop behaviour is driven through the real ``_agentic_chat_turn`` with a scripted
adapter that can raise, mirroring test_agent_continuation.py.
"""

from __future__ import annotations

import asyncio
import threading
import types

import pytest

from runspec_console.adapters.base import (
    ChatResponse,
    describe_exception,
    is_transient_model_error,
    retry_delay,
)
from runspec_console.bridge import Bridge
from tests.session_stub import agentic_shim, current as _S, install_sessions


# ── pure helpers ──────────────────────────────────────────────────────────────


class ReadTimeout(Exception):
    """Stands in for ``httpx.ReadTimeout`` — matched by class *name*, and its
    ``str()`` is empty, exactly like the real one that showed up as "Error"."""


def test_describe_exception_never_empty_for_messageless_error():
    # A bare, message-less timeout still yields a readable, hint-bearing string.
    class ReadTimeout(Exception):
        pass

    out = describe_exception(ReadTimeout())
    assert out  # never empty — the whole point
    assert "ReadTimeout" in out
    assert "read timeout" in out.lower()


def test_describe_exception_includes_message_and_module():
    out = describe_exception(ValueError("bad model"))
    assert out == "ValueError: bad model"


def test_is_transient_matches_by_name_and_status_and_chain():
    assert is_transient_model_error(ReadTimeout())  # name match

    class ConnectError(Exception):
        pass

    assert is_transient_model_error(ConnectError())

    # durable errors are not retried
    assert not is_transient_model_error(ValueError("nope"))
    assert not is_transient_model_error(KeyError("k"))

    # a 5xx status on the exception counts as transient…
    class ApiError(Exception):
        status_code = 503

    assert is_transient_model_error(ApiError())

    # …but a 4xx (other than 408/409/429) does not
    class BadRequest(Exception):
        status_code = 400

    assert not is_transient_model_error(BadRequest())

    # transient cause reached through the exception chain
    try:
        try:
            raise ReadTimeout()
        except ReadTimeout as inner:
            raise RuntimeError("wrapper") from inner
    except RuntimeError as exc:
        assert is_transient_model_error(exc)


def test_retry_delay_is_exponential_and_capped():
    assert retry_delay(1) == 1.0
    assert retry_delay(2) == 2.0
    assert retry_delay(3) == 4.0
    assert retry_delay(99) == 15.0  # capped
    assert retry_delay(0) == 1.0  # floored to attempt 1


def test_append_recovery_prompt_keeps_alternation():
    # History ending on a tool_result user turn → a synthetic assistant note is
    # inserted first so the reflection user turn still alternates.
    hist = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "calling tool"},
        {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
    ]
    Bridge._append_recovery_prompt(hist, "httpx.ReadTimeout — read timeout")
    roles = [m["role"] for m in hist]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    assert "continue the task" in hist[-1]["content"]
    assert "read timeout" in hist[-1]["content"]

    # History ending on an assistant turn → the reflection is appended directly.
    hist2 = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "done"},
    ]
    Bridge._append_recovery_prompt(hist2, "boom")
    assert [m["role"] for m in hist2] == ["user", "assistant", "user"]


# ── loop behaviour ────────────────────────────────────────────────────────────


class _RaisingAdapter:
    """Walks a list of per-call steps (clamped at the last). Each step is a dict:
    ``{"raise": exc}`` to raise, or ``{"text": str, "stop": str}`` to reply; an
    optional ``"pre_text"`` streams a chunk *before* raising (to exercise the
    'already streamed' guard)."""

    model = "raiser"

    def __init__(self, steps: list[dict]) -> None:
        self._steps = steps
        self.calls = 0

    async def stream_with_tools(self, messages, tools):
        step = self._steps[min(self.calls, len(self._steps) - 1)]
        self.calls += 1
        if step.get("pre_text"):
            yield ("text", step["pre_text"])
        if "raise" in step:
            raise step["raise"]
        if step.get("text"):
            yield ("text", step["text"])
        yield (
            "done",
            ChatResponse(
                text=None,
                tool_calls=[],
                stop_reason=step.get("stop", "end_turn"),
                _raw=None,
            ),
        )

    def make_tool_turn(self, response, results):
        return []


def _stub(max_net: int = 4, max_self: int = 1):
    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._chat_cancels = {}
    install_sessions(s)
    s._config = {
        "llm": {"max_network_retries": max_net, "max_self_recoveries": max_self}
    }
    s.tokens = []
    s.persisted = {}

    s.get_config = lambda: s._config
    s._runnables_to_tools = lambda *a, **k: []
    s._tool_mode = lambda: "dedup"
    s._chat_index_text = lambda: ""
    s._messages_chars = lambda _msgs: 0
    s._trace = lambda *a, **k: None
    s._play_sound = lambda *a, **k: None
    s._audit_chat_turn = lambda *a, **k: None
    s._window_is_foreground = lambda: True
    s._usage_from_response = lambda _r: {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_creation": 0,
    }

    def dispatch(event, payload):
        if event == "runspec:token":
            s.tokens.append(payload["token"])

    s._dispatch = dispatch

    def persist(session, message, assistant, tools, usage, model):
        s.persisted = {"message": message, "assistant": assistant}

    s._persist_conversation_turn = persist

    async def gated_run(
        tool_name, tool_input, chat_id, stage=None, on_decision=None, headless=False
    ):
        return "ok"

    s._gated_run_tool_async = gated_run

    # _append_recovery_prompt is a @staticmethod — assign the plain function (no
    # self), same as the real attribute lookup on a Bridge instance.
    s._append_recovery_prompt = Bridge._append_recovery_prompt
    for meth in (
        "_max_continuations",
        "_max_tool_rounds",
        "_max_parallel_tools",
        "_parallel_safe_call",
        "_max_network_retries",
        "_max_self_recoveries",
    ):
        setattr(s, meth, types.MethodType(getattr(Bridge, meth), s))
    s._agentic_chat_turn = agentic_shim(s)
    return s


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Zero out the retry backoff so these tests don't actually sleep."""
    monkeypatch.setattr("runspec_console.bridge_turn.retry_delay", lambda *a, **k: 0)


ERROR = "⚠ Error:"
RETRY = "retrying"
RECOVER = "Trying to recover"


def test_transient_error_is_retried_then_succeeds():
    """A read timeout on the first call is retried in code; the reply then streams
    normally with no error surfaced."""
    s = _stub(max_net=4)
    adapter = _RaisingAdapter(
        [{"raise": ReadTimeout()}, {"text": "the answer", "stop": "end_turn"}]
    )

    asyncio.run(s._agentic_chat_turn("c1", "question", adapter))

    assert adapter.calls == 2  # failed once, retried, succeeded
    assert any(RETRY in t for t in s.tokens)  # operator saw a retry notice
    assert "the answer" in "".join(s.tokens)
    assert not any(ERROR in t for t in s.tokens)  # never a bare error


def test_transient_retries_exhausted_surfaces_descriptive_error():
    """When every call times out, the budget is spent and the turn ends with a
    *descriptive* error (not a bare 'Error'), self-recovery disabled."""
    s = _stub(max_net=2, max_self=0)
    adapter = _RaisingAdapter([{"raise": ReadTimeout()}])  # always fails

    asyncio.run(s._agentic_chat_turn("c1", "question", adapter))

    # initial call + 2 retries
    assert adapter.calls == 3
    assert sum(RETRY in t for t in s.tokens) == 2
    # the surfaced error names the class + hint — never empty
    err = next(t for t in s.tokens if ERROR in t)
    assert "ReadTimeout" in err


def test_durable_error_is_not_retried():
    """A non-transient error (bad request) is not retried at the network layer."""
    s = _stub(max_net=4, max_self=0)
    adapter = _RaisingAdapter([{"raise": ValueError("bad request")}])

    asyncio.run(s._agentic_chat_turn("c1", "question", adapter))

    assert adapter.calls == 1  # no network retry
    assert not any(RETRY in t for t in s.tokens)
    assert any("ValueError: bad request" in t for t in s.tokens)


def test_self_recovery_reflects_and_resumes():
    """After the network budget is spent, the turn is handed back to the agent once
    to analyse and resume — a reflection turn is injected and the next call
    succeeds."""
    s = _stub(max_net=0, max_self=1)
    adapter = _RaisingAdapter(
        [{"raise": ReadTimeout()}, {"text": "recovered result", "stop": "end_turn"}]
    )

    asyncio.run(s._agentic_chat_turn("c1", "long task", adapter))

    assert adapter.calls == 2  # errored, then the recovery attempt succeeded
    assert any(RECOVER in t for t in s.tokens)
    # a reflection user turn was injected into the history
    assert any(
        m["role"] == "user" and "continue the task" in str(m["content"])
        for m in _S(s).history
    )
    assert "recovered result" in "".join(s.tokens)


def test_self_recovery_budget_exhausted_ends_with_error():
    """If recovery also fails and the budget is spent, the turn ends with a
    descriptive error rather than looping forever."""
    s = _stub(max_net=0, max_self=1)
    adapter = _RaisingAdapter([{"raise": ValueError("still broken")}])  # always fails

    asyncio.run(s._agentic_chat_turn("c1", "task", adapter))

    # initial call + one recovery attempt
    assert adapter.calls == 2
    assert any(RECOVER in t for t in s.tokens)
    assert any("ValueError: still broken" in t for t in s.tokens)


def test_partial_stream_then_error_is_not_retried_as_network():
    """If text already streamed before the failure, the *call* is not retried
    (that would double the reply); it falls through to self-recovery instead."""
    s = _stub(max_net=4, max_self=0)
    adapter = _RaisingAdapter([{"pre_text": "half a sentence", "raise": ReadTimeout()}])

    asyncio.run(s._agentic_chat_turn("c1", "q", adapter))

    assert adapter.calls == 1  # no network retry despite being transient
    assert not any(RETRY in t for t in s.tokens)
    assert "half a sentence" in "".join(s.tokens)  # shown exactly once
    assert any(ERROR in t for t in s.tokens)
