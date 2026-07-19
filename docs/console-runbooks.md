# Runbooks

A **runbook** is a saved, reusable procedure the agent can follow — "when you
see *this symptom*, run *these runnables in this order*". Runbooks are how a
one-off conversation becomes standing operational knowledge: parameterised,
versioned with your team config, and runnable by name from the chat `$` menu,
from a schedule, or from a trigger.

---

## Author one from a conversation

The best runbooks are captured right after doing the thing. Work through a
procedure with the agent, then:

> Save what we just did as a runbook called "web-502-triage". Add a `host` argument that's a choice of web-01 and web-02.

The agent's `save_runbook` tool writes the entry (confirm-gated, and saving
again under the same id is how you edit). You can also add runbooks under
**Settings → Runbooks**, or edit the file directly.

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
