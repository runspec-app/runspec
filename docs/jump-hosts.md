# Jump Hosts

`runspec jump` runs a runspec tool on a remote machine over SSH+MCP. Pass the
SSH connection string directly on the command line; connection details (user,
port, key, ProxyJump, etc.) come from your `~/.ssh/config`. The binary speaks
MCP JSON-RPC over stdin/stdout to a `runspec serve` on the remote and streams
the result back.

---

## The model

```
agent / terminal                              remote host
    │                                            │
    │   runspec jump user@prod deploy            │
    │                                            │
    │   ssh -o BatchMode=yes user@prod           │
    │     runspec serve                          │
    │   ──────────────────────────────────────►  │
    │                                            │
    │   ◄──── MCP JSON-RPC over stdin/stdout ──► │
    │                                            │
    │   tools/call deploy --env prod             │
    │   ──────────────────────────────────────►  runspec serve
    │                                            │   spawns
    │                                            ▼
    │                                          deploy --env prod
    │   ◄──── stdout / stderr streamed back ──┤
    │   exit code propagated                  │
```

The runnables are defined and installed on the remote. The local machine only
needs `ssh` and `runspec`.

---

## Basic usage

```bash
# List tools available on the remote
runspec jump user@prod.example.com

# Run a tool
runspec jump user@prod.example.com deploy -- --env production

# Specify an exact runspec path on the remote
runspec jump user@prod.example.com --bin /opt/venv/bin/runspec deploy -- --env prod
```

Everything after `--` is forwarded to the tool on the remote. stderr from
the remote is streamed live — not buried in a log file you have to fetch.

---

## SSH configuration

All SSH options (user, port, private key, ProxyJump, timeouts, etc.) live in
`~/.ssh/config` for the given host. `runspec jump` passes the host string
directly to `ssh` with `BatchMode=yes` set (required because stdin/stdout are
the JSON-RPC channel).

`BatchMode=yes` is locked — interactive prompts would corrupt the protocol.
Use `ssh-agent` for keys that need a passphrase.

**Example `~/.ssh/config` entries:**

```
# Simple remote
Host prod
    HostName prod.example.com
    User deploy
    IdentityFile ~/.ssh/id_deploy

# Behind a bastion
Host internal
    HostName internal.example.com
    User deploy
    ProxyJump bastion.example.com
    ConnectTimeout 10

# CI target (non-standard port)
Host ci-target
    HostName 10.0.0.5
    User ci
    Port 2222
    IdentityFile /secrets/deploy_key
    UserKnownHostsFile /dev/null
    StrictHostKeyChecking no
```

Then:

```bash
runspec jump prod                              # uses Host entry "prod"
runspec jump internal deploy -- --env staging
runspec jump ci-target run-tests
```

---

## Remote `runspec` binary (`--bin`)

If `runspec` is not on the remote shell's PATH (common because SSH commands
run in a non-login shell and don't source `~/.bashrc`), pass the full path:

```bash
runspec jump user@prod --bin /opt/venv/bin/runspec
```

The `--bin` value can also be set via environment variable:

```bash
export RUNSPEC_JUMP_BIN=/opt/venv/bin/runspec
runspec jump user@prod deploy -- --env prod
```

Resolution cascade (first match wins):

1. `--bin` CLI flag
2. `RUNSPEC_JUMP_BIN` environment variable
3. `"runspec"` (relies on remote `PATH`)

**Basename validation:** the resolved path's basename must be `runspec` or
`runspec.exe`. Anything else is rejected before SSH runs — this prevents
accidental redirection to unrelated binaries.

### Trust model

`runspec jump` executes whatever binary lives at the resolved path on the
remote. Three forms of intent enforcement apply, but **no cryptographic
protection**:

1. **Basename locked to `runspec` / `runspec.exe`.** Accidental redirection
   is rejected before SSH runs.
2. **MCP handshake required.** Any process that doesn't speak JSON-RPC over
   stdio fails the `initialize` exchange and the call aborts.
3. **`stderr` streamed live.** Pre-exec output from a wrapper script appears
   in your terminal in real time.

These cover accidents, not adversaries. To harden further:

- Lock down remote filesystem permissions — the remote should not be writable
  by anyone not trusted to run code as you.
- Pin `--bin` to absolute paths under controlled directories (`/opt/...`,
  `/usr/...`) rather than user-writable locations.
- Audit the runspec install on the remote the same way you'd audit any
  `pip install`.

---

## Privilege escalation

When a tool needs to run as a different user on the remote, use `run_as`
together with `become_method` / `become_flags` in the runnable's definition.
These are resolved by `runspec serve` on the remote before the tool runs.

### Four `run_as` shapes

**1. Simple string** — same user on all hosts:

```toml
[deploy]
run_as = "oracle"
```

**2. Environment variable** — resolved at startup, useful when config
management (Ansible group_vars, Salt pillars, etc.) supplies the value:

```toml
[deploy]
run_as = "$ORACLE_RUN_AS"
```

**3. Per-host exact match** — empty string means no escalation on that host:

```toml
[deploy]
run_as.default = "oracle"
run_as.hosts."special-box-01" = "dba"
run_as.hosts."special-box-02" = ""        # no privilege escalation here
```

**4. Pattern matching** (regex, `re.fullmatch`, top-to-bottom, first match wins):

```toml
[deploy]
run_as.default = "oracle"
run_as.patterns."[lg]pexp[0-9]*" = "orasvc"
run_as.patterns."prod[0-9]*"     = "produser"
```

Forms 3 and 4 can be combined. Resolution order: exact `hosts` match →
first matching `patterns` entry → `default` → no escalation.

Invalid regex patterns cause `runspec serve` (and `runspec local`) to exit
with a clear error at startup.

### Become methods

```toml
[deploy]
run_as        = "oracle"
become_method = "sudo"      # default — also: su, pbrun, dzdo
become_flags  = "-H"        # passed through to the become method
```

| Method | Command constructed |
|---|---|
| `sudo` | `sudo {flags} -u {user} {command} {args}` |
| `su` | `su {flags} -c "{command} {args}" {user}` |
| `pbrun` | `pbrun {flags} -u {user} {command} {args}` |
| `dzdo` | `dzdo {flags} -u {user} {command} {args}` |

`su` uses a distinct `-c "..."` wrapping because it doesn't accept `-u`.

When the runnable declares both `env` args and `run_as`, the command is
prefixed with `env KEY=val ...` after the become so variables apply in the
target user's process context. This avoids requiring `sudo -E` / `env_keep`
in sudoers and needs no `sshd_config` changes.

---

## Cross-platform notes

`runspec jump` invokes the system `ssh` binary. This works identically on:

- **Linux / macOS** — OpenSSH is the system default.
- **Windows 10 (1809+) / Windows 11** — built-in OpenSSH Client at
  `C:\Windows\System32\OpenSSH\ssh.exe`, on `PATH` by default. The
  ssh-config lives at `C:\Users\<you>\.ssh\config`.

If `Get-Command ssh` doesn't find anything on a Windows machine, the
OpenSSH Client capability is disabled. Enable it (admin):

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
```

PuTTY, plink, and MobaXterm can coexist on the same machine but are not
used by runspec — the protocol-level requirement is OpenSSH semantics.

---

## See also

- [CLI Reference: `runspec jump`](cli.md#runspec-jump)
- [Format Reference: Remote execution](format.md#remote-execution)
- [Agent Integration](agents.md)
