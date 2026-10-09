# Agent CLI as the console's agent (design note, parked)

Status: **idea, not scheduled.** Written 2026-10-09 to keep the thinking for later.

## The idea

Today the console's agent is an in-process loop: an LLM adapter (Anthropic,
Bedrock, LangServe, plugin) returns tool calls, and the console's own loop
(`bridge_turn._run_agent_turn`) runs them through the autonomy gate. `[console]
ai = false` turns all of that off.

The idea is a third option. The operator picks an **agent CLI runnable** in
Settings → Model / API (for example `claude-run` from `runspec-claude`). The
console then hands each agent turn to that runnable. The external agent runs its
own tool loop and reaches the fleet through the console's **MCP server**, so
every tool call still goes through the console's confirm cards, credentials,
History and audit. The console keeps all its features; the model, its cost and
its auth move out to the CLI.

The prompt box stays. Slash commands (`/` runnables, `>` saved runs) work as
today, and plain text wakes the agent session.

## Why it's not "another adapter"

An adapter is a *model*: it answers one request and hands tool calls back. An
agent CLI is a whole *agent*: it decides, calls tools itself (over MCP) and
returns only when it's done. So this is a **second turn engine**, not a new
`adapters/*.py`:

| | Model provider (today) | Agent CLI (this idea) |
|---|---|---|
| Who runs the tool loop | the console (`_run_agent_turn`) | the CLI |
| How tools are called | in-process, `_gated_run_tool_async` | over the MCP server, same gate |
| Model / cost / auth | `[llm]` in the console | the CLI's own config |
| Turn output | streamed tokens + tool events | final text (or `stream-json`) |

`TurnSpec` already describes a turn independently of where it came from (chat,
trigger, schedule, runbook, helpdesk), so the switch point is one place: when
the provider is an agent CLI, `_run_agent_turn` delegates the whole turn instead
of looping.

## Shape

- **Settings.** One picker: *Anthropic*, *Bedrock*, *LangServe/plugin*, *Agent
  CLI (runnable)*, *None*. *None* is what `ai = false` does today; it stays as
  the hard off for locked-down installs. *Agent CLI* names a runnable (and
  host, default local) plus its fixed args (permission mode, model, working
  directory).
- **Turn mapping.** First turn of a session → `ask "<prompt>"`; later turns →
  `resume --session <id> "<prompt>"`. The CLI's session id is stored on the
  `ChatSession`. A trigger with `once_per_thread` maps a mail thread to one CLI
  session the same way.
- **Context the console adds today** (pinned wiki facts, local date and
  timezone, `_console_context`) is passed via `--append-system-prompt`, or
  exposed as MCP resources/prompts so the CLI can pull it.
- **MCP wiring.** The CLI must have the console's MCP server configured. The
  discovery file `runspec_mcp_server.json` carries the URL + token;
  `runspec-claude` would gain an `--mcp-config` arg (or the console writes a
  per-run config) so nothing depends on the user's global Claude config.
- **Triggers and schedules.** Unchanged at the call site: an agent-mode
  trigger, a `prompt`/`runbook` schedule, a helpdesk turn and Improve synthesis
  all build a `TurnSpec`, and the engine wakes the CLI. A rule-mode trigger
  running `claude-run ask` directly already works today with no console change
  and is the cheap first experiment.

## Approvals when nobody is at the desk

The MCP server parks a `confirm` call for `confirm_wait` (default 50s) and then
returns a resumable "awaiting approval" result. In an interactive turn that's
fine. In an unattended trigger/schedule turn, confirm-level calls will time out,
so in practice only `autonomous` runnables run unattended. That is arguably the
right line, but it should be stated in the UI. `claude-run` itself is
`confirm`; as the configured agent engine it's invoked by the console directly,
not through the gate, so that outer confirm doesn't apply.

Mail bodies and room messages become prompts for an agent with tools. The
console's gate (autonomy + confirm cards + denylist) is the control, exactly as
for today's in-console agent.

## What is lost or needs rework

- **Streaming.** `--output-format json` returns at the end. `stream-json`
  would restore live text and tool activity in the run card.
- **Evals.** They score which tool the model *would* pick without executing.
  An external agent just runs, so evals stay model-provider only.
- **Context gauge, effort, compaction, auto-continue.** Owned by the CLI.
- **Usage / metrics.** `claude -p` reports `total_cost_usd` and turn count;
  these can feed `runspec:chat_usage` and the turn metric, but token splits may
  not be available.
- **Cost model.** Each turn is a CLI session on whoever's account the CLI is
  signed in as, on the console machine.

## Open questions

- Is `claude -p` per turn fast enough interactively, or does it need a
  long-lived process (`stream-json` input) per session?
- Does the agent CLI also replace the `[llm] utility_model` uses (compaction,
  Improve synthesis, knowledge distill), or do those need a model provider?
- Other agent CLIs: the runnable contract (`prompt` in; text + session id out)
  is CLI-agnostic, so any agent with MCP support could sit behind it.
- MCP push (sampling, server→client notifications) as an alternative wake-up
  path for an already-running external session.

## Suggested first steps

1. Try a rule-mode mail trigger that runs `claude-run ask "{{ body }}"` with the
   console's MCP server configured for the headless CLI. No console code.
2. Add `--mcp-config` to `runspec-claude`.
3. Prototype the delegating turn engine behind an *Agent CLI* provider choice,
   chat only, then extend to triggers/schedules.
