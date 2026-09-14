# Console without AI

runspec-console can run as a **non-AI fleet tool** — the same binary, with the
assistant and every agent feature switched off by one config flag:

```toml
# config.toml
[console]
ai = false
```

Two reasons to want this:

- **Onboarding.** Get comfortable with the runspec concept — hosts, credentials,
  Forms, fleet runnables, saved runs, schedules — before adopting the agent.
- **Corporate robustness.** Let a security or platform team vet the fleet
  execution substrate (SSH transport, keychain credentials, denylist,
  `enforce_run_as`, Config Sync / seed) with **zero AI in the loop**, then enable
  the agent later as a seeded flag flip on an architecture already in production.

## What you get with AI off

| Keeps working | Goes away |
|---|---|
| **Console** tab as the run-output pane (command bar `/` runnables and `>` saved runs, Forms runs, History reruns) | Chat: plain text in the command bar, conversation History, "Ask the assistant", include-in-context toggles, the context gauge |
| **Forms**, **History**, **Logs**, **Analytics**, **Specs**, **Rota**, **Dev** | **Improve** and **Evals** tabs |
| **Schedules** — `runnable`, `saved_run`, `event` actions | `prompt`, `runbook` and `eval` schedules (refused as invalid) |
| **Triggers** — rule mode (fixed run, single or multiple runnables, claim gate, replies) | Agent-mode triggers and a rule's LLM `pre_turn` (refused as invalid) |
| **Rooms** — join, read, reply; portal requests for runnables are enacted | Runbooks in the room catalog; agent replies / claim cards from agent-mode chat triggers |
| Settings: SSH, Jump Hosts, Local venvs, Saved runs, Self-Service, Filters, Repos, Credentials, Denylist, **MCP Server**, Config Sync, Mailbox, Teams, Sounds | Settings: Model / API, Agent tools, Runbooks, MCP (client), Chat History, Telemetry |
| **The console as an MCP server** — an *external* coding agent (Claude Code) still reaches the fleet through the console's confirm gate | Tracing, external MCP servers and the code sandbox are not started (they only serve the agent) |

The backend enforces all of this itself — the UI gate only keeps the interface
honest. An agent-mode trigger or a `prompt` schedule that lands in a seeded file
shows as **invalid with a clear reason**, it never silently runs as something
else; `send_chat` / `run_runbook` / `run_evals` return one message:

> AI features are disabled in this console ([console] ai = false).

## How the flag behaves

- **Default on.** An absent or non-boolean value means enabled, so a default
  install is unchanged.
- **Read once at start.** The console reads it at bring-up (after the config
  seed applies) and holds it for the session, so the UI and the backend gates
  can never disagree. Change the flag and **restart** to apply. Settings shows a
  notice while it is off.
- **Config only.** There is no toggle in Settings — the flag is meant to be a
  governance default that ships from a config seed or Config Sync.
- **Independent of "no provider".** Leaving `[llm] provider` unset still shows
  the assistant with a "no model provider configured" message; `ai = false`
  removes the assistant.

## Rolling it out with a config seed

Because the flag lives in `config.toml`, it rides the [config seed](white-label.md#3-the-config-seed)
and Config Sync like any other key:

```toml
# config_seed/config.toml in the corporate package
[console]
ai = false

[console.theme]
brand_name = "Acme Fleet"    # optional — present it as its own tool
```

1. Ship that seed → every console comes up **AI-off, forms + fleet only**.
   Nothing to configure, nothing for security to object to.
2. Teams get comfortable with the whole fleet substrate.
3. Once the AI application is approved, ship a seed with `ai = true` (and add a
   model extra such as `runspec-console[anthropic]` to the package's
   dependencies). The **same installed console** lights up the agent on its next
   upgrade.

The key is deliberately **not** in the `preserve` lists: a seeded value
re-asserts on every seed re-apply and every sync pull, so a local flip lasts
only until then. That is a re-asserting default, not a lock — the only true
lock is not installing a model extra at all, which is what a bare
`pip install runspec-console` already is (the model SDKs are optional extras).

See also `docs/design/console-ai-off-switch.md` for the design and the full
inventory of gated surfaces.
