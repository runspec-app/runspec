# runspec

**A desktop console where an AI agent runs your operations — safely.**

`runspec-console` discovers every runnable tool on your machine and across
your SSH fleet, and puts an AI agent in front of them: ask for something in
plain language, approve what it proposes, and let it automate the rest with
schedules, triggers, and shared runbooks. Underneath is **runspec** — a
single TOML interface specification that turns any script into a validated
CLI, a typed form, and an agent tool at once.

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
| [`runspec-linux`](https://pypi.org/project/runspec-linux/) | 59 runnables: Linux administration, package management, Python venv bootstrap, and the same web/TLS diagnostics. |
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
