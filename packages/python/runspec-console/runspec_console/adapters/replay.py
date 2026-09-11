"""replay.py — record / replay adapters for offline tool-selection evals.

An LLM eval in CI means a real API key, real tokens, and stochastic pass/fail on
every push — the thing that kills these if done naively. Record/replay defuses
all three for the regression tier:

* :class:`RecordingAdapter` wraps a *real* adapter, forwards every call to it, and
  captures each ``ChatResponse`` into a per-case script. A maintainer runs it
  once (``--record``) after intentionally changing a prompt, a runnable
  description, the catalog index, or ``tool_visibility``.
* :class:`ReplayAdapter` serves those canned responses back with **no network and
  no key** — so CI is free, deterministic, and the *fixture diff* becomes the
  review artifact ("my catalog-index change flipped tool selection on these 3
  cases" shows up in the PR).

Both satisfy the :class:`~runspec_console.adapters.base.ModelAdapter` contract and
carry a ``begin(case_id)`` hook the eval harness calls to key the script /
sequence per case (a case can span several turns in search mode). Replay cannot
catch live *model* drift — it only proves the model-at-record-time chose right —
so it pairs with a scheduled live sweep (see ``evals.py``).

The script is plain JSON (``save_script`` / ``load_script``): a dict of
``case_id -> [turn, …]`` where each turn is ``{"text", "tool_calls":
[{"name", "input"}], "stop_reason"}``. Provider-agnostic on purpose, so a script
recorded against one provider replays under another.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from .base import ChatResponse, ModelAdapter, ToolCall

# A recorded/scripted turn, provider-agnostic.
#   text:        assistant free text for the turn ("" if none)
#   tool_calls:  [{"name": str, "input": dict}, …]
#   stop_reason: adapter stop reason (defaults to tool_use/end_turn by presence)
ScriptedTurn = dict[str, Any]
Script = dict[str, list[ScriptedTurn]]


def _response_from_turn(turn: ScriptedTurn, key: str, step: int) -> ChatResponse:
    raw_calls = turn.get("tool_calls") or []
    tool_calls = [
        ToolCall(
            id=str(tc.get("id") or f"{key}:{step}:{i}"),
            name=str(tc.get("name") or ""),
            input=dict(tc.get("input") or {}),
        )
        for i, tc in enumerate(raw_calls)
    ]
    stop_reason = turn.get("stop_reason") or ("tool_use" if tool_calls else "end_turn")
    return ChatResponse(
        text=turn.get("text") or None,
        tool_calls=tool_calls,
        stop_reason=str(stop_reason),
        _raw=turn,
    )


class ReplayAdapter(ModelAdapter):
    """Serve a pre-recorded script — no network, no API key, deterministic.

    ``begin(case_id)`` selects the sequence and resets the step pointer; each
    ``stream_with_tools`` / ``chat`` call advances one step. Past the end of a
    case's script it yields a benign empty ``end_turn`` response (so a runaway
    harness terminates rather than hanging)."""

    def __init__(self, script: Script | None = None, *, system: str = "") -> None:
        self._script: Script = script or {}
        self.system = system
        self.model = "replay"
        self._key: str = ""
        self._step: int = 0

    def begin(self, case_id: str) -> None:
        self._key = case_id
        self._step = 0

    def _next(self) -> ChatResponse:
        turns = self._script.get(self._key, [])
        if self._step < len(turns):
            turn = turns[self._step]
        else:
            turn = {"text": "", "tool_calls": [], "stop_reason": "end_turn"}
        resp = _response_from_turn(turn, self._key, self._step)
        self._step += 1
        return resp

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ChatResponse:
        return self._next()

    async def stream_chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[str]:
        resp = self._next()
        if resp.text:
            yield resp.text

    async def stream_with_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ):
        resp = self._next()
        if resp.text:
            yield ("text", resp.text)
        yield ("done", resp)

    def make_tool_turn(
        self, response: ChatResponse, results: list[tuple[ToolCall, str]]
    ) -> list[dict[str, Any]]:
        # Provider-agnostic; replay is keyed by step, so message content is inert.
        return [
            {"role": "assistant", "content": response.text or ""},
            {
                "role": "user",
                "content": "\n".join(out for _, out in results) or "(no results)",
            },
        ]


class RecordingAdapter(ModelAdapter):
    """Wrap a real adapter, forward every call, and capture the responses into a
    script keyed by the current case. Delegates ``make_tool_turn`` (and the system
    prompt) to the inner adapter, so a trajectory recorded here replays faithfully."""

    def __init__(self, inner: ModelAdapter, sink: Script | None = None) -> None:
        self._inner = inner
        self.sink: Script = sink if sink is not None else {}
        self._key: str = ""
        self.model = getattr(inner, "model", "recording")

    # Forward the system prompt to the wrapped adapter (the harness sets it).
    @property
    def system(self) -> str:
        return getattr(self._inner, "system", "")

    @system.setter
    def system(self, value: str) -> None:
        if hasattr(self._inner, "system"):
            self._inner.system = value

    def begin(self, case_id: str) -> None:
        self._key = case_id
        self.sink.setdefault(case_id, [])
        if hasattr(self._inner, "begin"):
            self._inner.begin(case_id)

    def _capture(self, resp: ChatResponse) -> None:
        self.sink.setdefault(self._key, []).append(
            {
                "text": resp.text or "",
                "tool_calls": [
                    {"name": tc.name, "input": tc.input} for tc in resp.tool_calls
                ],
                "stop_reason": resp.stop_reason,
            }
        )

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ChatResponse:
        resp = await self._inner.chat(messages, tools)
        self._capture(resp)
        return resp

    async def stream_chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[str]:
        async for token in self._inner.stream_chat(messages, tools):
            yield token

    async def stream_with_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ):
        response: ChatResponse | None = None
        async for kind, payload in self._inner.stream_with_tools(messages, tools):
            if kind == "done":
                response = payload
            yield (kind, payload)
        if response is not None:
            self._capture(response)

    def make_tool_turn(
        self, response: ChatResponse, results: list[tuple[ToolCall, str]]
    ) -> list[dict[str, Any]]:
        return self._inner.make_tool_turn(response, results)


def save_script(script: Script, path: str) -> None:
    """Write a script to ``path`` as pretty JSON (a reviewable fixture)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(script, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def load_script(path: str) -> Script:
    """Read a script written by :func:`save_script`."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
