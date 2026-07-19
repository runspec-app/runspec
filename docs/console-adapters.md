# Writing a model adapter for runspec-console

runspec-console talks to a language model through a small **adapter** interface.
The built-ins (`anthropic`, `openai`, `bedrock`, `langserve`) are adapters; a
white-label or corporate deployment ships **its own** adapter — usually to reach
an internal LLM gateway — and that adapter is also what the console brands itself
against.

This page is the contract an adapter must satisfy, with the pieces that changed
in **0.166.0** (configurable output ceiling + automatic continuation of long
replies) called out explicitly. If you are building or updating a custom adapter,
this is the reference.

Everything you import lives in two modules:

- `runspec_console.adapters.base` — the `ModelAdapter` base class, the
  `ChatResponse` / `ToolCall` data classes, `register_adapter`, and the
  `is_output_truncation` helper.
- `runspec_console.adapters.testing` — a conformance harness
  (`assert_adapter_contract`, `assert_chat_response`, `assert_tool_turn`) you can
  drop into your package's own tests.

---

## The interface

An adapter subclasses `ModelAdapter` and implements four methods:

```python
from runspec_console.adapters.base import (
    ModelAdapter, ChatResponse, ToolCall,
)


class MyCorpAdapter(ModelAdapter):
    def __init__(self, *, base_url=None, api_key=None, model="", max_tokens=8192):
        # Every keyword here can be supplied from [llm] config — see
        # "Configuration" below. Store what you need.
        self.model = model
        self.max_tokens = int(max_tokens) or 8192
        ...

    async def chat(self, messages, tools) -> ChatResponse:
        """One non-streaming round-trip. Returns a ChatResponse (below)."""
        ...

    async def stream_chat(self, messages, tools):
        """Async generator yielding text tokens as they arrive.
        If your gateway can't stream, yield the whole reply once."""
        ...

    async def stream_with_tools(self, messages, tools):
        """Async generator yielding ('text', str) per token, then
        ('done', ChatResponse) at the end. If you don't stream, the base
        class default calls chat() and emits it in one shot — inherit it."""
        ...

    def make_tool_turn(self, response, results) -> list[dict]:
        """Given the assistant response and [(ToolCall, output_str), …],
        return the message turns to append so the next call sees the tool
        results. Shape is whatever your `messages` format expects."""
        ...
```

`messages` is the running conversation (a list of role dicts). `tools` is a list
of tool schemas in Anthropic shape (`{"name", "description", "input_schema"}`);
convert them to your provider's format inside the adapter.

The agentic loop only ever calls `stream_with_tools` for a chat turn, and
`make_tool_turn` after it runs the tools. `chat` / `stream_chat` back them.

---

## `ChatResponse` and `stop_reason` — the part that drives auto-continue

```python
@dataclass
class ChatResponse:
    text: str | None            # the assistant's text, or None on a pure tool turn
    tool_calls: list[ToolCall]  # tool calls the model wants to run (may be empty)
    stop_reason: str            # why the model stopped — see below
    _raw: Any = None            # your native response object (optional)
```

`stop_reason` tells the loop what to do next. **This is the contract that makes
long replies stream out whole instead of being silently cut:**

| Your model stopped because… | Return `stop_reason` = | The loop then… |
|---|---|---|
| it wants to call tools | `"tool_use"` | runs the tool calls |
| it hit the **output-token ceiling** mid-reply | `"max_tokens"` (or any spelling below) | **auto-continues** — re-prompts to finish |
| it finished naturally | `"end_turn"` / `"stop"` | ends the turn |

Two robustness rules the loop follows so you don't have to match one exact
string:

1. **Tool calls run whenever `tool_calls` is non-empty**, regardless of the exact
   `stop_reason`. Returning the tool calls is what matters.
2. **A truncation is detected with `is_output_truncation(stop_reason)`**, which
   accepts every common spelling — `max_tokens`, `length` (OpenAI), `model_length`,
   `max_output_tokens`, `max_completion_tokens`, case-insensitive. Return
   whichever one your provider reports.

```python
from runspec_console.adapters.base import is_output_truncation

# In chat(), after you've parsed the provider's response:
if tool_calls:
    stop_reason = "tool_use"
elif is_output_truncation(provider_finish_reason):
    stop_reason = "max_tokens"          # canonical; any truncation spelling works
else:
    stop_reason = "end_turn"
```

!!! warning "The one mistake that silently breaks long replies"
    If your gateway truncates a reply at the token ceiling but your adapter
    reports `end_turn` (or hardcodes it), the loop treats the partial answer as
    complete and stops — with no marker. **Surface the real finish reason.** For a
    gateway that wraps Bedrock/Anthropic the field is usually
    `response_metadata.stop_reason` / `stopReason`; for OpenAI-shaped it's
    `finish_reason` (`length` = truncated).

When the loop does auto-continue it appends your assistant text plus a short
"continue where you left off" user turn and calls you again, up to
`[llm] max_continuations` (default 3) times; past that it appends a visible
`(response was cut off at the output limit)` marker. You don't implement any of
that — you only need to report `stop_reason` honestly.

---

## `max_tokens` — the output ceiling

Accept a `max_tokens` keyword in `__init__`, store it, and pass it to your
provider on every request. It is a **ceiling**, not a target — bill only for what
the model produces — so a generous value costs nothing on short replies, and the
auto-continue above covers the rare overflow.

- If you declare `max_tokens` on your constructor, the console forwards
  `[llm] max_tokens` to it automatically (see Configuration).
- A sensible default is `8192`. Treat a falsy/zero value as "use the default".

---

## Tool calls

Parse the model's tool calls into `ToolCall(id, name, input)` where `input` is a
**dict** of the arguments:

```python
tool_calls = [
    ToolCall(id=call.id, name=call.name, input=call.arguments_dict)
    for call in provider_tool_calls
]
```

`input` **must be a dict** (the parsed arguments), not a JSON string. If your
gateway returns arguments as a JSON string, `json.loads` it in the adapter. A
tool that receives a non-dict / missing arguments will reject the call — a common
cause of an agent tool "not receiving its arguments" is an adapter that forwards
the raw string.

---

## Configuration: how `[llm]` keys reach your adapter

Console config lives in `config.toml` under `[llm]`:

```toml
[llm]
provider = "mycorp"          # selects your adapter (see Registration)
model = "claude-via-gateway"
base_url = "https://llm.corp/invoke"
max_tokens = 8192
tenant_id = "acme"           # any custom key your __init__ declares
```

The console threads these to your adapter by **inspecting your constructor's
parameter names**: any `[llm]` key that matches a declared keyword argument is
passed in. So to accept a new setting, just add it as a parameter — no allowlist
to edit.

!!! note "Declare real parameters, not `**kwargs`"
    Signature forwarding reads named parameters. If your factory is a function
    that takes only `**kwargs`, nothing is forwarded (the console can't see the
    names). Register the **class itself** as the factory, or a factory whose
    signature lists the parameters. `provider` and `system` are handled specially
    and never forwarded as raw kwargs.

Keys the console always understands (forwarded when present): `api_key`,
`api_key_command` + `api_key_ttl_ms` (a stdout-vending command with a TTL, for
short-lived corporate tokens), `base_url`, `model`, `system`, and `max_tokens`
(for `anthropic` / `bedrock` / `langserve` and any adapter that declares it).
`[llm] max_continuations` is read by the loop, not the adapter.

---

## Registration

The console resolves `provider` from three sources, in order:

1. **Dotted path** — set `provider = "mycorp_console.adapter:MyCorpAdapter"` in
   `[llm]`. No packaging step; points straight at your class.
2. **Entry point** — advertise the `runspec_console.adapters` group in your
   package so installing the wheel is enough:

   ```toml
   [project.entry-points."runspec_console.adapters"]
   mycorp = "mycorp_console.adapter:MyCorpAdapter"
   ```

3. **In-process** — call `register_adapter("mycorp", MyCorpAdapter)` from a module
   the console imports (e.g. one listed in `[plugins] modules`).

A provider name outside the built-in set also signals **white-label branding**:
the console derives its title, docs link, and the package its update check
targets from the distribution that supplies the provider. The full
distribution story — config seeds, theming, branded launcher — is the
[white-label walkthrough](white-label.md).

---

## Building on the LangServe adapter

If your gateway is a LangServe-style `/invoke` endpoint (a LangChain runnable,
commonly Bedrock/Claude underneath), subclass `LangServeAdapter` and override
just the two hooks that describe your gateway's request/response shape — you
inherit auth, token rotation, and all the agent-loop plumbing:

```python
from runspec_console.adapters.langserve import LangServeAdapter

class MyGatewayAdapter(LangServeAdapter):
    def _build_payload(self, messages, tools):
        # Shape the /invoke body for your chain. self.max_tokens is available.
        ...

    def _parse_output(self, output):
        # Return (text, [ToolCall, …], usage_dict) from your chain's output.
        ...
```

The base `chat()` already surfaces the gateway's stop reason (it reads
`stop_reason` / `stopReason` / `finish_reason` from the message metadata and
normalises it), so **if you don't override `chat` you get auto-continue for
free**. If you *do* override `chat`, set `stop_reason` yourself per the table
above.

The base adapter also forwards `max_tokens` into the payload — configurable via
`[llm] max_tokens_in` (`kwargs` (default) / `config` / `input`, i.e. where it
lands in the `/invoke` body) and `[llm] max_tokens_key` (the field name your
chain expects). It sends nothing when `max_tokens` is 0, so an unset value never
disturbs a chain that pins its own ceiling.

---

## Verifying your adapter

Drop the conformance harness into your package's tests — it catches the common
mistakes (wrong return type, a missing `tool_use` stop when tool calls are
present, a malformed tool turn) without a network:

```python
import asyncio
from runspec_console.adapters.testing import (
    assert_adapter_contract, assert_chat_response, assert_tool_turn,
)

def test_contract():
    adapter = MyCorpAdapter(base_url="…", client=FakeClient(canned_response))
    assert_adapter_contract(adapter)                      # structural, no network

    resp = asyncio.run(adapter.chat([{"role": "user", "content": "hi"}], TOOLS))
    assert_chat_response(resp, expect_tool_calls=True)    # validates the response
    assert_tool_turn(adapter.make_tool_turn(resp, [(resp.tool_calls[0], "ok")]))
```

`assert_chat_response` enforces the `stop_reason` contract: it accepts
`tool_use` / `end_turn` / `stop` **and** every truncation spelling, and it fails
if you return tool calls without a `tool_use` stop.

---

## Checklist

- [ ] Subclass `ModelAdapter`; implement `chat`, `stream_chat`,
      `stream_with_tools` (or inherit the default), `make_tool_turn`.
- [ ] Return `ToolCall.input` as a **dict**.
- [ ] Set `stop_reason` to `"tool_use"` when tool calls are present; to
      `"max_tokens"` (or any `is_output_truncation` spelling) on a length cutoff;
      else `"end_turn"`.
- [ ] Accept and use a `max_tokens` constructor parameter.
- [ ] Declare every `[llm]` setting you need as a named constructor parameter
      (not `**kwargs`).
- [ ] Register via entry point, dotted path, or `register_adapter`.
- [ ] Run the `adapters.testing` conformance checks in CI.
