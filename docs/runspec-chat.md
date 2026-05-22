# runspec-chat

A browser-based chat interface for your runspec tools. Describe what you want
in plain language, or call tools directly with slash commands. Tools run on your
local machine or on remote hosts connected over SSH.

---

## Install

```bash
pip install "runspec-chat[anthropic]"
```

The `anthropic` extra pulls in the Anthropic SDK. If you already have an
`ANTHROPIC_API_KEY` set in your environment, that's all you need.

---

## Run

```bash
runspec-chat
```

Opens on `http://0.0.0.0:8000` by default.

```bash
runspec-chat --port 9000          # custom port
runspec-chat --model claude-opus-4-7  # override model
```

---

## How it works

```
  your browser
       │
  runspec-chat  ──── LLM (Anthropic / Bedrock / OpenAI ...)
  this machine         API key from Settings ⚙
       │
       ├── local: runspec serve  (starts automatically)
       │          └─ any pip-installed runspec tool
       │
       └── ssh user@host  runspec serve  (add via plug icon)
                  └─ tools on that host
```

The app runs on your machine. The browser is just the UI. Tools run on
whichever hosts you connect — local or remote via SSH.

---

## Configuration

### hosts.toml

Copy `hosts.toml.example` (bundled with the package) to
`~/.config/runspec-chat/hosts.toml` and edit it:

```toml
[config]
user = "myuser"      # default SSH username for all hosts

[hosts.dev-box]
ssh = "192.168.1.10"
runspec_path = "/home/myuser/.venv/bin/runspec"

[hosts.build-server]
ssh = "build.internal"
user = "builder"     # overrides config.user
runspec_path = "/opt/runspec/.venv/bin/runspec"
```

- `[config].user` — default SSH username, used for all hosts that don't set their own
- Per-host `user` — overrides the default for that host only
- `ssh` — hostname (or IP) to connect to
- `runspec_path` — path to `runspec` on that host

### Settings (⚙ gear icon)

The browser settings panel shows credential fields based on your `hosts.toml`:

| Field | When it appears |
|---|---|
| `ANTHROPIC_API_KEY` | Always — skip if set in your environment |
| `SSH_PASS` | When any SSH host uses the default `[config].user` |
| `SSH_<HOST>_PASS` | For each host that declares its own `user` |

SSH passwords are session-scoped: never written to disk, gone when you close
the tab. They are only needed to run `/setup-keys` — once your key is installed
on each host, you never need them again.

---

## Setting up SSH keys

The first time you connect a new host, run:

```
/setup-keys
```

or just ask:

> set up my SSH keys for all configured hosts

This generates an `ed25519` key at `~/.ssh/runspec-chat_ed25519` (if it doesn't
already exist), then copies the public key to every host in your `hosts.toml`
using the password from Settings.

After keys are installed, add to `~/.ssh/config`:

```
Host *
    IdentityFile ~/.ssh/runspec-chat_ed25519
```

!!! note "Windows"
    `sshpass` and `ssh-copy-id` are not available on Windows. `setup-keys`
    generates the key and prints the exact command to run once to copy it
    manually. Everything else works identically.

---

## Connecting remote hosts

Click the **plug icon** (top-right, MCP button) and add a stdio connection:

| Target | Command |
|---|---|
| Remote host via SSH | `ssh user@host /path/to/.venv/bin/runspec serve` |
| Local (alternative venv) | `/path/to/.venv/bin/runspec serve` |

Once connected, all tools on that host are immediately available for slash
commands and natural language.

---

## Adding tools

Any runspec runnable installed into the **same venv as runspec-chat** is
discovered automatically:

```bash
pip install my-runspec-tool
# open a new chat — tool appears in "Local tools ready"
```

`runspec serve` uses `importlib.metadata` to find all installed packages with a
`runspec.toml`. The venv is the inventory — no config, no restart needed.

---

## Slash commands

Type `/` to see available tools. Call any tool directly, bypassing the LLM:

```
/scan --directory /data --older-than 30
/clean --directory /tmp --delete
/setup-keys
/setup-keys --key-type rsa
```

Add `--help` to any command to see its declared arguments:

```
/scan --help
```

---

## Natural language

Describe what you want — the assistant picks the right tool and arguments:

> scan the /data directory for files older than 30 days  
> clean up /tmp, delete anything older than a week  
> set up my SSH keys

The assistant will explain what it's doing before calling each tool, and
summarise the result afterwards.

---

## LLM providers

The default adapter uses the Anthropic SDK. The model can be overridden:

```bash
runspec-chat --model claude-sonnet-4-6
```

Or set permanently via environment variable:

```bash
export RUNSPEC_CHAT_MODEL=claude-sonnet-4-6
runspec-chat
```

Additional provider adapters (Bedrock, OpenAI, etc.) can be added by
implementing the `ModelAdapter` interface in `runspec_chat/adapter.py`.
