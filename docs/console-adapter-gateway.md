# Custom adapter behind a gateway — caching, streaming, model limits (agent guide)

This is a self-contained guide for a **custom model adapter** that reaches the
model through a corporate gateway — typically a LangChain / LangServe proxy in
front of Amazon Bedrock — rather than the Anthropic SDK directly. It's written
so you can hand it straight to a coding agent — the last section is a
copy-paste brief. It complements the
[adapter contract](console-adapters.md) and the
[telemetry guide](console-adapter-telemetry.md).

## The short version

Through a gateway, a model feature only helps if it survives three hops: the
**adapter's wire shape**, the **proxy's message conversion**, and the **Bedrock
API the proxy calls**. Most of what the built-in `anthropic` adapter gets for
free (prompt caching, adaptive thinking, model limits, streaming) is available
on Bedrock — but only if the adapter *emits* it and the proxy *forwards* it.
The console ships the hooks for all of it as of **0.247.0** — `capabilities()`,
`[llm.model_limits]`, the LangServe `cache_markers` flag, and `/stream_events`
streaming — and the `langserve` built-in now streams and (opt-in) emits cache
markers. A **custom** adapter still has to emit the markers and declare the
limits, and the proxy still has to forward them — otherwise the console re-sends
the whole conversation uncached on every tool round and has to guess the model's
output ceiling and context window.

The work splits three ways:

| Where | What | Why it matters |
|---|---|---|
| **Adapter** (your package) | Honest usage incl. cache fields; declared model limits; cache markers; streaming | The console clamps `max_tokens`, sizes the context gauge, and can show cache hits |
| **Proxy** (gateway team) | Deterministic serialisation; cache-point translation; forward stop reason + usage; ideally a raw passthrough route | Without these the markers cost nothing but gain nothing |
| **Console** (runspec-console) | The `capabilities()` hook, `[llm.model_limits]`, `cache_markers`, `/stream_events` streaming | Ships in the console; check the installed version (see Requirements) |

## What survives the proxy path

| Feature | Bedrock | Through a LangChain proxy | Adapter work |
|---|---|---|---|
| Streaming + tool use | Yes | Yes (`/stream_events`) | Implement `stream_with_tools` over the streaming route |
| Prompt caching | Yes — Converse expresses a breakpoint as a `cachePoint` block in `system`, `messages`, and the tool config; InvokeModel takes Anthropic `cache_control` | **Only if the proxy translates** the adapter's markers into `cachePoint`. Verify on your langchain-aws version — don't assume | Emit markers behind a flag; keep serialisation deterministic |
| Adaptive thinking / `effort` | Yes | Yes, via the proxy's `additional_model_request_fields` or equivalent — a proxy-shape decision | Forward `effort` in whatever shape the chain accepts (see the adapters page) |
| Strict tool schemas | Yes | Depends on the proxy's `bind_tools` | Optional |
| Models API (limits, capabilities) | **No** | No | Declare limits per model id instead (`capabilities()`) |
| Native tool search | InvokeModel only, not Converse | **No** — LangChain has no `defer_loading` | None; the console's own search mode stays the mechanism |
| Cache diagnostics | First-party API only | No | Verify caching by diffing consecutive proxy→Bedrock request bodies |

## Tier 1 — honest usage, including the cache split (works today)

The console's usage reader accepts `input_tokens` / `output_tokens` plus
`cache_read_input_tokens` and `cache_creation_input_tokens` on the response's
`_raw.usage`. Bedrock Converse reports the cache split as
`cacheReadInputTokens` / `cacheWriteInputTokens`; LangChain normalises it into
`usage_metadata.input_token_details.cache_read` / `.cache_creation`. Surface
**both** — the context gauge's *cache hit %* and the cost estimate read them,
and a zero `cache_read` across turns is the signal that caching isn't reaching
Bedrock.

```python
usage = {}
um = msg.get("usage_metadata") or {}
usage["input_tokens"] = int(um.get("input_tokens") or 0)
usage["output_tokens"] = int(um.get("output_tokens") or 0)
details = um.get("input_token_details") or {}
if details.get("cache_read"):
    usage["cache_read_input_tokens"] = int(details["cache_read"])
if details.get("cache_creation"):
    usage["cache_creation_input_tokens"] = int(details["cache_creation"])
```

Also surface the **real stop reason** (`stop_reason` / `stopReason` /
`finish_reason` in the message metadata) — the loop's auto-continue depends on
it. This is the existing contract; it's restated here because a gateway that
swallows the finish reason is the single most common adapter bug.

## Tier 2 — declare model limits (`capabilities()`)

The Models API isn't on Bedrock, so the console can't look up a model's output
cap or context window. Instead the adapter **declares** them. Implement
`capabilities()` on your adapter, returning a **`ModelCapabilities`** — the
console reads it by attribute, so a plain `dict` is silently ignored:

```python
from runspec_console.adapters.base import ModelCapabilities

def capabilities(self) -> ModelCapabilities:
    return ModelCapabilities(
        context_window=200_000,      # tokens the model accepts
        max_output_tokens=64_000,    # the model's real output cap
        adaptive_thinking=True,      # send thinking/effort at all?
        cache_markers=True,          # the proxy translates cache breakpoints
        strict_tools=False,
    )
```

Most adapters don't need to override the method at all: the default
`ModelAdapter.capabilities()` builds a `ModelCapabilities` from the
`[llm.model_limits]` table below (forwarded to your constructor as
`model_limits` and stored as `self._model_limits`) via the pure helper
`capabilities_from_limits(self.model, self._model_limits)`. Override it only when
you have a richer source (e.g. your gateway exposes a limits endpoint) — and fall
back to that helper for the table.

The console uses these to **clamp `[llm] max_tokens`** to the model's cap
(so a Haiku-sized ceiling isn't applied to Opus, or the reverse), to size the
context gauge from the real window instead of a substring guess, and to skip
thinking when the model doesn't support it. Values can come from a
`[llm.model_limits]` table keyed by model-id substring — it lives in
`config.toml`, so it rides the config seed / Config Sync and one table covers
every Bedrock model id your gateway exposes:

```toml
[llm]
provider = "mycorp"
model = "anthropic.claude-opus-5"

[llm.model_limits."claude-opus-5"]
context_window = 1_000_000
max_output_tokens = 128_000
adaptive_thinking = true

[llm.model_limits."claude-haiku-4-5"]
context_window = 200_000
max_output_tokens = 64_000
adaptive_thinking = false
```

This shipped in runspec-console **0.247.0**; on an older console a
`capabilities()` method is harmless but unread. Confirm the hook against the
installed package (see the brief) rather than trusting a version number.

## Tier 3 — emit cache markers (only useful once the proxy translates them)

Prompt caching is a prefix match in render order *tools → system → messages*.
The built-in `anthropic` adapter marks the last tool and the system block; the
multi-turn pattern adds a marker on the **last content block of the last
message** so each iteration of the agent loop reads the whole prior
conversation from cache. A gateway adapter should emit the same three markers
in its wire format, behind a **`cache_markers`** constructor flag that defaults
to off — a proxy that doesn't translate them should receive none.

```python
def _build_payload(self, messages, tools):
    payload = super()._build_payload(messages, tools)
    if self.cache_markers:
        msgs = payload["input"]["messages"]
        # system as its own block-content message, marked
        msgs[0] = {"role": "system", "content": [
            {"type": "text", "text": self.system,
             "cache_control": {"type": "ephemeral"}}]}
        # last content block of the last message, marked
        last = msgs[-1]
        if isinstance(last.get("content"), str):
            last["content"] = [{"type": "text", "text": last["content"]}]
        last["content"][-1] = {**last["content"][-1],
                               "cache_control": {"type": "ephemeral"}}
        # last tool, marked
        if payload["input"].get("tools"):
            payload["input"]["tools"][-1]["cache_control"] = {"type": "ephemeral"}
    return payload
```

Three things silently defeat caching through a proxy, all on the adapter's side
of the boundary:

1. **Non-deterministic serialisation.** Sort the tool list by name; keep dict
   key order stable; never interpolate a timestamp or request id into the
   system prompt (the console already keeps its per-turn context date-only for
   this reason).
2. **Rebuilding history.** A `make_tool_turn` that reconstructs the assistant
   turn from `text` + tool calls drops thinking blocks and can re-order fields,
   so the prefix changes byte-for-byte. When the proxy returns the provider's
   raw content blocks, echo them back unchanged (the way the `anthropic`
   adapter reuses `response._raw.content`).
3. **The 4-breakpoint limit and minimum cacheable size** (model-dependent, ~1–4K
   tokens). Below the minimum the API silently skips caching — no error.

## Tier 4 — stream instead of one long `/invoke`

A non-streaming `/invoke` per iteration means a large output ceiling depends on
one long-lived request — exactly what trips a proxy or load-balancer idle
timeout, and the usual reason a ceiling gets set well below the model's cap. The
`langserve` built-in now streams over `/stream_events` by default (with an
`/invoke` fallback if the route is unavailable, `[llm] stream_events`); a custom
adapter should do the same. LangServe exposes `/stream` and `/stream_events`;
implement `stream_with_tools` over one of them, yielding `("text", delta)` per
token and ending with `("done", ChatResponse)` carrying the real stop reason and
usage from the final event. Bedrock Converse streaming puts both in its last
event, so a proxy that streams can forward them.

## What to ask the proxy team

In order of leverage:

1. **A raw passthrough route.** `POST /anthropic/v1/messages` forwarded to
   Bedrock **InvokeModel** unchanged (streaming included), the proxy adding only
   auth. With it the console's built-in `anthropic` adapter works with a
   `base_url`, the custom adapter shrinks to token vending, and caching,
   adaptive thinking, strict tools and (later) native tool search all ride
   through with no LangChain translation in the middle. Many corporate gateways
   already ship this route.
2. **Cache-point translation on the Converse path.** If the LangChain route
   must stay: translate the adapter's `cache_control` markers into Converse
   `cachePoint` blocks in `system`, `messages` and `toolConfig`.
3. **Deterministic serialisation** of the forwarded history, so the prefix the
   adapter sent is the prefix Bedrock sees.
4. **Forward stop reason and usage** on every response, including the streaming
   route's final event, with the cache split intact.

## Verifying

- **Caching:** log two consecutive proxy→Bedrock request bodies and diff them.
  Strip the `cache_control`/`cachePoint` markers first (the moving marker always
  differs); the overlap must be byte-identical up to the previous request's
  end. Then confirm `cache_read_input_tokens` is non-zero from the second
  iteration of a tool turn — the console shows it as *cache hit %* on the
  context gauge and in the usage event.
- **Limits:** with `capabilities()` declared, set `[llm] max_tokens` above the
  model's cap and confirm the request goes out clamped; switch model ids and
  confirm the gauge's window changes.
- **Contract:** keep the `runspec_console.adapters.testing` conformance checks
  in CI, and add one test asserting two consecutive payloads share a
  byte-identical prefix up to the last marker.

## Requirements

Tier 1 needs nothing beyond the existing contract. Tiers 2–4 ship in
runspec-console **0.247.0** — `ModelAdapter.capabilities()`, `[llm.model_limits]`,
the LangServe `cache_markers` flag, and `/stream_events` streaming — so on a
custom adapter they're fully wired. Check the installed package rather than
trusting a version number:

```python
from runspec_console.adapters.base import ModelAdapter
from runspec_console.adapters.langserve import LangServeAdapter
import inspect
print(hasattr(ModelAdapter, "capabilities"))                        # tier 2
print("cache_markers" in inspect.signature(LangServeAdapter).parameters)  # tier 3
```

## Copy-paste brief for an agent

> **Task: make our custom runspec-console model adapter cache-, limit- and
> stream-aware behind our LangChain/Bedrock gateway.**
>
> Read this page and https://runspec.app/console-adapters/ first.
> `runspec-console` is pip-installed; verify the **real** API by introspecting
> the installed package (don't code against a paraphrase):
>
> ```bash
> python - <<'PY'
> import inspect
> from runspec_console.adapters.base import ModelAdapter, ChatResponse, ToolCall
> from runspec_console.adapters.langserve import LangServeAdapter
> from runspec_console.bridge_invoke import InvokeMixin
>
> print(inspect.signature(LangServeAdapter))                      # constructor keys
> print(hasattr(ModelAdapter, "capabilities"))                    # tier 2 hook present?
> print(inspect.getsource(InvokeMixin._usage_from_response))      # exact usage fields
> for name in ("stream_with_tools", "make_tool_turn", "_build_payload", "_parse_output"):
>     print(name, inspect.signature(getattr(LangServeAdapter, name)))
> PY
> # To read the files instead:
> #   python -c "import runspec_console,os;print(os.path.dirname(runspec_console.__file__))"
> # then open adapters/base.py, adapters/langserve.py, bridge_invoke.py there.
> ```
>
> Rules:
>
> 1. **Usage.** Surface `input_tokens`, `output_tokens`, `cache_read_input_tokens`
>    and `cache_creation_input_tokens` on `_raw.usage`, mapped from the gateway's
>    `usage_metadata.input_token_details.cache_read` / `.cache_creation`. Surface
>    the real stop reason (`"tool_use"` with tool calls; a length spelling such as
>    `"max_tokens"` on a cutoff; else `"end_turn"`). Confirm field names from
>    `_usage_from_response` above.
> 2. **Limits.** If `ModelAdapter.capabilities` exists, implement it returning a
>    **`ModelCapabilities`** (not a plain dict — the console attribute-accesses it)
>    with `context_window`, `max_output_tokens`, `adaptive_thinking`,
>    `cache_markers`, `strict_tools` for the configured model id — prefer the
>    `[llm.model_limits]` table via `capabilities_from_limits(self.model,
>    self._model_limits)`, else a per-model-id table in the adapter. Don't hardcode
>    one number for all models.
> 3. **Cache markers.** Behind a `cache_markers` constructor parameter (default
>    off): mark the system block, the last tool, and the last content block of
>    the last message with `{"cache_control": {"type": "ephemeral"}}` in the
>    wire shape the gateway forwards. Sort tools by name; keep serialisation
>    deterministic; in `make_tool_turn` echo the provider's raw content blocks
>    back unchanged when the gateway returns them. Add a test that two
>    consecutive payloads share a byte-identical prefix up to the last marker.
> 4. **Streaming.** Implement `stream_with_tools` over the gateway's
>    `/stream_events` (or `/stream`) route: yield `("text", delta)` per token,
>    end with `("done", ChatResponse)` carrying stop reason and usage from the
>    final event. Keep `chat()` as the non-streaming fallback.
> 5. Adapter code only — don't touch console config or the agent loop. No new
>    dependencies beyond what the adapter already uses. Keep the
>    `runspec_console.adapters.testing` conformance checks passing.
>
> Before relying on rule 3 in production, confirm with the gateway team that
> their LangChain route translates `cache_control` into Bedrock Converse
> `cachePoint` blocks (or offers an InvokeModel passthrough). Until it does, the
> markers are inert; `cache_read_input_tokens` staying at 0 is the tell.

## See also

- [Custom model adapters](console-adapters.md) — the full adapter contract,
  `effort` on the LangServe adapter, registration.
- [Telemetry for a custom adapter](console-adapter-telemetry.md) — the same
  usage fields feed the LLM span's token attributes.
- [Settings & config reference](console-reference.md#llm) — `[llm]` keys,
  `tool_mode`, `tool_index_limit`.
