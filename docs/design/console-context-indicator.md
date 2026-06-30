# Console context indicator (window-fill gauge + composition + truncation signal)

## Why

The chat agent's context window is a finite, invisible budget. Today the console
spends it silently: a per-turn token/cost line is rendered *inside* each assistant
bubble (`OutputPanel.tsx:994`), but there is no **at-a-glance** indication of how
full the window is right now, no breakdown of **what** is filling it, and — the
case that bit us in practice — **no signal at all when a tool response is
truncated** before it reaches the model.

The operator's need, in their words: *"I want to see that the window is getting
full so I know when the memory file needs pruning or another strategy is needed —
and I experienced response truncation yesterday because a runnable returned a huge
JSON blob, so I want to be able to see that's happened."*

So this is two related things:

1. A **persistent fill gauge** with a **by-source composition** hover, so the
   operator can see the window filling and *which bucket* is responsible.
2. A **truncation signal** on tool-result cards, so the silent
   `_AGENT_TOOL_OUTPUT_CAP` cut-off becomes visible — the thing that makes a turn
   quietly wrong (the agent receives JSON cut off mid-structure).

It is an **observability / hygiene** aid, not a hard limiter. It surfaces what is
already happening so the operator can act (prune memory, switch tool mode, clear
chat, or — the durable fix — redesign a chatty runnable to return a summary first).

## What already exists (build on, don't rebuild)

- `bridge_invoke.py:982 _usage_from_response` already pulls **exact** token counts
  off every model response: `input` (uncached), `cache_read`, `cache_creation`,
  `output`. Anthropic/Bedrock split caching; OpenAI reports prompt/completion only.
- `bridge_invoke.py:966` already emits `runspec:chat_usage` per turn; `OutputPanel.tsx`
  already renders per-message tokens, cache-hit %, and an estimated cost
  (price table at `OutputPanel.tsx:69`).
- `App.tsx:654` already has a **footer** showing catalogue size + version/update —
  the natural home for the gauge, no new chrome.

The key realisation: `input + cache_read + cache_creation` from the last turn **is
the exact current prompt size** — the model reports the whole input it was sent.
The measurement is solved; the **presentation** and the **composition split** are
the gap, along with truncation visibility.

## What fills the window (the buckets)

Assembled per turn in `_agentic_chat_turn` (`bridge_invoke.py:792`); system override
built at `bridge_invoke.py:318-332`.

| Bucket | Source | Size / behaviour |
|---|---|---|
| System base | `_base_system` (`bridge_internals.py:845`) | stable, cached |
| Console context | `_console_context` (`bridge_internals.py:858`) | grows with triggers / schedules / host-groups / mailbox; `""` when unconfigured |
| Date | `_now_context` (`bridge_internals.py:826`) | ~250 B, date-only (cache-stable within a day) |
| Pinned memory | `memory.build_header` (`memory.py:179`) | **hard-capped 6 facts / 700 chars** |
| Tool definitions | `_runnables_to_tools` (dedup default) | scales with **distinct runnables**, not hosts × runnables |
| Catalog index | `_build_catalog_index` (`bridge_search.py:477`, search mode) | capped `tool_index_limit` (default 300), ~2–5 KB, **folded into the first user message** (counts as history, not tools) |
| Conversation history | `_chat_history`, trimmed to 60 msgs (`bridge_invoke.py:777`) | **dominated by large tool outputs** — full output goes to the model; only the *display* is clipped to 2000 chars |

Honest finding: the memory file the operator worried about is **already the most
disciplined bucket** (capped tiny, rest kept out of context behind
`search_memory`). The real, volatile weight is almost always a **single large tool
output** sitting in recent history — which is exactly the truncation case. A good
indicator proves that, and incidentally reassures that memory is fine.

## Truncation today (the silent failure)

`bridge_autonomy.py:946` caps a tool result at `_AGENT_TOOL_OUTPUT_CAP`
(`bridge_const.py:133`) and appends an **in-band** marker:
`[output truncated at 1000000 chars — incomplete; any JSON above is cut off mid-structure]`.

Two problems:

1. **It's invisible.** The `runspec:tool_end` event ships only `output[:2000]`
   (`bridge_invoke.py:911`); the marker lives at the *end* of the string, so the UI
   never sees it. The full marked output goes to the model and to the
   `_trace("tool_result", {length, …})` diagnostic (`bridge_invoke.py:917`) — but
   nothing surfaces to the operator.
2. **The cap is `1_000_000` chars ≈ 250 k tokens — a hardcoded constant, not a
   config knob.** By the time it trips, the turn has already overflowed most
   context windows; the truncation marker is cold comfort. The cap wants to be
   *configurable*, paired with the "summary-first, drill-down" runnable redesign
   the operator is doing anyway.

   **Decision (as built): make it configurable; keep the 1 MB default.** The
   1 MB default is deliberate (see `bridge_const.py:127`) — it exists precisely so
   a data-heavy runnable emitting one big compact-JSON line isn't sliced
   *mid-structure*, which is the bug the old 16 KB cap caused. Lowering the
   *default* would re-introduce that for legitimate large results, so the lever is
   per-deployment (`[llm] tool_output_cap`) rather than a lower global default. The
   real fix is **visibility** — even at 1 MB you now *see* the cut on the tool
   card — plus the operator's own runnable redesign, which sets the right ceiling
   for *their* data and can dial the cap down to match.

## Design

### Measurement — hybrid (exact total, proportioned slices)

The model returns one aggregate `input_tokens`, so a per-bucket split needs
per-component counts. Use a **hybrid**:

- **Headline total = exact**, from the existing `chat_usage` (`input + cache_read +
  cache_creation`).
- **Slices = local estimate**, `chars / ≈4` per bucket, then **scaled so the slices
  sum to the exact total**. So the gauge number is real; the breakdown is an honest
  estimate that always reconciles to it.

Why not Anthropic's `count_tokens` API: it's accurate but adds a round-trip per
measurement and is provider-specific. Not worth it for a "how full / rough split"
gauge. (We already label cost as an estimate — same posture.)

This is nearly free to compute: `_agentic_chat_turn` **already holds every bucket
in hand** at send time (`model_messages`, `tools`, `index_text`, the system
override) and already serialises them for `_trace("model_request", …)`
(`bridge_invoke.py:830`). Sum chars per labelled bucket, emit one new
`runspec:context_estimate` event alongside `chat_usage`:

```
runspec:context_estimate {
  id, model,
  window: 200000,                 # model's max, from a per-model table
  total_tokens: <exact from usage>,
  buckets: [ {key:"history", tokens, est:true}, {key:"tools", …}, … ],
  cache_hit_pct,
}
```

### UI — footer gauge + hover

Home: a **third row in the bottom-left footer column** (`App.tsx:654`), a direct
sibling of the existing Rooms (`App.tsx:665`) and Catalogue (`App.tsx:710`) rows —
the same `Popover placement="topLeft" mouseEnterDelay={0.2}` idiom, icon +
tabular-nums count + status dot. No new visual language, and the composition hover
reuses the Catalogue popover's label-+-count breakdown layout (`App.tsx:725`).

- The row: `◐ 48.2k / 200k (24%) · cache 71%`, with the fill dot green → amber →
  red against the **actual model's** window. Reuse the existing footer dot colours
  (`#52c41a` / `#faad14` / `#ff4d4f`) — green < ~60 %, amber ~60–85 %, red > 85 % —
  so it reads as part of the same status column. Window size comes from extending
  the per-model price table (`OutputPanel.tsx:69`) to carry context-window sizes.
- **Render only when live.** The footer is global chrome (visible on Forms, Hosts,
  … tabs too), but the gauge is per-chat and dynamic. Mirror the Rooms row, which
  hides when there's nothing to show (`App.tsx:665`): render the gauge only once the
  active chat has a turn's usage — don't park a dim `0k / 200k` on the Forms tab. It
  appears when you're actually chatting.
- Hover → a stacked-bar popover: System base / Console context / Memory (pinned, *N
  pins*) / Tools / Catalog / History (*N turns*, **largest tool output highlighted**).
  Each slice shows the token estimate; the footer total stays exact.

The gauge answers *"how full is the window"*. The **truncation badge is a separate
surface** — it lives on the **tool card in `OutputPanel`**, in-context where the cut
happened (*"this call got cut off"*), not in the footer. Two questions, two homes.

### Truncation signal (Phase 1 — they hit this for real)

- Detect truncation at the cut site (`bridge_autonomy.py:946`) and carry it as
  **structured metadata** on `runspec:tool_end` (`truncated: true`, `full_length`,
  `cap`) instead of only in-band text — so the tool card can badge it
  (⚠ *output truncated — 3.2 MB cut to 1 MB; JSON incomplete*).
- The other tools that already return a `truncated` flag in their JSON
  (`bridge_search.py:404`, `repos.py`, `fs_browse.py`) get the same badge for free
  by reading that field.
- Make `_AGENT_TOOL_OUTPUT_CAP` a **config.toml `[llm] tool_output_cap`** (default
  lowered substantially from 1 M) so an operator can tune it per deployment — and so
  the "summary-first" runnable pattern has a sane ceiling to fail against, loudly
  and early, instead of silently at a quarter-million tokens.

### Efficiency levers (all four, surfaced by the indicator)

1. **Large tool-output flagging** *(Phase 1, with truncation)* — highlight the
   biggest tool result in history and, on truncation, hint at the durable fix:
   return a summary and let the agent request specifics (and/or route the blob
   through `runspec-logops summarize-log`, which exists for exactly this).
2. **`tool_mode = "search"` nudge** — when tool/catalog weight is the dominant
   bucket on a large fleet, the breakdown makes the case for search mode (a compact
   index instead of every schema) concrete.
3. **Live compact / clear** — a soft warning as history nears the 60-message trim
   (`bridge_invoke.py:777`) with a *Compact now* action reusing the resume-time
   `_summarize_middle_turns` machinery (`bridge_invoke.py:675`) on the live session.
4. **Pinned-memory review** — show `pins / cap` with a quick "review pins"
   affordance (the file the operator asked about). Mostly reassurance, since it's
   already capped — but it closes the loop on the original question.

## Phasing

- **Phase 1 — visibility:** the footer gauge + composition hover
  (`runspec:context_estimate`), the **truncation badge** + structured
  `tool_end` metadata, large-output highlighting, and the configurable/lowered
  `tool_output_cap`. Delivers the operator's whole stated need; low risk (one new
  backend event computed from data already in hand, plus one footer component and a
  tool-card badge).
- **Phase 2 — levers:** the `tool_mode=search` nudge, live *Compact now* / clear,
  and pinned-memory review. Each is an affordance hung off the Phase 1 signal.

## Phase 1 — as built

Backend (`packages/python/runspec-console`):

- **Measurement helpers** (pure, in `bridge_utils.py`, unit-tested in
  `tests/test_context_indicator.py`): `estimate_context_buckets(parts, total)`
  (largest-remainder split, slices always sum to the exact total),
  `model_context_window(model)`, and the parseable truncation marker pair
  `make_truncation_suffix` / `parse_truncation`.
- **`runspec:context_estimate`** dispatched at the end of each turn in
  `bridge_invoke.py::_agentic_chat_turn`, computed from the *last* iteration's
  prompt size (not the cross-iteration `usage_total` sum) and the char sizes of
  the buckets — system parts captured in `_spawn_chat_turn` (passed in as
  `system_parts`), `tools` / `catalog` / `history` measured in-loop via
  `_messages_chars`. Payload: `{model, window, total_tokens, cache_hit_pct,
  buckets:[{key,tokens,est}]}`.
- **Truncation + size on `runspec:tool_end`**: `output_chars` and `truncated`
  (`{orig_len, cap}` or `null` via `parse_truncation`).
- **Configurable cap**: `_tool_output_cap()` reads `[llm] tool_output_cap`
  (default `_AGENT_TOOL_OUTPUT_CAP`); the truncation site in `bridge_autonomy.py`
  uses it + the parseable marker.
- **`runspec:chat_cleared`** dispatched from `clear_chat` to reset the gauge.

Frontend (`packages/console-ui`):

- **Footer gauge** (`App.tsx`): third row in the bottom-left column, a
  `DashboardOutlined` + `48.2k / 200k · 24%` chip (dot green/amber/red at
  60/85 %), hidden until `runspec:context_estimate` fires, reset on
  `runspec:chat_cleared`. Hover popover lists buckets (largest first, zero-token
  ones dropped) with per-bucket bars, plus cache-hit % and an "estimate" caption.
- **Tool-card badges** (`OutputPanel.tsx`): `⚠ output cut` (red, with the
  original→cap sizes) when `truncated`; a dim size chip when a non-truncated
  result exceeds `LARGE_OUTPUT_CHARS` (50 KB). `ToolCallEntry` carries
  `outputChars` / `truncated`; the `tool_end` handler populates them.

## Phase 2 — as built

All three levers hang off the gauge hover, computed from extra fields the
backend now folds into `runspec:context_estimate` (`tool_mode`, `history_msgs`,
`memory_pins`, `memory_pins_shown`):

- **Search-mode nudge** — when `tool_mode != "search"` and tool + catalog
  definitions are ≥ 30 % of the window, a hover line offers *Switch to search
  mode* (opens Settings). Pure UI from the emitted `tool_mode` + bucket shares.
- **Live *Compact now*** — `compact_chat()` (`bridge_invoke.py`) condenses the
  in-session history in place: it drops the older prefix, folds a note (mechanical
  by default, LLM summary under `[chat_history] compact_on_resume`) into the first
  kept user message, and keeps the recent tail — the same safe, pairing-preserving
  transform as `resume_conversation`. The hover offers it once history ≥ 24
  messages; emits `runspec:chat_compacted`. New JS API method (frozen in
  `test_bridge_api_surface.py`).
- **Pinned-memory hint** — the Memory bucket row shows `· N pinned`; when more
  facts are pinned than fold into the prompt (`memory.HEADER_MAX_FACTS`), a hover
  line says so. Count via `memory.pinned_count()`.

Unit tests: `test_chat_history.py` (compaction drops the prefix, folds the note,
preserves tool pairing, no-ops when short), `test_memory.py::test_pinned_count`.

## Not in scope / honest limits

- The slice breakdown is an **estimate** (char-based), reconciled to the exact
  total — not a tokenizer. Good enough to answer "how full / what's heavy"; it
  should be labelled as an estimate, like cost.
- This **observes**; it does not auto-prune or auto-compact. The operator (or a
  future explicit setting) decides. Auto-anything here would surprise.
- Per-bucket exactness would need `count_tokens` round-trips — deliberately
  declined for latency/provider-coupling reasons.
