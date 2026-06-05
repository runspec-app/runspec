# Building a runspec-console LLM provider plugin

> Self-contained brief for an AI coding agent (Claude via GitHub Copilot, Claude
> Code, Cursor, etc.). Copy this into your private plugin repo — paste it into
> `.github/copilot-instructions.md` so Copilot loads it automatically, or keep it
> as `AGENTS.md`. Pair it with `.github/prompts/create-console-plugin.prompt.md`.

## Goal

Implement an LLM **provider adapter** so runspec-console can drive your
company's model gateway. The adapter lives in this (private) package; you do
**not** modify or fork runspec-console. Users `pip install` this wheel and pick
the provider in the console.

## The contract — implement `runspec_console.ModelAdapter`

It's dependency-free, so depend on `runspec-console` only for these classes; no
SDKs leak in. Required methods:

| Method | Returns | Notes |
|---|---|---|
| `async def chat(messages, tools)` | `ChatResponse` | one model turn |
| `async def stream_chat(messages, tools)` | `yield` text tokens | cosmetic streaming |
| `def make_tool_turn(response, results)` | `list[dict]` | turns to append to history |

`stream_with_tools()` has a correct non-streaming default — only override it for
true token-streaming with tools.

```python
@dataclass
class ToolCall:      id: str; name: str; input: dict
@dataclass
class ChatResponse:  text: str | None; tool_calls: list[ToolCall]; stop_reason: str
```

**The one rule you must not get wrong:** set `stop_reason="tool_use"` whenever
`chat()` returns tool calls, and only then. The agent loop runs tools iff
`stop_reason == "tool_use"`. Use `"end_turn"` (or `"stop"`) otherwise.

`tools` arrive in **Anthropic format** (`{"name", "description", "input_schema"}`).
Convert them to whatever your gateway expects. The first user turn arrives as
`{"role": "user", "content": "<str>"}`. After a tool round, `make_tool_turn` is
what defines the message format for subsequent turns — keep `chat()` and
`make_tool_turn` consistent with each other.

## Decision: subclass or from scratch

- **Subclass `LangServeAdapter`** (recommended if your gateway is a LangServe
  `/invoke` endpoint or anything close): override only `_build_payload` and
  `_parse_output`. You inherit HTTP transport, auth, **token rotation**, the
  streaming fallback, and `make_tool_turn`. See `console_adapter_plugin/adapter.py`.
- **Implement `ModelAdapter` from scratch** only if the gateway is nothing like
  an HTTP chat API.

## Rotating / short-lived tokens

If your gateway uses bearer tokens that expire (vended by a CLI/STS/`aws`
command), you get rotation **for free when subclassing `LangServeAdapter`** — no
code. The user sets, in the console's `[llm]` config:

```toml
[llm]
provider = "mycorp"
base_url = "https://gateway.corp/my-chain"
api_key_command = "your-token-vendor --print"   # stdout (stripped) is the token
api_key_ttl_ms  = 300000                         # re-run after 5 min; 0 = every call
```

`LangServeAdapter._token()` runs the command, caches the result for the TTL, and
re-runs it when stale. For a **from-scratch** adapter, replicate that: accept
`api_key`, `api_key_command`, `api_key_ttl_ms` in `__init__`, and before each
request return the static key or the cached/refreshed command output.

## Registration (pick one)

1. **Entry point** (recommended) — in `pyproject.toml`:
   ```toml
   [project.entry-points."runspec_console.adapters"]
   mycorp = "console_adapter_plugin.adapter:MyCorpAdapter"
   ```
   `pip install` the wheel → `provider = "mycorp"` resolves to the class.
2. **`register_adapter("mycorp", MyCorpAdapter)`** from a module the user lists
   under `[plugins] modules` in the config.
3. **Dotted path** — `provider = "console_adapter_plugin.adapter:MyCorpAdapter"`.

The factory (your adapter class) receives the kwargs the console threads from
`[llm]`: `api_key`, `model`, `system`, `base_url`, `api_key_command`,
`api_key_ttl_ms`, and any custom keys you read yourself.

## Before you finish — validate the contract

Use the shipped conformance helpers (no live gateway needed; drive the adapter
with a fake HTTP client returning a canned response):

```python
from runspec_console.adapters.testing import (
    assert_adapter_contract, assert_chat_response, assert_tool_turn,
)
```

See `tests/test_adapter_conformance.py` for a complete fake-client example.
Run `pytest`; all green means the adapter is shaped correctly.

## If you don't know the gateway's wire format yet

Determine it first: fetch `GET {url}/input_schema` and `/output_schema`, and do
one `POST {url}/invoke` with a tool defined, to see the real request/response.
Map the response onto `(text, tool_calls, usage)` in `_parse_output`, and the
request in `_build_payload`. Don't guess — confirm shapes from the schemas.
```
