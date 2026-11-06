# You decide what runs.

**Approved tools, not a shell. Every run on record.**

runspec turns your team's scripts into an allow list of tools, each with its
autonomy set in advance: reads run on their own, changes wait for approval,
and some only a person can start. `runspec-console` runs them on the servers
you've added, from typed forms, schedules or an AI agent, and records who ran
what, where and as whom.

It's built for application support and DevOps teams in regulated
environments, where nobody hands a shell to an agent and every change needs
an owner. Underneath is **runspec**, a single TOML interface specification
that turns any script into a validated CLI, a typed form and an agent tool at
once.

<div class="grid cards" markdown>

- **Operate** — install the console, connect an AI model, and drive your
  tooling from conversation and typed forms.
  [Get started →](console-quickstart.md)

- **Author** — turn your own scripts into runnables the console, the forms,
  and the agent can use.
  [Add a runnable →](authoring-intro.md)

- **Distribute** — package a branded, pre-configured console for your whole
  team.
  [White-label walkthrough →](white-label.md)

</div>

## Approved tools, not a shell

- **An allow list, not a shell.** Each tool is a script with a
  [`runspec.toml`](format.md) that declares its arguments and limits. The
  agent picks from that list and fills in typed arguments; it never gets a
  prompt to type into. Each server needs only a Python environment holding
  the tools: no containers and no agent service.
- **Fast where it's safe. Careful where it counts.** Autonomy is set per tool,
  and per argument, before anyone runs it: `autonomous`, `confirm` or
  `manual`. A risky argument raises the level. The approval card shows the
  exact server, account and arguments, and you can edit them before you
  approve.
- **Automation you sign off on.** Every run is logged with who started it,
  from where, as which account, and its exit code. [Audit events](audit.md)
  go to a hash-chained log and syslog. Secrets never appear on a command
  line, and a [denylist](denylist.md) switches tools off across the fleet in
  one step.

| | Agent with a shell | Agent in a sandbox | runspec |
|---|---|---|---|
| **The agent gets** | Any command | Any command, in an isolated copy | Only the tools on the list |
| **Runs on** | Your machine or server | A container or VM | Only the servers you've added |
| **Great for** | Writing and changing code | Trying things without risk | Supporting production, where every change needs an owner |

There are great agents that work with a shell or in a sandbox, and we use
them too. runspec is for the servers where you can't hand one over.

!!! info "Versions"
    This documentation tracks the latest published releases:
    `runspec-console` (the desktop app), `runspec` (Python) and
    `runspec-node` (Node) — the core libraries — and the runnable packs
    below.

---

## How it fits together

1. A **runnable** is a script or tool with its interface declared in a
   [`runspec.toml`](format.md): arguments, types, validation, and an
   **autonomy level** that says how much an agent may do with it.
2. Installing a runnable's package into a Python environment makes it
   discoverable — no registry, no configuration.
3. The **console** discovers runnables locally and on remote hosts over
   plain SSH (nothing to install server-side beyond `runspec` itself), and
   exposes them three ways at once: typed **forms**, an **AI agent**, and
   **automation** (triggers, schedules, runbooks).
4. Every agent-driven run passes the **autonomy gate** — confirm-by-default,
   with arguments editable at approval time, and a fleet-wide
   [denylist](denylist.md) as the kill switch.

## Batteries included

Install-and-go packs give the agent real capabilities on day one:

| Package | What you get |
|---|---|
| [`runspec-windows`](https://pypi.org/project/runspec-windows/) | Windows system administration and web/TLS diagnostics (Microsoft 365 is the separate `runspec-o365`). Installed with the console on Windows via `runspec-console[windows]`. |
| [`runspec-linux`](https://pypi.org/project/runspec-linux/) | 51 runnables: Linux administration, Python venv bootstrap, and the same web/TLS diagnostics. Nothing runs as root. |
| [`runspec-linux-root`](https://pypi.org/project/runspec-linux-root/) | The Linux tools that need root: package upgrades and installs, `reboot-host`, `set-sysctl`. For a root-owned admin venv. |
| [`runspec-jsm`](https://pypi.org/project/runspec-jsm/) | Jira Service Management: raise, comment, transition, discover request types. |
| [`runspec-snow`](https://pypi.org/project/runspec-snow/) | ServiceNow ITSM: create, comment, state changes, catalog discovery. |
| [`runspec-logops`](https://pypi.org/project/runspec-logops/) | Log triage: cluster errors into bounded digests, map traces to source. |
| [`runspec-claude`](https://pypi.org/project/runspec-claude/) | Drive the Claude Code CLI headlessly — delegate bounded coding sub-tasks. |
| [`runspec-room`](https://pypi.org/project/runspec-room/) | Team chat rooms and helpdesk queues with a self-service request portal. |

## For developers

The same `runspec.toml` that powers the console gives any script a real CLI —
generated `--help`, type coercion, validation, subcommands, structured
logging — with zero argument-parsing code, in
[Python](python.md) or [Node](node.md). And every runnable is an agent tool
for free: `runspec serve` exposes the whole environment over
[MCP](agents.md) to Claude Desktop, Cursor, or any MCP host.

```toml
# greet/runspec.toml
[greet]
description = "Greet someone from the command line"
autonomy    = "autonomous"

[greet.args]
name  = {type = "str"}
loud  = {default = false}
times = {default = 1}
```

```python
from runspec import parse

def main():
    args = parse()
    message = f"Hello, {args.name.value}!"
    if args.loud.value:
        message = message.upper()
    for _ in range(args.times.value):
        print(message)
```

That's a complete, validated CLI — and a typed form and agent tool in the
console. Start with the [quickstart](quickstart.md).
