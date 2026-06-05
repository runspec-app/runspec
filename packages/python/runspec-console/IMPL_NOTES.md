# runspec-console — Implementation Notes

Details worth capturing for documentation. Covers behaviour that isn't obvious
from reading the code and that users / operators will ask about.

---

## Background refresh cycle

All discovery and connectivity work runs in a daemon thread — the UI never blocks
waiting for SSH. On app start a single background cycle begins immediately, then
repeats every **30 seconds**.

**Phase 1 — connectivity probes** (fast: `ssh -o ConnectTimeout=3 <host> true`)
- One thread per remote host, run concurrently
- Results written to `_connected_cache`; local is always True
- Fires `runspec:hosts_updated` → App re-fetches host list (dot colours update)

**Phase 2 — runnables discovery** (heavier: `runspec local --format json`)
- One thread per host, run concurrently; unreachable hosts are skipped
- Results written to `_runnables_cache`
- Fires `runspec:runnables_updated` → App re-fetches runnable list

`get_hosts()` and `get_runnables()` both read from cache and return instantly.
On first render the runnable list is empty and dots are grey; both populate within
a few seconds as the first cycle completes.

A fresh cycle is also triggered immediately after `save_jump_hosts()` or
`import_jump_hosts()` so newly added hosts appear without waiting 30 s.

---

## Host connectivity dots

The coloured dot next to each host in the sidebar reflects a cached SSH probe result.

- **Green** — last probe succeeded (`ssh -o ConnectTimeout=3 <host> true` exited 0)
- **Grey** — not yet probed, or last probe failed / timed out
- **Local host** — always green (no probe needed)

**Refresh cadence:** probes run concurrently (one thread per remote host) immediately
on app start, then every **30 seconds** in a background daemon thread. The UI dot
updates automatically when each round completes via a `runspec:hosts_updated` event.

**First load behaviour:** hosts appear grey on the initial render (cache is empty).
Dots flip to green a few seconds later once the first probe round finishes. This is
intentional — `get_hosts()` returns instantly rather than blocking for SSH round-trips.

---

## Streaming (invoke_runnable)

Output from a runnable is streamed line-by-line from a background thread back to
the frontend via `window.evaluate_js(CustomEvent)`. Two events:

- `runspec:output  { id, line, stream }` — one stdout/stderr line
- `runspec:run_end { id, exit_code, duration_ms }` — invocation complete

Both local and SSH-remote runnables use the same streaming path (`executor.py`).
For SSH, the subprocess is `ssh -o BatchMode=yes <host> <remote_runspec_path> [args]`.

**No stdin support** — `BatchMode=yes` is set and the subprocess has no stdin pipe.
Scripts that prompt for confirmation will stall and time out.

---

## Chat / LLM (agentic loop)

**Conversation memory.** The agent keeps **one rolling conversation per app
session** in `Bridge._chat_history` — each `send_chat` appends the user message,
the assistant reply, and any tool_use/tool_result turns, and the next message is
sent with the full prior history (so follow-ups like "restart the first one"
resolve). A `_chat_lock` serialises turns; `_trim_chat_history` caps the history
to the most recent ~60 messages, dropping leading turns only down to a real user
message so tool_use/tool_result pairs are never split. `clear_chat()` (wired to
the Console's **New chat** button) resets it. Caching note: history sits *after*
the tools+system cache breakpoint, so growing it doesn't invalidate the cached
prefix.

**Stopping a turn.** `send_chat` registers a per-`chat_id` cancel `Event` in
`_chat_cancels` (guarded by `self._lock`, *not* `_chat_lock` — cancel must fire
while a turn holds `_chat_lock`). `cancel_chat(chat_id)` sets it; the loop checks
it before each model call, while streaming (breaking the async-for closes the
provider stream), and before running any tool — so Stop takes effect without
further model calls or host actions. A tool already mid-execution is left to
finish (chat-cancel ≠ runnable-cancel). On stop the partial assistant text is
persisted (text-only, so no orphaned tool_use) and a `⏹ Stopped.` token is
dispatched. The run() thread pops the cancel entry in a `finally`.

`send_chat` always runs `_agentic_chat_turn`, which loops up to **10 iterations**:

1. Call `adapter.stream_with_tools(history, tools)` — yields `('text', token)` then
   `('done', ChatResponse)`.
2. Dispatch each `token` as `runspec:token`. On `('done', ...)`, inspect `stop_reason`.
3. If `stop_reason == 'tool_use'`, run each `ToolCall` via `asyncio.to_thread(_run_tool_sync)`,
   dispatch `runspec:tool_start` / `runspec:tool_end`, build the tool-result turns,
   extend `history`, and loop.
4. Otherwise break — model is done.

**Streaming implementation by adapter:**
- **Anthropic / Bedrock** — `stream_with_tools()` iterates raw SSE events from the SDK
  stream (`async for event in stream`). Text deltas yield tokens; `input_json_delta`
  events accumulate JSON strings per block index. `get_final_message()` is called after
  the loop to get the `Message` object needed by `make_tool_turn`.
- **OpenAI** — falls back to `chat()` (non-streaming). Streaming + tool call delta
  accumulation is complex; non-streaming is correct for tool turns.

**Tool schemas** — `_runnables_to_tools()` converts the `_runnables_cache` to
Anthropic-format `input_schema` tool definitions. Tool name: `{host}__{runnable}`,
sanitised to `[a-zA-Z0-9_-]` and truncated to 64 chars. OpenAI adapter converts
`input_schema` → `parameters` via `_to_openai_function()`.

**`_run_tool_sync`** is blocking — safe because it's called via `asyncio.to_thread()`.
Calls `run_local` / `run_remote` directly (they block). Output capped at **16 KB**;
non-zero exit codes prepend `[exit N]` to the output string.

**Tool output cap** — tool results sent to the LLM are truncated at 2 000 chars in the
`runspec:tool_end` event (for the UI); the full ≤16 KB goes into `make_tool_turn`.

**Frontend rendering** — `InvocationBlock` for chat blocks uses `segments: BlockSegment[]`
(interleaved `{ kind: 'text', text }` and `{ kind: 'tool', entry: ToolCallEntry }`) plus
`currentText: string` for the currently-streaming text. When `runspec:tool_start` fires,
`currentText` is flushed to a text segment and a running tool entry is appended.
`runspec:tool_end` marks the entry complete (output stored, `running: false`). `runspec:run_end`
flushes any remaining `currentText` and sets `done: true`. Tool call blocks in the UI show
the runnable name (host prefix stripped), args inline, and a collapsible output section.

**Provider config** lives in `%APPDATA%\runspec-console\runspec_config.toml` under `[llm]`:
  - `provider` — `"anthropic"` | `"openai"` | `"bedrock"` | `"langserve"`
  - `api_key` — for Anthropic/OpenAI; Bedrock proxy token; or LangServe bearer token
  - `model` — defaults: `claude-sonnet-4-6`, `gpt-4o`, `anthropic.claude-sonnet-4-6` (langserve has none — the published chain usually pins it)
  - `base_url` — optional; for OpenAI-compatible endpoints or Bedrock corporate proxy. **Required** for langserve (the `/invoke` endpoint; `/invoke` is appended if absent)
  - `aws_region` — Bedrock only
  - `api_key_command` / `api_key_ttl_ms` — rotating-token vending (Anthropic + LangServe): shell command stdout is the token, cached for the TTL
  - **langserve-only** — `auth_header` (default `Authorization`), `auth_scheme`
    (default `Bearer`; `""` sends the raw token), `input_messages_key`
    (default `messages`), `input_tools_key` (default `tools`), `tools_in_config`
    (default `false` — when true tools go to `config.configurable.<key>`). These
    describe the gateway's `/invoke` input shape; thread through `_get_adapter`
    only when set. The adapter (`adapters/langserve.py`) maps runspec's
    Anthropic-format tools → OpenAI-function dicts and the conversation →
    LangChain-ingestable role dicts (so `make_tool_turn` uses the OpenAI shape),
    parses the returned `AIMessage` (incl. the LangServe `dumpd` 'constructor'
    envelope) for `tool_calls`. `httpx` is imported lazily so the pure mapping
    helpers stay testable without the `[langserve]` extra. `chat()` routes
    through `_build_payload` / `_parse_output` so a plugin can subclass it and
    override just the request/response shaping.

**Adapter plugins** (`adapters/base.py`). `load_adapter` is a registry, not a
hardcoded dispatch. Resolution order: (1) a `module:attr` **dotted path**;
(2) the `_REGISTRY` populated by `register_adapter()` — built-ins register
themselves at import, plugins may register from a module imported via
`[plugins] modules`; (3) **entry points** in the `runspec_console.adapters`
group (`available_providers()` unions the registry + entry-point names).
`ModelAdapter` / `ChatResponse` / `ToolCall` / `register_adapter` /
`available_providers` are re-exported from the top-level `runspec_console`
package as the plugin contract. `Bridge.list_providers()` (built-ins + discovered
plugins, with friendly labels) drives the dynamic provider dropdown;
`Bridge._ensure_plugins_imported` imports `[plugins] modules` once before
resolving. Example template: `examples/console-adapter-plugin/` (not built or
tested with the package). The `[llm]` config can also point `provider` at a
plugin name or dotted path; the Settings UI renders the common
api_key/base_url/model/system fields for any non-built-in provider.
`adapters/testing.py` ships public conformance helpers
(`assert_adapter_contract` / `assert_chat_response` / `assert_tool_turn`) so
plugin authors validate the contract (incl. the `stop_reason="tool_use"`
invariant) without a live gateway. The example template carries an `AGENTS.md`
brief + a Copilot prompt file for AI-agent authoring and a fake-client
conformance test.
  - `system` — optional standing instructions (system prompt). `_get_adapter`
    passes it to the adapter only when non-blank, so adapters keep their
    `DEFAULT_SYSTEM` otherwise. Editable in Settings → LLM; `save_config` clears
    the cached adapter so it applies on the next message.

**Stop in the command bar.** `ConsoleView` derives the in-flight chat id from its
blocks (a `chat` block that isn't `done`) and broadcasts it via a
`runspec:chat_active` window event; `App` tracks it and passes `activeChatId` +
`onStopChat` to `CommandInput`, whose send arrow becomes a red Stop control while
a turn runs (and submit/Enter is gated off so a second turn isn't queued behind
the conversation lock). The running chat block also keeps its own Stop button —
both call `bridge.cancel_chat(id)`.
- Configurable in-app via Settings → General. Provider dropdown shows relevant fields
  only (e.g. AWS Region only appears for Bedrock).

---

## Prompt caching

The agentic loop sends the entire tool-schema list every turn (one entry per
discovered runnable). That block is static within a session, so the Anthropic and
Bedrock adapters apply ephemeral `cache_control` breakpoints via the pure helper
`adapters.base.apply_prompt_caching(kwargs)` before each API call.

Caching is a **prefix match** in render order `tools → system → messages`:
- the **last tool** definition is annotated → caches the whole tools block;
- `system` is sent as a cached text block → caches tools+system together.

Subsequent turns read the prefix at ~0.1× input cost. No behavior change — every
action is still a normal gated tool call. Below the model's minimum cacheable
prefix (~2K tokens on Sonnet 4.6, ~4K on Opus/Haiku) the API silently skips
caching, so small tool sets are unaffected. Confirm hits via the response
`usage.cache_read_input_tokens`. The helper is SDK-free and unit-tested without
`anthropic` installed (`tests/test_prompt_caching.py`).

---

## Production build

Only `--dev` mode is wired up (pywebview connects to Vite dev server on port 5173).
`npm run build` → embedded `dist/` path is not yet configured in `app.py`. Running
without `--dev` will show a blank window.

---

## Schedules

Stored at `%APPDATA%\runspec-console\runspec_schedules.toml` as a TOML array of
`[[schedule]]` entries. No scheduler process exists yet — schedules are persisted
but nothing executes them. `nextRun` is always blank (`—`) until a scheduler is
implemented. Marked as a **server-side / central git repo** feature for a later pass.

---

## File locations (Windows)

| File | Path |
|------|------|
| Hosts config | `%APPDATA%\runspec-console\runspec_hosts.toml` |
| App config (LLM / SSH defaults) | `%APPDATA%\runspec-console\runspec_config.toml` |
| Schedules | `%APPDATA%\runspec-console\runspec_schedules.toml` |
| Local venv logs | `{venv_root}\logs\{runnable}.log` |
| Remote logs | fetched via one-shot SSH cat on demand |
