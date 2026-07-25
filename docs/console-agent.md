# Working with the agent

The Console tab is a conversation with an AI agent that has real tools: every
runnable on every connected host, plus built-in tools for files, schedules,
runbooks, memory, and (on Windows) Outlook. This page is a cookbook — what the
agent can do, ready-to-paste prompts for each capability, and an honest list
of the things that still need the Settings UI.

---

## How agent actions are gated

Every runnable declares an **autonomy** level in its `runspec.toml`, and the
agent honours it:

- `autonomous` — the agent runs it without asking.
- `confirm` (the default) — the agent proposes the call; a confirmation card
  appears with the exact arguments, which you can **edit before approving**.
- `supervised` / `manual` — stricter still; `manual` runnables are never run
  by the agent.

Built-in tools follow the same model: read-only tools (searching, reading
workspace files, checking status) run autonomously; side-effecting tools
(creating schedules, writing documents, executing code) are confirm-gated.
When the agent fans one runnable out across a host group, you get **one
grouped confirmation** for the whole batch.

---

## What the agent can do — and what it can't

The agent can enact almost all of the product, including its own setup. Two
configuration areas are deliberately UI-only; the docs below always tell you
which is which.

| The agent can… | UI-only (Settings) |
|---|---|
| Run any discovered runnable on any connected host, or fan out across a [host group](console-hosts.md#groups) | Store **credentials** (Settings → Credentials — the agent *uses* credentials but never reads, creates, or sees secret values) |
| Add, update, and remove **[hosts](console-hosts.md)** and the **group** registry (`manage_hosts`, `manage_groups`) | **Model / API** and theme configuration (the agent editing its own model setup is a bootstrap problem) |
| Create, edit, enable/disable **[triggers](console-automation.md#triggers-windows)** (`manage_triggers`) | **Profiles** registry (sidebar working sets) |
| Set the working-hours **[rota](console-automation.md#rota-working-hours-for-automation)** (`manage_rota`) | |
| Search and describe runnables (`search_runnables`, `describe_runnable`) | |
| Save and follow **[runbooks](console-runbooks.md)** | |
| Create **[schedules](console-automation.md#schedules)** — cron/interval, running a runnable, a prompt, or a runbook | |
| Manage the fleet **denylist** (disable/re-enable runnables per host) | |
| Transfer files between hosts (`download_file`, `relay_file`) | |
| Read and locally edit registered **git repos** (`repo_*` tools) | |
| Work with session **workspace files** — list, read, grep, write | |
| Execute **Python and SQL** in the session workspace (confirm-gated) | |
| Remember facts across conversations (`remember` / `search_memory`) | |
| Create markdown **documents** and upload them to hosts | |
| Read and act on **Outlook** mail/calendar (**Windows**; send is confirm-gated) | |
| Report its own state (`console_status` — time, rota, triggers, schedules) | |

The configuration tools (`manage_hosts`, `manage_groups`, `manage_triggers`,
`manage_rota`) are **chat-only by design**: a trigger- or schedule-fired agent
turn can never use them, so your automation can never rewrite its own
configuration. Reading config is autonomous; every change shows a
confirmation card you can edit before approving.

---

## Prompt cookbook

Paste these into the Console tab and adapt. Where a confirmation card will
appear, it's noted.

### Configure the console

> Add a host called web-01: deploy@web-01.internal, runspec at /opt/venvs/ops/bin/runspec, in the `web` group.

> Create a trigger: when an email arrives with "outage" in the subject from anyone at bigcustomer.com, run the diagnostics runnable against the affected host and draft (don't send) a reply with the findings.

> Set my working hours to Monday–Friday 09:00–17:30 and enable the rota.

Each change presents one confirmation card with the exact config it will
write — edit it before approving if the agent got a detail wrong. Trigger
definitions are validated before saving, so an invalid one comes back with
the reason instead of landing in your config. (The one setup step the agent
can never do is its own Model / API configuration.)

### Explore what's installed

> What runnables do you have on each connected host? Group them by purpose and flag anything that looks risky.

> Describe the `http-check` runnable — what arguments does it take and what does it return?

Runs autonomously — discovery and description are read-only.

### Run things

> Check the TLS certificate on portal.example.com and tell me when it expires.

> Run disk-usage on every host in the `web` group and summarise anything above 80%.

Each run (or one grouped card for a group fan-out) asks for confirmation
unless the runnable is `autonomous`. You can edit the arguments on the card
before approving.

### Save a procedure as a runbook

> Save what we just did as a runbook called "cert-expiry-sweep": check the TLS certificates on every host in the `web` group and report any expiring within 30 days. Add a `days` argument defaulting to 30.

The agent's `save_runbook` tool writes it to your runbook collection — from
then on it's in the chat `$` menu, runnable by name, and shareable with your
team via [Config Sync](console-team.md). Saving again under the same id edits
it. See [Runbooks](console-runbooks.md).

### Schedule work

> Schedule the cert-expiry-sweep runbook every Monday at 08:00.

> Every night at 02:00, run log-rotate on whichever of app-01 or app-02 is reachable.

`create_schedule` is confirm-gated; the schedule then appears on the
Schedules tab. Reactive **triggers** — "when an email arrives…" — work the
same way via `manage_triggers` (see
[Configure the console](#configure-the-console) above and
[Automation](console-automation.md)).

### Analyse logs and output

> Download /var/log/app/errors.log from app-01, summarise the distinct errors, and map the most frequent one to source if you can.

With [`runspec-logops`](https://pypi.org/project/runspec-logops/) installed on
the host, the heavy lifting (clustering, code-mapping) runs host-side and the
agent reads a bounded digest — cheap enough to do routinely. Large outputs
spill into the **session workspace**, where the agent greps and pages them
instead of re-reading everything.

### Fleet safety

> Disable the reboot-host runnable on every host until I say otherwise.

`manage_denylist` drives the venv-scoped [denylist](denylist.md) over SSH —
listing is autonomous, changes are confirm-gated, and re-enabling is always
possible from the console.

### Documents and memory

> Write up this incident as a markdown document and save it to the workspace.

> Remember that app-02 is the canary — deploy there first.

`remember` stores machine-local notes the agent recalls in later
conversations; pinned memories are always in context.

### Outlook (Windows)

> Anything urgent in my inbox this morning? Draft (don't send) a reply to the one about the certificate.

Reading mail/calendar is autonomous; drafting and sending are confirm-gated,
and drafts land in your Outlook Drafts folder for you to send.

---

## Chat history and long conversations

Conversations auto-save locally. The **History** button reopens or continues
any past chat; reopening a long one keeps the first and recent turns verbatim
and condenses the middle so it fits the model's context. Retention is
configurable under **Settings → Chat History**.

---

## The session workspace and code execution

Each conversation gets a scratch **workspace** on disk. Large tool results
spill there automatically (with a shape summary for big JSON), and the agent
uses `workspace_grep` / `workspace_read` to sift them without re-reading
everything into context. Two confirm-gated tools execute code in that
workspace: `run_python` (console interpreter) and `run_sql` (SQLite;
read-only queries run without confirmation). The confirmation gate is the
security control; to remove the capability entirely set:

```toml
[workspace]
code_exec = false
```

---

## How runnable output is rendered

When a runnable prints JSON, the console renders it as tables instead of raw
text: an array of objects becomes a table, a dict becomes a key/value view,
nested structures expand inline, and every block has a raw/table toggle and
copy action. Very large results render behind a brief placeholder and page in
chunks, so a 10k-row result doesn't freeze the UI. For the cleanest tables,
runnables should emit a top-level array of flat row objects.

---

## Under the hood: how tools reach the model

By default every runnable becomes one tool with a `host` parameter
(`tool_mode = "dedup"`). Large fleets can switch `[llm] tool_mode` to
`"search"`, where the agent gets three meta-tools (`search_runnables`,
`describe_runnable`, `run_runnable`) plus a compact catalogue index — fewer
tokens per turn, same reach. See the
[config reference](console-reference.md#llm).
