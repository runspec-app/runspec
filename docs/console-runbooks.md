# Runbooks

A **runbook** is a saved, reusable procedure the agent can follow — "when you
see *this symptom*, run *these runnables in this order*". Runbooks are how a
one-off conversation becomes standing operational knowledge: parameterised,
versioned with your team config, and runnable by name from the chat `$` menu,
from a schedule, or from a trigger.

If the procedure is really just *one runnable* with fixed targeting and args
(no judgement needed), use a [saved run](console-saved-runs.md) instead — the
deterministic sibling that runs without the agent.

---

## Author one from a conversation

The best runbooks are captured right after doing the thing. Work through a
procedure with the agent, then:

> Save what we just did as a runbook called "web-502-triage". Add a `host` argument that's a choice of web-01 and web-02.

The agent's `save_runbook` tool writes the entry (confirm-gated, and saving
again under the same id is how you edit). Fields it doesn't mention keep their
current values, so asking it to "fix step 3" won't quietly drop the runbook's
parameters. You can also add runbooks under **Settings → Runbooks**, or edit the
file directly.

The agent will also *offer* to capture a correction you just gave it, when that
correction would apply again next time. It always proposes — every save parks on
the autonomy gate, where you can edit the steps before approving. Publishing a
runbook to a room's self-service catalog (`discoverable`) stays operator-only:
the agent cannot set it.

## The format

Runbooks live in `runspec_runbooks.toml` as a `[[runbook]]` collection:

```toml
[[runbook]]
id = "web-502-triage"
title = "Triage 502s on the web tier"
symptom = "Web nodes returning 502s after a deploy"
tags = ["web", "incident"]
steps = """
1. Probe each node: web-01__http-check and web-02__http-check.
2. For any unhealthy node, run {{host}}__restart-service --name nginx.
3. Re-run http-check and confirm 200s before resolving.
"""

[runbook.args]
host = { type = "choice", options = ["web-01", "web-02"], description = "Node to act on" }
```

`steps` is a prompt template the agent follows: `web-01__http-check` is the
`host__runnable` tool token it calls (a step can also name a
[host group](console-hosts.md#groups) to fan out), and `{{host}}` is filled
from the `[runbook.args]` table — which uses the **same argument grammar** as
a `runspec.toml`, so choices, defaults, and types all work. Every runnable a
runbook drives still goes through the normal autonomy gate.

## How the agent finds one

You rarely need to name a runbook. The agent sees every runbook's id, title and
**symptom** (the "when to use this" line), so describing the problem is enough —
*"the web nodes are 502ing"* reaches `web-502-triage` on its own. Give each
runbook a good `symptom`; that is the field discovery runs on.

In `[llm] tool_mode = "search"`, `search_runnables` returns matching runbooks
too, under a separate `runbooks` key alongside the runnables — one search covers
both, and the agent is told to prefer an existing procedure over improvising a
sequence of runnables. Past 25 runbooks the inline list is capped (it lives in
the prompt-cached tool schemas, so it can't grow unbounded) and search covers
the rest.

**One tool reads a runbook: `run_runbook`.** It returns the runbook's steps with
`{{placeholders}}` filled, and it runs without asking you first — because
returning text isn't an action. Every runnable those steps then drive is gated
normally, at that runnable's own autonomy level, which is where the decisions
that matter actually happen. Writing a runbook (`save_runbook`) *is* gated.

!!! note "Changed in 0.202.0"
    There used to be a second tool, `load_runbook`, which returned the same steps
    without a gate while `run_runbook` asked for approval. In practice the agent
    took the ungated path, and in trigger turns it also sidestepped the
    `action_allow` allowlist — which governs `run_runbook` but not `load_runbook`.
    Folding the two together removed both problems.

## Running runbooks

- **Chat**: the `$` menu lists runbooks; or just ask — *"run the web-502-triage
  runbook against web-02"*.
- **Schedules**: a schedule's action can be a runbook with its arguments
  filled — see [Automation](console-automation.md#schedules).
- **Triggers**: an agent-mode trigger can render a runbook inline via the
  `run_runbook` action tool.

## Sharing with the team

Runbooks ride [Config Sync](console-team.md) (merged by `id`), so a team
ships vetted procedures from one git tag and every console gets them on the
next pull. Because group names are a synced registry too, a runbook that
targets the `web` group behaves identically on every operator's console.

---

## The offline runbook wiki

The same runbooks generate a human-readable **wiki** for when the console
itself is down — so a person can SSH to a host and run the same runnables the
agent would. In **Settings → Config Sync → Runbook docs**, point the output
directory at your local clone of the config repo and click **Generate**. It
renders, offline from the last sync:

- `README.md` — index with an outage preamble
- `runbooks/<id>.md` — one page per runbook (when-to-use, steps, params)
- `hosts.md`, `runnables.md`, `runnables/<host>.md` — the offline
  host/runnable catalogue
- `maintaining-docs.md` — how to regenerate, plus a paste-able
  `regenerate-runbook-docs` runbook

The output is deterministic (sorted, no timestamps), so it diffs cleanly —
review, commit, push back to the config repo, and the team gets the update on
their next sync.
