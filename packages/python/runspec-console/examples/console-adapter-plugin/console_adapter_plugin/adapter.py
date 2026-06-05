"""The proprietary adapter — the only part that lives in your private repo.

Two ways to implement it:

A. Implement ``ModelAdapter`` from scratch when the gateway is nothing like an
   Anthropic/OpenAI/LangServe API (see the commented skeleton at the bottom).

B. Subclass the bundled ``LangServeAdapter`` when the gateway is "LangServe-ish"
   but not vanilla — inherit the auth, token rotation, and agent-loop plumbing
   and override only the request/response shaping. That is what this example
   shows.

The contract is the SDK-free trio ``ModelAdapter`` / ``ChatResponse`` /
``ToolCall``; an adapter factory receives the kwargs runspec-console threads
from the ``[llm]`` config (api_key, model, system, base_url, api_key_command,
api_key_ttl_ms, and any custom keys you read yourself).
"""

from __future__ import annotations

from typing import Any

from runspec_console import ToolCall
from runspec_console.adapters.langserve import LangServeAdapter


class MyCorpAdapter(LangServeAdapter):
    """Adapter for MyCorp's internal gateway (a non-vanilla LangServe variant)."""

    def _build_payload(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        # Shape the request exactly as MyCorp's /invoke expects. Start from the
        # inherited default and adjust, or build it from scratch. Example: the
        # gateway wants the model name echoed and tools under a custom key.
        payload = super()._build_payload(messages, tools)
        payload["input"]["model"] = self.model
        return payload

    def _parse_output(
        self, output: Any
    ) -> tuple[str | None, list[ToolCall], dict[str, int]]:
        # Map MyCorp's response onto (text, tool_calls, usage). If it differs from
        # a standard serialised AIMessage, translate it here, e.g.:
        #
        #   calls = [ToolCall(id=c["call_id"], name=c["tool"], input=c["params"])
        #            for c in output.get("actions", [])]
        #   return output.get("reply"), calls, {}
        return super()._parse_output(output)


# ── Skeleton for a from-scratch adapter (no LangServe base) ───────────────────
#
# from runspec_console import ModelAdapter, ChatResponse, ToolCall
# import subprocess, time
#
# class MyCorpAdapter(ModelAdapter):
#     def __init__(self, *, api_key=None, model="", system="",
#                  api_key_command=None, api_key_ttl_ms=0, **kwargs):
#         # Rotating tokens (you get this for free by subclassing LangServeAdapter;
#         # here is the from-scratch equivalent): cache the command's stdout and
#         # re-run it once the TTL lapses.
#         self._static_key = api_key
#         self._cmd = api_key_command
#         self._ttl_s = api_key_ttl_ms / 1000.0
#         self._cached = None
#         self._fetched_at = 0.0
#         ...  # build your client
#
#     def _token(self):
#         if not self._cmd:
#             return self._static_key
#         now = time.monotonic()
#         if self._cached and self._ttl_s > 0 and now - self._fetched_at < self._ttl_s:
#             return self._cached
#         self._cached = subprocess.run(
#             self._cmd, shell=True, capture_output=True, text=True
#         ).stdout.strip()
#         self._fetched_at = now
#         return self._cached
#
#     async def chat(self, messages, tools) -> ChatResponse:
#         # call your gateway with self._token(); return text + tool_calls + stop_reason
#         # stop_reason must be "tool_use" when there are tool calls to run.
#         ...
#
#     async def stream_chat(self, messages, tools):
#         # yield text tokens; ok to call chat() once and yield its text.
#         resp = await self.chat(messages, tools)
#         if resp.text:
#             yield resp.text
#
#     def make_tool_turn(self, response, results):
#         # return the assistant turn + tool-result turns to append to history,
#         # in whatever message format your chat() consumes on the next call.
#         ...
#
#   stream_with_tools() has a correct non-streaming default in the base class.
