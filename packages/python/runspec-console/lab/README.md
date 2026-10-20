# Agent lab

Measures how the console's agent loop handles a workload — how big each model
call is, how history grows, what a prompt cache could reuse, and whether the
model got what it needed — so changes like spill thresholds or compact runnable
output can be compared on numbers instead of guesses.

It drives the real `Bridge` headless (no window) against **fake runnables**
(`lab/fakes`: `fake-jira-issue` and `fake-jira-search`, deterministic and
offline, shaped like the real Jira REST API and sized by an argument).

```bash
# from packages/python/runspec-console
python lab/agent_lab.py                                  # scripted tier, all scenarios × variants
python lab/agent_lab.py -s follow-up-chat -v baseline,compact
python lab/agent_lab.py --md report.md --json runs.json  # save the table / per-call data
```

The first run builds a venv at `~/.cache/runspec-agent-lab/venv` (console,
core library and the fakes, editable, without pywebview) and re-runs itself
inside it; delete it to rebuild. Each variant runs in its own process.

## Two tiers

- **Scripted** (default): a scripted stand-in model makes each turn's `steps`,
  greps a spilled result for any `need` string it hasn't seen, then answers.
  No key, no network, same numbers every run. It measures the console's
  plumbing, not model judgement. It works in either tool layout: with
  `tool_mode = "search"` it describes a runnable on first use in a chat, then
  calls `run_runnable`. Tokens are estimates (chars / 4) of the tool
  schemas plus messages on each call (the system prompt is left out); the
  cache column is an estimate of what a prompt cache would bill.
- **Live** (`--live`): a real model drives. The table shows the provider's own
  token counts (input, cache read, cache write, output), and a turn passes when
  the answer matches its `expect` regex, plus two totals: input with no
  prompt cache (input + cache read + cache write, what a proxy that drops
  cache writes bills) and the cache-priced equivalent. Needs
  `ANTHROPIC_API_KEY` (default model `claude-sonnet-5-5`, change with
  `--model`; it calls `https://api.anthropic.com` unless `--base-url` says
  otherwise, ignoring `ANTHROPIC_BASE_URL`), or `--llm my-llm.toml`
  with an `[llm]` table for any provider the console supports — including a
  corporate proxy, run on a machine that can reach it. Use `--samples N`:
  live runs vary.

## Traces

Add `--trace` to send OpenTelemetry traces from either tier: the same
turn → model call → tool call spans the console sends, so a lab run can be
inspected in Arize, LangSmith, Phoenix or any OTLP backend.

```bash
python lab/agent_lab.py --trace my-trace.toml -s follow-up-chat
```

`my-trace.toml` holds a `[telemetry]` table in the console's own format (keep
it out of the repo if it carries a key):

```toml
[telemetry]
endpoint = "https://otlp.arize.com/v1/traces"
headers = { api_key = "...", space_id = "..." }
# or api_key_command = "..." + api_key_header = "api_key"
```

Leave the file off (`--trace` alone) to configure the exporter only from the
standard `OTEL_EXPORTER_OTLP_*` variables. Unless the file says otherwise, the
lab sends to project `runspec-agent-lab` with content capture on. Each variant
reports as its own instance (`service.instance.id` = the variant name), and
every turn span carries `runspec.lab.variant`, `runspec.lab.scenario`,
`runspec.lab.sample` and `runspec.lab.tier`. The scripted tier's model spans
are named `chat scripted` and carry no token usage; live runs carry the
provider's.

## Files

- `scenarios.toml` — chats to run. Each `[[scenario.turn]]` is one user
  message; later turns carry earlier results in history.
- `variants.toml` — what to compare: `config` (merged into `config.toml`),
  `env` (the fakes read `FAKE_JIRA_SHAPE=raw|compact`), and `patch` (override
  a code constant, for options that don't have a setting yet).
- `fakes/` — the fake runnable package. Add a runnable there to model another
  tool's output.

Variants deliberately avoid API-side features (context editing, server-side
tool-result clearing): some LLM proxies don't support them, and its
prompt caching is unreliable, so read the *no cache* column as the
production-relevant one.

## Search benchmark

`search_bench.py` measures the `search_runnables` ranking on its own: it loads
every `runspec.toml` in this repo's packages (about 110 runnables) onto one
fake host and runs each query through the real `Bridge._search_runnables`,
scoring the rank of the first expected runnable. No model, no network.

```bash
python lab/search_bench.py          # both query sets: hit@1/3/10, MRR, the misses
python lab/search_bench.py --all    # every query with its top three
```

`search_queries.toml` was written alongside the ranking change;
`search_queries_heldout.toml` was written separately, in everyday ops wording,
so judge a change on both. Whether the agent then picks the right tool with a
real model is the Evals tab's job.
