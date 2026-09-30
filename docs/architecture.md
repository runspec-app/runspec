# Architecture

runspec is a **contract** for command-line tools. A `runspec.toml` next to the
code declares a tool's interface: arguments, types, validation, which arguments
are secrets, and how much autonomy an AI agent may have with it. Everything
else — a CLI, an MCP tool, a form, a scheduled job — is generated from that one
declaration.

The design rests on one principle:

> **The spec declares. Executors enforce.**

A declaration can't stop anything on its own. Autonomy, credentials and
"who may run this" are enforced by whatever actually runs the tool. runspec
ships several executors that enforce the same declarations, each suited to a
different kind of user.

## The layers

```
┌───────────────────────────────────────────────────────────────┐
│ Front-ends                                                    │
│   CLI · runspec serve (stdio MCP) · MCP gateway · console     │
├───────────────────────────────────────────────────────────────┤
│ Executors — enforce autonomy, credentials, identity           │
│   local process · SSH to remote hosts (agentless) · sudo/su   │
├───────────────────────────────────────────────────────────────┤
│ Contract — runspec (stdlib only)                              │
│   runspec.toml · parser · validation · secret args · logging  │
└───────────────────────────────────────────────────────────────┘
│ The host's own controls: Unix accounts · sudoers · auditd     │
```

The bottom line matters most. runspec doesn't invent its own users,
permissions or audit trail. It runs tools **as real Unix accounts, through
sudo**, so everything the organisation already trusts keeps working.

## Two kinds of user

| | **Tool author / local user** | **Operator with a fleet** |
|---|---|---|
| Runs tools | on their own machine | on their machine and on remote hosts over SSH |
| Front-ends | the CLI; any MCP client via `runspec serve` (Claude Code, Copilot, …) | the console (with or without AI); any MCP client via the console's MCP server |
| Autonomy | advertised in the tool schema; honoured by clients that support it | **enforced**: confirm cards, manual tools refused |
| Credentials | environment variables, `.runspec_env`, or an interactive prompt | the OS keychain, injected per run and never on a command line |
| Needs | `pip install runspec` | + `runspec-console` on the operator's machine; nothing new on hosts except the tools themselves |

A tool written once serves both users unchanged.

## Remote execution: agentless, per-user identity

The console reaches a host over **SSH as the operator's own account**. It runs
the tool as a short-lived process, and nothing stays running on the host.

- **Identity is the operator's.** A tool runs as whoever logged in, or as a
  service account via `sudo -u`, where sudoers allows it. sudo refuses anyone
  it doesn't allow; runspec doesn't keep its own list of who may run what.
- **Secrets never touch a command line.** Values travel on the process's
  standard input and are read by a fixed shell prelude, so they're absent from
  `ps` and from the sudo log (SPEC → "Remote Execution → Command building").
  This is tested on every change with real accounts.
- **Audit is the host's.** sshd, sudo and, where enabled, auditd record who ran
  what. auditd records the original login account even through sudo, and can
  forward to a central SIEM.
- **Kill switch.** A per-venv denylist lets an administrator disable some or all
  tools on a host at once, and it can't be used to lock itself out.

### Why SSH rather than an MCP server on every host

| | SSH + runspec | HTTP MCP server per host |
|---|---|---|
| Whose identity a tool runs as | each operator's, with their own sudo rights | the server's single account |
| On each host | the tools, nothing running | a daemon, a port, TLS, tokens, upgrades |
| Access control | existing accounts and sudoers | per-server tokens or OAuth |
| Audit | existing (sshd, sudo, auditd) | whatever each server logs |

HTTP servers suit a tool that is really a service owned by one team, or users
who must not have shell access. For many operators with different rights on
many existing Linux hosts, SSH reuses controls the organisation already runs.
It's the same argument that made agentless configuration management (Ansible)
the norm.

## Shared tool environments

A tool environment (a Python venv) on a host is either **personal** or
**shared**:

- **Personal:** a venv in a user's home, run only by that user.
- **Shared:** a venv owned by a **service account**. Every run happens **as that
  account, via sudo**, and sudoers — typically a Unix group — decides who may
  run it. Adding or removing a person is an administrator's group change, not a
  runspec change.

Either way, each venv has exactly one account running its tools, so its logs
and run history have a single writer and need no special permissions.

This is the recommended model. Today the console runs a tool as the service
account only when the tool declares `run_as`, or when the operator chooses Run
as. Making "run everything in this venv as its owner" a single per-venv setting
is on the roadmap below.

## Autonomy and AI

Every tool declares an autonomy level: `autonomous`, `confirm`, `supervised` or
`manual`. The console enforces it for its own agent **and** for external agents
through its local MCP server. A `confirm` call from Claude Code or Copilot
waits for an approval card in the console. The console also runs with
`ai = false`, as a plain operations tool: forms, schedules, history and fleet
management, with no model involved.

## What's deliberately out of scope

- **A central server.** There's no runspec service to host, patch or secure;
  the console runs on the operator's machine.
- **Its own identity system.** Unix accounts, sudoers and SSH keys are the
  identity system.
- **Long-running tools on hosts.** Tools are commands. Services are better
  served by their own daemons.

## Roadmap items that affect this picture

- **A shared executor library.** The console and the MCP gateway currently carry
  separate copies of the SSH, credential and escalation code. They should share
  one package.
- **The MCP gateway** (today `runspec-mcp`). It needs a client-agnostic setup
  that works in Copilot as well as Claude Code, simpler credential
  configuration, and a clearer name (`runspec-mcp-gateway`).
- **Per-venv "run as owner".** The console detects a shared venv's owner and
  escalates every run to it with `sudo -n -u`, so sudo alone decides who may run
  it. runspec can also refuse direct runs by anyone else (`enforce_run_as`).
- **Run history.** Simplifying it follows from the single-writer rule above; see
  `design/venv-run-history-assessment.md`.
