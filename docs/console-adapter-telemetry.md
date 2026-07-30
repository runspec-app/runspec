# Telemetry for a custom adapter (agent guide)

This is a self-contained guide for making a **custom model adapter**
[telemetry](console-telemetry.md)-friendly. It's written so you can hand it
straight to a coding agent — the last section is a copy-paste brief.

## The short version

**A custom adapter needs no extra code to be traced.** When
[telemetry](console-telemetry.md) is enabled, runspec-console creates the spans
— one trace per agent turn, `turn → llm → tool` — *around* your adapter's
`stream_with_tools` call, and sets the standard OpenTelemetry GenAI attributes
itself. An adapter that simply satisfies the `ModelAdapter` contract is fully
traced without knowing telemetry exists.

There are three tiers of involvement; only the third is optional adapter work:

| Tier | What | Adapter work |
|---|---|---|
| 1. Tracing works | Spans, tree, model name, durations | **None** |
| 2. Accurate tokens / finish reason | LLM span's `gen_ai.usage.*` + `finish_reasons` | Already part of the adapter contract — surface usage on the response |
| 3. Provider-specific detail | Extra span attributes, prompt/completion text | Optional, no-op-safe helpers |

## Tier 2 — surface usage on the response (already the contract)

The console reads token counts and the finish reason off the `ChatResponse` your
adapter returns. For the LLM span's `gen_ai.usage.input_tokens` /
`output_tokens` (and the cache split) to be populated, the response's `_raw`
must expose a `.usage` with the provider's token fields, and `stop_reason` must
be set. This is normal adapter behaviour, not telemetry-specific — if usage
isn't present, the span still forms correctly and the token attributes just read
`0` (no error).

Confirm the exact field names against the **installed** package rather than
guessing (see the brief below) — the console's usage reader accepts the common
spellings (`input_tokens`/`prompt_tokens`, `output_tokens`/`completion_tokens`,
`cache_read_input_tokens`, `cache_creation_input_tokens`).

## Tier 3 — enrich the live span (optional)

While your adapter's `stream_with_tools` runs, the LLM span is the current
OpenTelemetry span. So an adapter can add provider-specific detail — a gateway
request id, a response id — or, when the operator has turned on
`capture_content`, the completion text, by calling the helpers on
`runspec_console.observability`:

```python
from runspec_console import observability

# inside the adapter, after the provider responds:
observability.set_attributes(**{
    "gen_ai.response.id": response_id,
    "mycorp.gateway.request_id": request_id,
})
observability.add_completion(text)   # honours capture_content; no-op when off
```

Every helper is a **complete no-op** when telemetry is disabled or the `otel`
extra isn't installed, so call them unconditionally — your adapter never needs
`opentelemetry` as a dependency, and this keeps `ChatResponse` SDK-free
(enrichment is a side channel, not a new response field).

## Requirements

The `observability` enrichment export requires **runspec-console ≥ 0.218.0**.
The reliable capability check is simply that the import succeeds:

```python
from runspec_console import observability   # only present on ≥ 0.218.0
```

## Copy-paste brief for an agent

> **Task: make our custom runspec-console model adapter telemetry-friendly.**
>
> Read this page and https://runspec.app/console-telemetry/ first. `runspec-console`
> is pip-installed; verify the **real** API by introspecting the installed package
> (don't code against a paraphrase):
>
> ```bash
> python - <<'PY'
> import inspect
> from runspec_console import observability          # public top-level export
> from runspec_console.adapters.base import ChatResponse, ToolCall, ModelAdapter
> from runspec_console.bridge_invoke import InvokeMixin
>
> for fn in (observability.current_span, observability.set_attributes,
>            observability.add_prompt, observability.add_completion):
>     print(fn.__name__, inspect.signature(fn))
> print(inspect.getsource(ChatResponse))
> print(inspect.getsource(InvokeMixin._usage_from_response))  # exact usage fields
> PY
> # To read the files instead:
> #   python -c "import runspec_console,os;print(os.path.dirname(runspec_console.__file__))"
> # then open observability.py, adapters/base.py, bridge_invoke.py there.
> ```
>
> Rules:
>
> 1. Telemetry (OTLP tracing) needs **no** adapter changes to function — the
>    console creates the `turn → llm → tool` spans around `stream_with_tools`.
>    Do **not** add span-creation code.
> 2. Ensure the adapter returns a `ChatResponse` whose `_raw.usage` exposes
>    `input_tokens` and `output_tokens` (plus `cache_read_input_tokens` /
>    `cache_creation_input_tokens` if the provider reports them), and that
>    `stop_reason` is correct (`"tool_use"` when tool calls are present; a length
>    spelling like `"max_tokens"` on a cutoff; else `"end_turn"`). Confirm the
>    exact field names from `_usage_from_response` above. If usage isn't on
>    `_raw.usage`, token attributes silently read `0` (no error) — fix only that.
> 3. (Optional) For provider-specific span detail, or completion/prompt text when
>    `capture_content` is enabled, call the no-op-safe helpers unconditionally:
>    `from runspec_console import observability` then
>    `observability.set_attributes(**{...})` / `observability.add_completion(text)`.
>    **Never** import `opentelemetry` directly and add no new dependency — the
>    helpers are no-ops when telemetry is off or the `otel` extra isn't installed.
> 4. Adapter code only — don't touch console config or the span wiring.
>
> Requires runspec-console ≥ 0.218.0; treat `from runspec_console import
> observability` importing successfully as the capability check.

## See also

- [Custom model adapters](console-adapters.md) — the full adapter contract.
- [Telemetry — OpenTelemetry tracing](console-telemetry.md) — enabling tracing,
  backends, and the span/attribute map.
