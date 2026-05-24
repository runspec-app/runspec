# runspec-chat

Chat with your runspec tools via natural language or slash commands.

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

## Quick start

**1. Create your hosts config**

Copy `jump_hosts.toml.example` to `~/.config/runspec-chat/jump_hosts.toml` and edit it
with your server addresses.

**2. Enter your credentials in Settings**

Click the **⚙ gear icon** (top-right) to open Settings. Fields shown depend on
your `jump_hosts.toml`:

| Field | What to enter |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key (`sk-ant-...`) — not needed if set in `.env` |
| `SSH_PASS` | SSH password — appears when hosts share the default `user` from `[config]` |
| `SSH_<HOST>_PASS` | Per-host password — appears for any host that defines its own `user` |

SSH password fields are only needed to run `/setup-keys`. Once your key is
installed on each host the password is never required again — just close the
tab and it's gone.

**3. Set up SSH keys**

Ask the assistant or type directly:

```
/setup-keys
```

On Linux/Mac this generates an ed25519 key and copies it to every host in your
config automatically. You will not be prompted in the terminal.

On Windows, `setup-keys` generates the key and shows you the exact command to
run once to copy it — Windows does not ship `sshpass` or `ssh-copy-id`.

After keys are in place, add to `~/.ssh/config` (path shown after setup):

```
Host *
    IdentityFile ~/.ssh/runspec-chat_ed25519
```

**4. Connect a remote host**

Click the **plug icon** (MCP button) and add a stdio connection:

| Where | Command |
|---|---|
| Remote host via SSH | `ssh user@host /path/to/.venv/bin/runspec serve` |
| Local (this machine) | `/path/to/.venv/bin/runspec serve` |

Once connected, all tools on that host are available immediately.

Name the connection identically to the host key in `jump_hosts.toml` (e.g.
`dev-box`) so the category from your config is shown in connection messages
and slash-command descriptions. If the names don't match, the connection name
itself is used as the category.

---

## Adding tools

Any runspec runnable installed into the **same venv as runspec-chat** becomes
available automatically — no config, no restart, just a new chat session:

```bash
pip install my-runspec-tool
# open a new chat — tool appears in "Local tools ready"
```

`runspec serve` discovers all installed packages with a `runspec.toml` at
startup. The venv is the inventory.

---

## Slash commands

Call a tool directly, bypassing the LLM — instant, free, deterministic:

```
/scan --directory /data --older-than 30
/clean --directory /tmp --delete
/setup-keys
/setup-keys --key-type rsa
```

Use `--help` on any tool to see its arguments.

---

## Natural language

Describe what you want — the assistant picks the right tool and arguments:

> scan the /data directory for files older than 30 days
> set up my SSH keys for all configured hosts

---

## Windows notes

- **ssh.exe** is built into Windows 10 and 11 via the OpenSSH Client feature.
  If `ssh` is not found, enable it:
  `Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0` (admin required).
- **sshpass** and **ssh-copy-id** are not available on Windows. `setup-keys`
  generates the key and shows you a manual copy command instead.
- Everything else — connecting hosts, natural language, slash commands — works
  identically on Windows.
