# Security posture — runspec-console

This document describes the console's security model: what it executes, how it
escalates privilege, how it talks to remote hosts, and where the trust
boundaries are. It is meant to be read before deploying the console in an
environment where it can run privileged commands.

## What the console is

A desktop UI that runs **runnables** — programs whose argument interface is
declared in a `runspec.toml` — on the local machine or on remote hosts over SSH,
optionally driven by an LLM. It does not invent commands: it can only invoke
runnables that are installed (discovered via `runspec local`) on a host you have
configured and can already reach over SSH.

## Trust model

The operator running the console is trusted to the extent of their own OS and
SSH credentials — the console is a convenience layer over `ssh` + a CLI, not a
privilege boundary that grants new access. Its job is to not *expand* the
operator's authority and to not let *runnable arguments* become a code-execution
vector.

## Execution model

- **Local runnables execute via `execv` (no shell).** Arguments are passed as a
  list to `subprocess.Popen` without `shell=True`, so argument values are never
  parsed by a shell.
- **Remote runnables execute over an SSH channel and are shell-quoted.** The
  remote command runs through the login shell, so every token (binary path,
  subcommand, flags, **and all argument values**) is quoted with `shlex.quote`
  before transmission. An argument value containing `;`, `|`, `$(...)`,
  backticks, `&&`, whitespace, or newlines is delivered as a single literal
  argument — it cannot start a second command. This is covered by regression
  tests that fire real injection payloads (`tests/test_remote_command_injection.py`).
- **No argument is interpolated into a command string unquoted**, on any path.

## Privilege escalation

Escalation is **explicit and declared in the runnable's spec**, never implicit:

- `run_as`, `become_method` (`sudo`/`su`/`pbrun`/`dzdo`), and `become_flags`
  come from `runspec.toml`/`[config]`, not from user input at run time.
- The escalated command is built as a token list (`build_become_argv`) — e.g.
  `sudo -u <user> env … <binary> <args>` — and then shell-quoted for the remote
  shell. The `su` path additionally quotes its inner `-c` command.
- The escalated user can only run the **declared runnable with the declared
  argument interface**; argument values are validated (below) and quoted, so
  escalation does not become arbitrary command execution.

## SSH

- **Host keys are verified.** Default policy is accept-new (trust-on-first-use)
  persisted to an app-managed `known_hosts`; a changed key afterward is a hard
  failure (MITM is caught). `~/.ssh/known_hosts` is also read. A strict mode
  (`[ssh] host_key_checking = "yes"`) refuses any host not already known. There
  is **no blind `AutoAddPolicy`.**
- **Keys are app-managed and rotatable.** The console can generate and rotate an
  Ed25519 keypair; the private key stays in the app-data directory on the
  operator's machine and is never transmitted.
- Optional HTTP `CONNECT` proxy support for reaching hosts through a corporate
  egress proxy.

## AI autonomy (when an LLM is configured)

- **Confirm-by-default.** A runnable's `autonomy` defaults to `confirm`: the LLM
  cannot run it without an explicit operator confirmation in the UI. Only
  runnables explicitly marked `autonomous` run without a prompt — intended for
  read-only operations.
- The resolution is "most restrictive wins" across per-arg → runnable →
  `[config]` default, so a single sensitive argument can force confirmation.
- The LLM provider is configurable, including an in-house gateway via a plugin;
  the model never gets shell access — it can only request a runnable invocation,
  which then flows through the same validation, autonomy gate, and quoting as a
  manual run.

## Input validation

Every argument is validated by the runspec core library before the runnable
acts: type coercion, `choice` membership, `pattern` (regex), `min-length` /
`max-length`, and numeric `range`. This is defense-in-depth on top of shell
quoting — quoting prevents injection regardless, and validation rejects
malformed values with a clear error.

## Secrets

- **LLM credentials** can be a static token or, preferably, a short-lived token
  vended by a command (`api_key_command` + TTL) so no long-lived secret is
  stored. Tokens are not written to logs.
- **SSH private keys** never leave the operator's machine.

## Audit log

Each invocation writes a per-run JSON record (operator, runnable, host, run-as,
arguments, timing, exit code) under the app-data `logs/` directory — a local
audit trail of who ran what, where.

## Known limitations (by design or scope)

- The console does **not** sandbox the runnable itself. A runnable runs with the
  privileges it is granted; the console's guarantees are about *not expanding*
  authority and *not letting arguments inject commands*, not about constraining
  what an authorized runnable may legitimately do.
- Trust is at the granularity of "can SSH to this host as this user." The
  console does not add its own authorization layer on top of SSH/sudo.
- LLM provider plugins load code named in local configuration; that is an
  operator-controlled extension point (equivalent to installing a package), not
  a remote capability.

## Reporting

Report suspected vulnerabilities privately to the maintainer rather than via a
public issue.
