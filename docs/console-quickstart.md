# Get started with the console

`runspec-console` is a desktop application that puts an AI agent in front of
your operational tooling. It discovers every runnable installed on your machine
and across your SSH fleet, lets you run them from typed forms or plain
conversation, and automates them with triggers, schedules, and runbooks — all
behind a confirm-by-default safety gate.

This page takes you from nothing to a working console with an AI agent in
about ten minutes. You don't need to write any code, and you don't need to
read the rest of the documentation first — once the agent is connected, it can
explain and drive most of the product from inside the app.

---

## Requirements

- **Windows 10/11** for the full feature set. The console itself also runs on
  macOS and Linux, but several features are Windows-only: event triggers,
  Outlook/Microsoft 365 tools, desktop toast notifications, and title-bar
  theming.
- **Python 3.11 or newer** — check with `python --version`.
- An **Anthropic API key** (or OpenAI / AWS Bedrock credentials) for the AI
  agent. You can get an Anthropic key at
  [console.anthropic.com](https://console.anthropic.com/).

!!! note "macOS / Linux"
    Everything on this page works on macOS/Linux except where marked
    **Windows**. The Windows-only features degrade gracefully — their tabs
    simply report themselves unavailable.

---

## 1. Install

Install into a dedicated virtual environment — the environment *is* the
console's runnable catalogue, so keeping it separate keeps it deliberate:

```
python -m venv %USERPROFILE%\runspec-console-venv
%USERPROFILE%\runspec-console-venv\Scripts\activate
pip install "runspec-console[anthropic]"
```

(macOS/Linux: `python3 -m venv ~/runspec-console-venv && source
~/runspec-console-venv/bin/activate` and the same `pip install`.)

The `[anthropic]` extra installs the Claude adapter. Other extras you can add
now or later:

| Extra | What it enables |
|---|---|
| `anthropic` / `openai` / `bedrock` / `langserve` | AI provider adapters (pick at least one) |
| `credentials` | Named credentials with secrets in the OS keychain |
| `git-sync` | Config Sync — pull team config from a git repo |
| `schedule` | Cron/interval schedules |
| `notifications` | **Windows** — toast trigger source + confirm notifications |
| `all` | Everything above |

```
pip install "runspec-console[all]"
```

On **Windows**, installing the console automatically pulls in
[`runspec-windows`](https://pypi.org/project/runspec-windows/), so ~40 system
administration, Microsoft 365, and web/TLS diagnostic runnables are available
in the UI immediately — the agent has real tools to work with out of the box.
On Linux, `pip install runspec-linux` gives you the equivalent pack (31
runnables).

---

## 2. First launch

```
runspec-console
```

No configuration is needed for the first start. You'll see:

- a **sidebar** listing hosts — just `local` (this machine) for now;
- the **Console** tab — the agent chat (empty until you add an API key in the
  next step);
- **Forms** — every discovered runnable as a typed run dialog;
- **Triggers**, **Schedules**, **Rota** — automation, covered in
  [Automation](console-automation.md);
- **History** — past runs and conversations.

Open **Forms** and you'll already see the runnables from `runspec-windows`
(or whatever runspec packages are installed in the environment). The console
lists whatever `runspec local` discovers: any installed package that ships a
`runspec.toml`. There is no registry and no filesystem scanning — `pip
install` into the console's environment is how a runnable becomes visible.

All configuration the console writes lives in one folder:
`%APPDATA%\runspec-console\` (on macOS/Linux: `~/AppData/Roaming/runspec-console/`).
You'll rarely need to edit these files by hand — the Settings UI manages them —
but everything is plain TOML and safe to inspect.

---

## 3. Connect the AI

This is the one step the agent can't do for you — it isn't running yet.

1. Open **Settings → Model / API**.
2. Set **Provider** to `Anthropic`.
3. Paste your **API key**.
4. Add a model id to the **Available models** list — e.g.
   `claude-sonnet-4-6` — and select it as the active model.
5. Save.

The equivalent configuration lands in `config.toml` as:

```toml
[llm]
provider = "anthropic"
api_key  = "sk-ant-..."
model    = "claude-sonnet-4-6"
models   = ["claude-sonnet-4-6"]
```

!!! tip "Corporate environments"
    If static API keys aren't allowed, `api_key_command` runs a command whose
    output is used as a short-lived token (with `api_key_ttl_ms` controlling
    refresh), and `base_url` points the adapter at an internal proxy. AWS
    Bedrock and LangServe gateway adapters are also available. See the
    [config reference](console-reference.md#llm) for every knob.

---

## 4. Talk to the agent

Switch to the **Console** tab and start with these — paste them as-is:

> **What can you do? List the tools and runnables you have available on this machine, grouped by what they're for.**

The agent will enumerate its built-in tools and every discovered runnable —
this is the fastest way to learn what your installation can actually do.

> **Show me this machine's system info and disk usage.**

The agent picks the right runnables and runs them. The first time it wants to
run something, you'll see a **confirmation card**: runnables default to
`confirm` autonomy, so the agent proposes, you approve. You can edit the
arguments on the card before approving. This gate is the console's core safety
model — an agent action you didn't approve doesn't happen, and runnables
marked `manual` are never run by the agent at all.

> **What features does this console have that I haven't set up yet? Walk me through what each one is for.**

The agent knows its own configuration state — hosts, triggers, schedules,
rota, memory, and more — and can both explain each feature and set it up with
you directly in chat: adding your SSH hosts, creating triggers and schedules,
setting working hours. Every change shows a confirmation card first. The one
thing it can never configure is its own Model / API setup (step 3).

From here, the [working with the agent](console-agent.md) page is a cookbook
of prompts for everything the agent can do — and is honest about the few
things that still need the Settings UI.

---

## 5. Where to go next

- **[Working with the agent](console-agent.md)** — the prompt cookbook: run
  things, save runbooks, schedule work, analyse logs, manage the fleet.
- **[Hosts & fleet](console-hosts.md)** — add your servers over SSH and run
  the same runnables across all of them.
- **[Automation](console-automation.md)** — react to events (triggers), run
  on a clock (schedules), and keep it all inside working hours (rota).
- **[Adding your own runnables](authoring-intro.md)** — turn your own scripts
  into runnables the console and agent can use.

---

## Launch flags

```
runspec-console                       # normal start
runspec-console --devtools            # enable the Chromium inspector
runspec-console --dev                 # point at a running Vite dev server (UI development)
runspec-console --config-dir ./cfg    # use an isolated config directory (testing)
```

`--config-dir <dir>` points the console at a specific directory for **all** its
state — hosts, credentials, triggers, `config.toml`, everything under the
default `%APPDATA%/runspec-console` — so you can test against a throwaway config
without touching your real profile. The `RUNSPEC_CONSOLE_DIR` environment
variable does the same (the flag wins if both are set).
