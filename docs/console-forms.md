# Forms & credentials

## The Forms tab

Every discovered runnable appears on the **Forms** tab as a typed run dialog
generated from its `runspec.toml` — the operator counterpart to the agent's
tools. The dialog gives you:

- typed fields with validation (choices as dropdowns, ranges enforced,
  required fields marked);
- a **Saved runs** lane — [deterministic presets](console-saved-runs.md)
  (runnable + target + fixed args) that open a params-only dialog, or run
  immediately when they have no open params;
- optional and defaulted arguments folded into a collapsed **"Default
  arguments"** section, so the common case is a short form;
- date and date-time pickers for arguments that declare `ui = "date"` /
  `ui = "datetime"`;
- multi-line text fields that grow as you type (Shift+Enter for a newline);
- for runnables with subcommands: the inherited global arguments and each
  command's own arguments, sectioned.

Results render as [tables](console-agent.md#how-runnable-output-is-rendered)
with raw/copy toggles. From **History** (or a run card's pencil), **Edit &
rerun** reopens the same form pre-filled with the previous run's values —
except secrets, which are never recorded or pre-filled.

---

## Credentials

Named credentials let runnables authenticate to things — Jira, ServiceNow,
internal APIs — without secrets ever living in config files, chat, or the
audit log.

- Install the extra and add entries under **Settings → Credentials** (this is
  UI-only — the agent can use credentials but never read, create, or see
  their secret values):

```
pip install "runspec-console[credentials]"
```

- Secret values are stored in the **OS keychain** (Windows Credential
  Manager / macOS Keychain / Secret Service). `runspec_credentials.toml`
  holds only metadata — label, kind, scope — and can therefore safely ride
  [Config Sync](console-team.md); each operator supplies the secret values
  on their own machine once.

A credential has a **kind** — `userpass`, `token`, or an SSH key — and a
**label**. Two mechanisms apply them to runs:

### Name-derived environment variables

The credential's label derives environment variables by convention, injected
into the run's environment (never into the command line):

| Label + kind | Environment variables set |
|---|---|
| `Jira` (userpass) | `JIRA_USERNAME`, `JIRA_PASSWORD`, `JIRA_USER` |
| `Jira Dev` (userpass) | `JIRA_DEV_USERNAME`, `JIRA_DEV_PASSWORD`, `JIRA_DEV_USER` |
| `Deploy` (token) | `DEPLOY_TOKEN`, `DEPLOY_PAT` |

The aliases (`*_USER`, `*_PAT`) mean one credential covers the common
spellings different tools expect — a credential labelled `JSM` works for
`runspec-jsm` (`JSM_USER`/`JSM_PAT`) with no per-tool configuration. Name a
credential to match what the runnable reads from its environment; select one
or more in the run dialog's credential section (the form remembers your picks
per runnable — ids only, never secrets).

### One credential across many runnables — a naming convention

The derivation above becomes much more powerful when your runnables agree on
a convention. Instead of per-tool credentials, standardise on
**platform-level** names — say `Windows` and `Linux` — and have every
runnable declare its identity arguments with an `env` fallback to the
matching derived variables:

```toml
[win-service-restart]
description = "Restart a Windows service on a target machine"

[win-service-restart.args]
target   = { type = "str", description = "Machine to act on" }
service  = { type = "str", description = "Service name" }
username = { type = "str", env = "WINDOWS_USERNAME", description = "Windows account" }
password = { type = "password", env = "WINDOWS_PASSWORD", description = "Windows password" }
```

A credential **labelled** `Windows` (kind `userpass`) derives exactly
`WINDOWS_USERNAME` / `WINDOWS_PASSWORD` (plus the `WINDOWS_USER` alias) — the
label drives the names, so keep labels to the platform word. Now selecting
that one credential in *any* conforming runnable's run form fills its
identity: one `Windows` credential and one `Linux` credential cover the whole
catalogue. Argument resolution order still applies — a value typed directly
into the form wins over the env fallback, so one-off overrides stay easy.

The convention also travels well: ship the credential **metadata** in your
team's [Config Sync](console-team.md) repo or a
[white-label config seed](white-label.md#3-the-config-seed) —

```toml
[[credential]]
id    = "windows"
label = "Windows"     # the label drives WINDOWS_* derivation
kind  = "userpass"

[[credential]]
id    = "linux"
label = "Linux"
kind  = "userpass"
```

— and every operator's console arrives knowing the two credentials exist;
each operator enters the secret values once in **Settings → Credentials**
(secrets live only in their OS keychain, never in the synced or seeded
files). The [agent path](#credentials-and-the-agent) gets the same derived
variables, so conforming runnables work identically from chat.

### Argument bindings

For runnables whose interface is *arguments* (`--user` / `--password`) rather
than env vars, a credential can declare `username_arg` / `secret_arg`
bindings. When you select such a credential in the form, the username
pre-fills its field (visible, editable) and the secret rides a protected
environment channel — it never appears in the form, the command line, or the
audit trail — with the bound fields badged "from *credential*".

### Credentials and the agent

In-scope credentials (matched by host/runnable pattern) apply to agent-driven
runs automatically: bound arguments are filled, `password`-typed arguments
are removed from the agent's tool schema entirely (the model can neither see
nor supply them), and the same name-derived env vars are set. The agent can
pick a credential by id when several match — it only ever handles ids, never
secret values. Credentials also survive `sudo` for runnables that declare
`run_as`.
