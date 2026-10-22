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

**Send exactly the tools you are given.** The console rebuilds `tools` from the
live fleet on every call: in search mode it is a small fixed set (the
discovery meta-tools plus the hot built-ins), and the runnables are found on
demand. Convert those schemas, but never add tools of your own (from host or
runnable discovery, `runspec local` / `runspec serve`, an MCP client, or the
console's config files), never cache the list between calls, and never drop
any. Extra tools undo search mode and inflate every request's prefix (a host's
58 runnables added ~30k tokens per call in one deployment), and the console
doesn't know them, so a call to one fails.

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

!!! tip "Running `api_key_command` in your own adapter"
    The console resolves `api_key_command` before passing it in: a helper named
    without a full path (`get-key`, `./get-key.exe`) that sits in one of the
    console's own folders (the venv's `Scripts`/`bin`, the folder of the
    console exe) arrives as its absolute path. So even a plain
    `subprocess.run(cmd, shell=True)` finds it when the console was started from
    a shortcut, with no venv on `PATH`. Better still, call
    `runspec_console.adapters.base.run_api_key_command(cmd)`, which also sets
    `$RUNSPEC_PYTHON` / `$RUNSPEC_VENV` and names the folders it searched when
    the helper is missing. Subclassing `LangServeAdapter` does this for you.

### Built-in `anthropic` extras: `cache_ttl`, `fallbacks`, `thinking_display`

Three `[llm]` keys are forwarded **only** to the built-in `anthropic` provider
(the direct Claude API) and applied in `adapters/anthropic.py`, not in the shared
`base` helpers — so the `bedrock` / `langserve` built-ins and plugin adapters
never see them, and each is off unless set:

- `cache_ttl = "1h"` — every prompt-cache breakpoint gets a 1-hour TTL.
- `fallbacks = "default"` — server-side refusal fallback on Fable 5.x / Opus 5.x
  (beta `server-side-fallback-2026-07-01`, sent via `client.beta.messages`).
  Tool calls from a declined attempt are discarded at the stream's `fallback`
  block, and the declined attempt's non-text blocks are not echoed back.
- `thinking_display = "summarized"` — adds `display` to an adaptive `thinking`
  request; `thinking_delta`s are yielded as `("thinking", text)` stream events,
  which the agent loop forwards as `runspec:thinking` for the activity bar and
  the turn card's live "Thinking…" box. When the model call ends, the loop
  attaches that thought to the step it led to: the first tool call's
  `runspec:tool_start` carries `thinking` / `thinking_ms` (saved on the tool's
  transcript entry), or, for the final reply, `runspec:reply_thinking`
  (saved as the turn's `reply_thinking`). A custom adapter may yield the same
  event to get the same UI.

### `effort` — reasoning effort (adaptive extended thinking)

`[llm] effort` sets how hard the model thinks before it answers — Anthropic
**extended thinking**. Named levels, `off` by default:

```toml
[llm]
effort = "medium"   # off (default) | low | medium | high | xhigh | max
```

At a named level the console uses Anthropic's **adaptive** thinking shape
(Claude 4.6+): it sends `thinking = {"type": "adaptive"}` plus
`output_config = {"effort": <level>}`, and the model self-manages its own thinking
depth per request. `effort` is just the dial. Adaptive thinking **counts toward
`max_tokens`** (the output ceiling), so there's no separate budget to reserve —
`max_tokens` is left exactly as you set it. If you turn effort up, consider raising
`max_tokens` too so a deep reply has room after thinking.

Levels: `low` / `medium` / `high` / `xhigh` / `max`. `xhigh` needs Opus 4.7+;
Sonnet 4.6 supports `low`/`medium`/`high`/`max` (no `xhigh`). A level the chosen
model doesn't support surfaces as an API error rather than being silently
downgraded. `medium` is the sensible default for routine ops work — see
[`console-reference.md`](console-reference.md).

!!! note "`off` is handled per model — omitting the parameter isn't "off" everywhere"
    Omitting the `thinking` parameter does **not** mean "no thinking" on every
    model, so `off` is **capability-driven** (from the model's
    [`ModelCapabilities`](#model-capabilities-capabilities-and-llmmodel_limits)):

    * **Opus 4.8 / 4.7** — omitting already means no extended thinking, so `off`
      sends nothing.
    * **Sonnet 5 / Opus 5** — these run *adaptive* thinking at effort **high** when
      the parameter is omitted, so the console sends an explicit
      `thinking = {"type": "disabled"}` — `off` is genuinely off, not the slowest,
      most token-hungry setting.
    * **Fable** — also defaults thinking on but **rejects** an explicit disable, so
      `off` falls back to `effort = "low"` (the cheapest available, logged once) —
      not truly zero. Set `effort` explicitly there if you want a specific depth.

    A gateway/proxy that advertises `adaptive_thinking = false` gets no thinking
    field at all. See the gateway guide's
    [field gotchas](console-adapter-gateway.md#gotchas-found-in-the-field).

    The built-in Anthropic + Bedrock adapters know these facts per model family;
    an operator can override them per model in
    [`[llm.model_limits]`](#model-capabilities-capabilities-and-llmmodel_limits)
    (`thinking_default_on` / `thinking_disable`).

!!! note "This replaced the deprecated `budget_tokens` shape"
    Earlier the console mapped `effort` to a manual `thinking = {"type":
    "enabled", "budget_tokens": N}` budget. That shape is deprecated on Opus/
    Sonnet 4.6 and **rejected with a 400** on Opus 4.8/4.7, Sonnet 5, and Fable 5.
    The adaptive shape above is the current API; `budget_tokens` never appeared in
    your `config.toml` and nothing there needs to change.

It's forwarded to the `anthropic` and `bedrock` built-ins (both talk the
Anthropic SDK, so the request shape is identical), to the `langserve` built-in
(as a pass-through wire field — see below), and to any plugin adapter that
declares an `effort` parameter. Thinking blocks are preserved across tool-call
turns automatically (`make_tool_turn` reuses the raw response content), so effort
composes with tool use. Higher effort trades latency and tokens for deeper
reasoning — leave it `off` for routine, low-latency work.

#### `effort` on the LangServe adapter (and gateway proxies)

The `langserve` adapter can't know your gateway's request shape, so it does **not**
send the Anthropic `thinking`/`output_config` shape. Instead it sends the effort
level as a plain **pass-through string** under an OpenAI-style `reasoning_effort`
field (the adapter already speaks the OpenAI wire format for tools):

```toml
[llm]
provider = "langserve"
effort = "high"                 # sent as-is (the string "high")
effort_key = "reasoning_effort" # rename the field your chain expects
effort_in = "kwargs"            # kwargs (default) | config | input
```

`effort_key` / `effort_in` mirror `max_tokens_key` / `max_tokens_in` — the field
name and which `/invoke` slot it lands in (`kwargs` = the extra-args bucket,
`config` = `config.configurable.<key>`, `input` = alongside the messages). `off`
/ empty sends nothing.

If your gateway instead wants the **native Anthropic adaptive shape** (e.g. it
forwards the body straight to Bedrock's Anthropic API), don't use `effort_key` —
subclass `LangServeAdapter`, override `_build_payload`, and inject it with the
shared helper:

```python
from runspec_console.adapters.base import apply_thinking

class MyGatewayAdapter(LangServeAdapter):
    def _build_payload(self, messages, tools):
        payload = super()._build_payload(messages, tools)
        apply_thinking(payload["kwargs"], self.effort)  # or wherever your chain reads it
        return payload
```

`apply_thinking` is a no-op when effort is off, so the override is always safe.
A **plugin** adapter (dotted path / entry point) gets `[llm] effort` forwarded
automatically the moment its `__init__` declares an `effort` parameter — no
allowlist entry needed. That's the hook a corporate proxy adapter uses.

---

## Model capabilities — `capabilities()` and `[llm.model_limits]`

An adapter can tell the console what the model *behind* it can do, independent of
which provider or proxy carries the request. This matters most when the model is
hidden behind a gateway (a LangChain proxy in front of Bedrock, say) where the
native Anthropic **Models API** isn't reachable — the console can't ask the model
its limits, so the operator declares them.

Override the optional `capabilities()` method to return a `ModelCapabilities`:

```python
from runspec_console.adapters.base import ModelCapabilities

@dataclass
class ModelCapabilities:
    context_window: int | None = None      # total prompt+output token budget
    max_output_tokens: int | None = None    # hard ceiling on one reply
    adaptive_thinking: bool = True           # endpoint accepts thinking/effort
    thinking_default_on: bool = False        # omitting the param runs thinking (high)
    thinking_disable: bool = True            # accepts thinking={"type":"disabled"}
    cache_markers: bool = True               # prompt-cache breakpoints honoured
    strict_tools: bool = False               # endpoint enforces strict tool args
```

`thinking_default_on` / `thinking_disable` drive what `effort = off` sends (see
[the `effort` section](#effort-reasoning-effort-adaptive-extended-thinking)). The
built-in Anthropic + Bedrock adapters seed them from per-family knowledge —
Sonnet 5 / Opus 5 / Fable default thinking **on**, and Fable alone rejects the
explicit disable — which an operator can override per model in the table.

You don't have to implement it. The **default** `ModelAdapter.capabilities()`
reads a new **`[llm.model_limits]`** config table — a mapping of *model-id
substring* → capability fields — that the console forwards to every built-in as a
`model_limits` kwarg (and to any plugin adapter that declares the parameter):

```toml
[llm]
provider = "langserve"
model    = "claude-sonnet-4-6-via-gateway"
max_tokens = 64000

# The gateway hides the model, so declare its limits here. Entries are matched by
# substring and merged broad → specific (the longest matching key wins per field),
# so a broad base + a specific override is the idiomatic shape. An empty "" key is
# a catch-all default.
[llm.model_limits."claude"]
context_window    = 200000
max_output_tokens = 8192

[llm.model_limits."claude-sonnet-4-6"]
max_output_tokens = 64000
adaptive_thinking = true     # this gateway does forward thinking/effort
```

The console uses the returned capabilities to:

1. **Clamp `[llm] max_tokens`** down to `max_output_tokens`, so a generous operator
   setting can't 400 against a model with a lower ceiling (it only ever clamps
   *down* — a smaller `max_tokens` is left alone).
2. Feed **`context_window`** to the context-window gauge instead of the model-id
   substring guess — the authoritative figure when the model is behind a gateway.
3. **Skip thinking / reasoning effort** entirely when `adaptive_thinking` is false
   — so a gateway that drops the field isn't sent a useless (or rejected) one.

The table is authoritative. The `anthropic` built-in *additionally* fills any
numeric limit the table leaves unset from `client.models.retrieve` when the Models
API is reachable (the result is cached, and a failure — the common case behind a
corporate proxy — silently falls back to the table). A plugin adapter with its own
source of truth just overrides `capabilities()` and reads whatever it has, falling
back to `capabilities_from_limits(self.model, self._model_limits)` for the table.

`capabilities()` is called per turn (for the gauge) and once at adapter
construction (for the clamp), so keep it cheap and side-effect-free — cache any
network lookup, as the `anthropic` built-in does.

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

### Streaming over `/stream_events`

`stream_with_tools` drives the LangServe **`/stream_events`** SSE route by default
(`[llm] stream_events = true`): it yields text deltas as they arrive and ends with
a `ChatResponse` carrying the gateway's real stop reason, usage, and tool calls
(read from the terminal `on_chat_model_end` event). This means a long reply streams
out incrementally instead of riding a single long-lived `/invoke` request that a
slow model could hang past an idle-read timeout.

If the `/stream_events` route isn't published (404/405/501), the adapter falls back
to `/invoke` automatically — and disables streaming for the rest of the session so
it isn't re-probed. Set `stream_events = false` to force the non-streaming path.

### Prompt-cache markers (`cache_markers`)

`[llm] cache_markers = true` (default off) adds Anthropic-style ephemeral
`cache_control` breakpoints to the payload, for a gateway that forwards them to an
Anthropic-family model (a proxy in front of Bedrock/Anthropic): the system prompt
becomes a marked content block, the **last** tool carries the marker (caching the
whole tools block), and the **last content block of the last message** carries it
each turn. To keep the cached prefix byte-stable across turns, the tools are
emitted **sorted by name** (discovery can reorder them) and the caller's lists are
copied, never mutated. The adapter also reads
`usage_metadata.input_token_details.cache_creation` (the first-turn write)
alongside `cache_read`, so the console's cache gauge reflects both. Leave it off
for a gateway that doesn't honour `cache_control` — the markers would just be
inert noise.

In **search mode** (`[llm] tool_mode = "search"`) the tool list is deliberately
tiny — three meta-tools instead of every runnable's schema — and the fleet's
**catalogue index** rides in the *system prompt* instead (a one-line-per-runnable
menu, plus the deferred-built-ins menu). So in search mode the system prompt is
the large, static, byte-stable part of the prefix, and the marker on the **system
block** is the one that carries the caching: a gateway that translates only the
system `cache_control` still caches the bulk of the prefix. (Before 0.262.0 the
index was folded into the first user message, where a system-only cache marker
cached almost nothing — see the gateway guide's minimum-cacheable-prefix gotcha.)

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

## Enriching telemetry spans (optional)

For a task-oriented walkthrough you can hand to a coding agent, see
[Telemetry for a custom adapter](console-adapter-telemetry.md).

When [telemetry](console-telemetry.md) is enabled, the console wraps each model
request in an OpenTelemetry span and sets the standard `gen_ai.*` attributes for
you (model, finish reason, token usage). Your adapter can add provider-specific
detail to that span — the LLM span is the current OTel span while your
`stream_with_tools` runs, so no plumbing is needed:

```python
from runspec_console import observability

observability.set_attributes(**{
    "gen_ai.response.id": response_id,
    "mycorp.gateway.request_id": request_id,
})
observability.add_completion(text)   # honours capture_content; no-op when off
```

Every helper is a **complete no-op** when telemetry is off or the `otel` extra
isn't installed, so you can call them unconditionally — your plugin never needs
OpenTelemetry as a dependency. This keeps `ChatResponse` SDK-free: enrichment is
a side channel, not a new response field.

## Behind a gateway: caching, streaming, model limits

If your adapter reaches the model through a corporate proxy (a LangChain /
LangServe gateway in front of Bedrock is the common case), the built-in
adapters' prompt caching, model-limit lookup and streaming don't reach the
model unless the adapter emits them and the proxy forwards them. The
task-oriented walkthrough — what survives the proxy path, the `capabilities()`
hook for declaring a model's output cap and context window, cache markers
behind a `cache_markers` flag, streaming over `/stream_events`, what to ask the
gateway team, and a copy-paste brief — is
[Custom adapter behind a gateway](console-adapter-gateway.md).

## Checklist

- [ ] Subclass `ModelAdapter`; implement `chat`, `stream_chat`,
      `stream_with_tools` (or inherit the default), `make_tool_turn`.
- [ ] Send the `tools` you are passed, converted, with nothing added or cached.
- [ ] Return `ToolCall.input` as a **dict**.
- [ ] Set `stop_reason` to `"tool_use"` when tool calls are present; to
      `"max_tokens"` (or any `is_output_truncation` spelling) on a length cutoff;
      else `"end_turn"`.
- [ ] Accept and use a `max_tokens` constructor parameter.
- [ ] Declare every `[llm]` setting you need as a named constructor parameter
      (not `**kwargs`).
- [ ] Register via entry point, dotted path, or `register_adapter`.
- [ ] Run the `adapters.testing` conformance checks in CI.
