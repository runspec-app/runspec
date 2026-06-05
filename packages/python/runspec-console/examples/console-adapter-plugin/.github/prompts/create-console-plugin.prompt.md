---
mode: agent
description: Build a runspec-console LLM provider plugin for an internal model gateway
---

Your task: implement an LLM **provider adapter** so runspec-console can drive our
internal model gateway. The adapter lives in *this* package; do **not** fork or
modify runspec-console. Read `AGENTS.md` in this repo for the full contract — it
is the source of truth. Follow it exactly.

Inputs I will give you (ask if missing):
- the gateway endpoint URL, and
- the existing client/chain code that calls it (so you can infer the wire format).

Steps:

1. **Determine the wire format.** From the client code and, if reachable,
   `GET {url}/input_schema`, `GET {url}/output_schema`, and one sample
   `POST {url}/invoke` (with a tool defined), establish: the request body shape,
   where tools go, the message format, and where the response puts text +
   tool_calls + usage. Don't guess — confirm from the schemas. State any
   "unknown".

2. **Choose the base.** If the gateway is a LangServe `/invoke` endpoint (or
   close), subclass `runspec_console.adapters.langserve.LangServeAdapter` and
   override only `_build_payload` and `_parse_output`. Otherwise implement
   `runspec_console.ModelAdapter` from scratch (chat / stream_chat /
   make_tool_turn).

3. **Honor the invariant.** `chat()` must set `stop_reason="tool_use"` exactly
   when it returns tool calls, else `"end_turn"`. Tools arrive in Anthropic
   format (`name`/`description`/`input_schema`) — convert as needed.

4. **Wire auth, including rotating tokens.** Support a static `api_key` and a
   rotating `api_key_command` + `api_key_ttl_ms` (subclassing LangServeAdapter
   gives this for free via `_token()`; from scratch, replicate the
   run-command-and-cache-for-TTL pattern).

5. **Register via entry point** in `pyproject.toml` under
   `[project.entry-points."runspec_console.adapters"]` with our provider name.

6. **Validate.** Write `tests/test_adapter_conformance.py` driving the adapter
   against a fake HTTP client (no live gateway) and asserting with
   `runspec_console.adapters.testing`: `assert_adapter_contract`,
   `assert_chat_response`, `assert_tool_turn`. Run `pytest` until green.

Deliver: the adapter module, the `pyproject.toml` entry point, and passing
conformance tests. Keep secrets out of code and tests.
