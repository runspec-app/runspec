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

[[runbook.step]]
text = "Probe each node: web-01__http-check and web-02__http-check."

[[runbook.step]]
title = "Restart unhealthy nodes"
text = "For any unhealthy node, run {{host}}__restart-service --name nginx."

[[runbook.step]]
text = "Re-run http-check and confirm 200s before resolving."

[runbook.args]
host = { type = "choice", options = ["web-01", "web-02"], description = "Node to act on" }
```

Steps are an ordered `[[runbook.step]]` array — each step its own block with the
prose the agent follows (`text`) and an optional short `title`. This keeps the
file readable and diffable in your Config-Sync repo instead of one long quoted
string; multi-line step text is written as a triple-quoted `"""…"""` block, so it
stays readable in an external editor too. At run time the console renders the steps back to numbered prose
(`1. …`, `2. Restart unhealthy nodes: …`) and hands it to the agent, so a step
is just an instruction: `web-01__http-check` is the `host__runnable` tool token
it calls (a step can also name a [host group](console-hosts.md#groups) to fan
out), and `{{host}}` is filled from the `[runbook.args]` table — which uses the
**same argument grammar** as a `runspec.toml`, so choices, defaults, and types
all work. Every runnable a runbook drives still goes through the normal autonomy
gate.

Because the agent judges the steps (rather than a workflow engine executing
them), conditional logic lives in the prose — write *"if the node is healthy,
skip the restart"* right in a step. A step carries only `text` and `title`; there
are deliberately no `condition`/`goto`/step-reference fields (that would be a
[workflow engine](design/console-triggers.md), which is out of scope).

A parameter with a `default` is **optional**; one without a default is treated as
**required** (the operator must supply it). To make a `choice` optional, give it a
default in the editor (or a `default = …` in the file) — the **Settings → Runbooks**
arg editor shows a default picker for choice params alongside their options, plus a
description field. Editing a runbook there saves that one entry only, so runbooks you
changed in the file directly (or that arrived via Config Sync) are left intact.

!!! note "Legacy `steps` string"
    Older runbooks used a single `steps = """…"""` string instead of the
    `[[runbook.step]]` array. That form is still read and runs identically; open
    such a runbook in **Settings → Runbooks** and save it to convert it to the
    ordered-step form.

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

**`run_runbook` follows a runbook.** It returns the runbook's steps with
`{{placeholders}}` filled, and it runs without asking you first — because
returning text isn't an action. Every runnable those steps then drive is gated
normally, at that runnable's own autonomy level, which is where the decisions
that matter actually happen. `values` is optional: you can consult a procedure
without knowing its parameters yet — unfilled ones stay as `{{name}}` in the
returned steps and are listed for you. Writing a runbook (`save_runbook`) *is*
gated.

**`load_runbook` reads a runbook to inspect or edit it.** It returns the raw
definition as data — title, symptom, tags, the verbatim step array (placeholders
intact) and the parameter table — *not* the rendered "follow now" body, and it
never executes anything. This is the read half of the edit loop: `load_runbook`
→ change the fields → `save_runbook` with the same id. It's read-only and
autonomous (no prompt), and **chat-only** — a trigger or schedule can't call it
(see the governance note below).

!!! note "History: `load_runbook` (removed 0.202.0, reinstated chat-only 0.214.0)"
    The original `load_runbook` returned the *same followable steps* as
    `run_runbook` while being ungated and outside the `action_allow` vocabulary,
    so in a trigger turn it let automation follow a runbook the operator's
    allowlist excluded. It was folded away in 0.202.0. The reinstated tool avoids
    that gap two ways: it returns the runbook's *definition* (not executable
    steps to follow), and it is **chat-only** — stripped from every headless
    (trigger/schedule) turn and refused by an origin guard — so a headless turn
    still reads a runbook only through `run_runbook`, which `action_allow`
    governs.

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
