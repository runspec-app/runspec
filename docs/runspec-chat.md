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
runspec-chat --port 9000                  # custom port
runspec-chat --model claude-opus-4-7      # override model
runspec-chat --watch                      # reload on file changes (dev)
runspec-chat --headless                   # don't open browser automatically
runspec-chat --host 127.0.0.1             # bind to a specific interface
runspec-chat --root-path /chat            # serve under a path prefix (reverse proxy)
runspec-chat --ssl-cert cert.pem --ssl-key key.pem  # enable HTTPS
```

| Argument | Default | Description |
|---|---|---|
| `--port` / `-p` | `8000` | Port for the web server |
| `--host` | `0.0.0.0` | Network interface to bind to |
| `--model` | `claude-haiku-4-5` | LLM model identifier |
| `--hosts` / `-H` | `~/.config/runspec-chat/jump_hosts.toml` | Hosts config path |
| `--watch` / `-w` | off | Reload on source file changes |
| `--headless` | off | Suppress auto-opening the browser |
| `--root-path` | — | Root path for reverse proxy deployments |
| `--ssl-cert` | — | SSL certificate file (enables HTTPS) |
| `--ssl-key` | — | SSL private key file (enables HTTPS) |

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

### jump_hosts.toml

Copy `jump_hosts.toml.example` (bundled with the package) to
`~/.config/runspec-chat/jump_hosts.toml` and edit it:

```toml
[config]
user = "myuser"      # default SSH username for all hosts

[hosts.dev-box]
ssh = "192.168.1.10"
runspec_path = "/home/myuser/.venv/bin/runspec"
category = "devops"  # optional — shown in connection messages and command descriptions

[hosts.build-server]
ssh = "build.internal"
user = "builder"     # overrides config.user
runspec_path = "/opt/runspec/.venv/bin/runspec"
category = "ci"
```

| Field | Required | Description |
|---|---|---|
| `ssh` | Yes (for remote hosts) | Hostname or IP to connect to |
| `runspec_path` | Yes | Path to `runspec` on that host |
| `user` | No | SSH username — overrides `[config].user` for this host |
| `category` | No | Label shown in the UI — falls back to the host name if omitted |

### Tool categories

Each connection is labelled with its category. When you connect a host, the
startup message includes a `── {category} ──` header. The slash-command
autocomplete prefixes every tool description with `[category]`:

```
deploy          ← command id (bold)
[devops] Deploy the app to production   ← description (muted, below id)
```

Local tools (the auto-started venv tools) always use the `local` category.

!!! tip "Naming convention"
    Name your Chainlit connection identically to the host key in
    `jump_hosts.toml` (e.g. `dev-box`) so the category resolves automatically.
    If the names don't match, the connection name itself is used as the category.

### Settings (⚙ gear icon)

The browser settings panel shows credential fields based on your `jump_hosts.toml`:

| Field | When it appears |
|---|---|
| `ANTHROPIC_API_KEY` | Always — skip if set in your environment |
| `SSH_PASS` | When any SSH host uses the default `[config].user` |
| `SSH_<HOST>_PASS` | For each host that declares its own `user` |

SSH passwords are session-scoped: never written to disk, gone when you close
the tab. They are only needed to run `/setup-keys` — once your key is installed
on each host, you never need them again.

### Session user identity

runspec-chat resolves the OS identity of the person who started the server and
surfaces it in two places:

- **Startup message** — `running as **Jason Finestone (jason)**` appears in the
  local tools ready line so you can confirm which account is active at a glance.
- **System prompt** — the resolved identity is injected into every session so
  the assistant is aware of who it's working with. This is useful when tools
  care about the invoking user (audit trails, personalised output, etc.).

Resolution order:

| Platform | Source |
|---|---|
| Linux / Mac | GECOS full name from `/etc/passwd` (`pwd.getpwuid`) |
| Windows (domain-joined) | AD display name via `pywin32` `GetUserNameEx` |
| Fallback | `USER` / `LOGNAME` / `USERNAME` environment variable |

The identity is the OS user who launched `runspec-chat`, not the browser user.
No user-input name — avoids spoofing in shared environments.

---

## Setting up SSH keys

The first time you connect a new host, run:

```
/setup-keys
```

or just ask:

> set up my SSH keys for all configured hosts

This generates an `ed25519` key at `~/.ssh/runspec-chat_ed25519` (if it doesn't
already exist), then copies the public key to every host in your `jump_hosts.toml`
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
