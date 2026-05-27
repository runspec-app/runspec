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
  - `provider` — `"anthropic"` | `"openai"` | `"bedrock"`
  - `api_key` — for Anthropic/OpenAI; or Bedrock proxy token
  - `model` — defaults: `claude-sonnet-4-6`, `gpt-4o`, `anthropic.claude-sonnet-4-6`
  - `base_url` — optional; for OpenAI-compatible endpoints or Bedrock corporate proxy
  - `aws_region` — Bedrock only
- Configurable in-app via Settings → General. Provider dropdown shows relevant fields
  only (e.g. AWS Region only appears for Bedrock).

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
