# Jump Hosts

`runspec jump` runs a runspec tool on a remote machine over SSH+MCP. The
remote configuration lives in `[config.jump-hosts]` in your local
`runspec.toml`; the binary itself does the SSH handshake, speaks MCP
JSON-RPC over stdin/stdout to a `runspec serve` on the remote, and streams
the result back.

!!! note "Replaces the removed registry service"
    Earlier versions of runspec shipped a separate `runspec-registry` HTTP
    service for tool discovery via heartbeat polling. That was removed in
    0.7.0; the package on PyPI is archived. `[config.jump-hosts]` is the
    current — and only — remote-execution model.

---

## The model

```
agent / terminal                              jump host
    │                                            │
    │   runspec jump prod-app deploy             │
    │   ── parses [config.jump-hosts.prod-app]   │
    │                                            │
    │   ssh -o BatchMode=yes prod-app            │
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

The local `runspec.toml` only needs to know how to reach the jump host. The
runnables themselves are defined and installed on the remote.

---

## Configuring a jump host

`[config.jump-hosts.<alias>]` declares a target. The alias is what you type:
`runspec jump prod-app` looks up `[config.jump-hosts.prod-app]`.

Every field is optional. The minimal config is just the section header — the
alias becomes the SSH host name and everything else comes from
`~/.ssh/config`.

| Field | Type | Default | Env fallback | Description |
|---|---|---|---|---|
| `host` | string | the alias | — | Hostname or IP. Usually matches a `Host` entry in `~/.ssh/config`. |
| `bin` | string | `"runspec"` | `RUNSPEC_JUMP_BIN` | Path to the `runspec` binary on the remote. Basename **must** be `runspec` (or `runspec.exe`). |
| `user` | string | — | — | SSH user (becomes `user@host`). |
| `port` | int | `22` | — | SSH port. Emitted as `-p N` only when non-default. |
| `ssh-key` | string | — | — | Path to private key (becomes `-i <path>`). |
| `use-ssh-config` | bool | `true` | — | When `false`, ssh runs with `-F /dev/null` and ignores `~/.ssh/config` entirely. |
| `ssh-options` | array of string | `[]` | — | Extra `-o KEY=VALUE` options passed to ssh. Each item becomes one `-o` flag. |

### Four typical setups

=== "Rely on ssh-config"

    The cleanest setup. Put per-host config in `~/.ssh/config`, give the
    alias the same name as the `Host` entry:

    ```toml
    [config.jump-hosts.prod-app]
    # everything (user, port, key, ProxyJump) comes from ssh-config
    ```

=== "Literal hostname with friendly alias"

    Useful when the alias is a project-readable name but ssh-config doesn't
    have it:

    ```toml
    [config.jump-hosts.shorty]
    host = "actual.hostname.internal.example.com"
    ```

=== "Ignore ssh-config (CI / shared environments)"

    ```toml
    [config.jump-hosts.ci-target]
    host           = "10.0.0.5"
    user           = "deploy"
    ssh-key        = "/secrets/deploy_key"
    use-ssh-config = false
    port           = 2222
    ```

=== "Pass-through ssh-options"

    For anything ssh-config supports but the TOML doesn't have a dedicated
    field for:

    ```toml
    [config.jump-hosts.bastion-fronted]
    host        = "internal.example.com"
    ssh-options = [
      "ProxyJump=bastion.example.com",
      "ConnectTimeout=10",
      "ServerAliveInterval=30",
    ]
    ```

---

## Listing and invoking

### List configured aliases

```bash
runspec jump --list-jump-hosts
runspec jump --list-jump-hosts --format json
```

Text output shows the effective `bin` value (the field's default of
`runspec`, the explicit override, or `$RUNSPEC_JUMP_BIN`):

```
Configured jump hosts (/home/user/project/runspec.toml):

  prod-app          host: prod-app                    bin: runspec
  shorty            host: actual.hostname.example.com bin: runspec
  ci-target         host: 10.0.0.5 (user: deploy)     bin: runspec
```

### List tools on a host

```bash
runspec jump prod-app
```

SSHes to the host, speaks MCP, and lists the tools the remote exposes —
exactly what an agent connected to that host would see.

### Run a tool

```bash
runspec jump prod-app deploy -- --env production
runspec jump ci-target run-migrations -- --schema users --dry-run
```

Everything after `--` is forwarded to the tool on the remote (the
positional `rest`-type arg). The local CLI parses out the alias and tool;
everything else is the remote tool's argv.

stderr from the remote is streamed live, so when something goes wrong on
the other side you see it in real time — not buried in a log file you have
to fetch.

---

## SSH argv construction

`runspec jump` shells out to the system `ssh` binary. The argv order
matters because OpenSSH uses first-value-wins for command-line options:

```
ssh -o BatchMode=yes        ← always; locked because stdin is JSON-RPC
    [-F /dev/null]          ← when use-ssh-config = false
    [-p PORT]               ← when port ≠ 22
    [-i SSH-KEY]            ← when ssh-key is set
    [-o OPT]...             ← each ssh-options item
    [user@]host bin serve
```

`BatchMode=yes` is locked because `runspec jump` pipes JSON-RPC over
stdin/stdout — interactive prompts would corrupt the protocol. Use
`ssh-agent` for keys that need a passphrase.

Explicit fields (`port`, `ssh-key`) appear in argv before `ssh-options`, so
on conflict the explicit field wins. If you specify both `port = 2222` and
`ssh-options = ["Port=99"]`, the connection uses port 2222.

---

## Restricting where a runnable can run

The runnable-side counterpart to `[config.jump-hosts]` is the `hosts` field
on a runnable. `runspec serve` checks the current hostname at startup;
tools that don't match are excluded from the MCP tool list:

```toml
[parse-app-logs]
description = "Parse and summarise application logs"
autonomy    = "confirm"
hosts       = ["logserver-01", "logserver-02"]
```

On any other host, `parse-app-logs` is invisible. The `hosts` field protects
against accidental invocation on the wrong machine — it's not a security
boundary, just an availability filter.

---

## Privilege escalation

When a tool needs to run as a different user on the remote, use `run_as`
together with `become_method` / `become_flags`. These are resolved by
`runspec serve` on the remote against the local hostname before the tool
runs.

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
target user's process context. This avoids requiring `sudo -E` /
`env_keep` in sudoers and needs no `sshd_config` changes.

---

## Remote `runspec` binary resolution

The `bin` field is locked to executables named `runspec` (or
`runspec.exe`). The resolution cascade is:

1. Explicit `bin` field in `[config.jump-hosts.<alias>]`
2. `RUNSPEC_JUMP_BIN` environment variable
3. The remote's `PATH`

```toml
[config.jump-hosts.prod-app]
bin = "/opt/runspec/bin/runspec"     # pinned absolute path
```

If the resolved path doesn't exist on the remote (or its basename isn't
`runspec`), `runspec jump` fails before invoking SSH with a message that
distinguishes between "the bin field was explicit" and "we tried `PATH`".

### Trust model

`runspec jump` ultimately executes whatever binary lives at the resolved
`bin` path. The format provides three forms of intent enforcement, but **no
cryptographic protection**:

1. **Basename is locked to `runspec` / `runspec.exe`.** Accidental
   redirection (`bin = "/usr/bin/cat"`) is rejected before SSH runs.
2. **MCP handshake required.** Any process that doesn't speak JSON-RPC over
   stdio fails the `initialize` exchange and the call aborts.
3. **`stderr` is streamed live.** Anything the remote writes to stderr —
   including pre-exec output from a wrapper script — appears in the user's
   terminal in real time, not hidden behind a log file.

These cover accidents (typos, stale values, wrong paths), not adversaries.
The format **cannot** distinguish a real `runspec` binary from a wrapper
named `runspec` that runs malicious code and then `exec`s the real binary.

If your threat model includes that, defences live above runspec:

- **Treat `runspec.toml` like shell config.** Audit changes via PR review;
  don't accept TOMLs from untrusted sources without reading them.
- **Lock down remote filesystem permissions.** The remote should not be
  writable by anyone who isn't trusted to run code as you.
- **Pin `bin` to absolute paths under controlled directories.** Prefer
  `/opt/...` or `/usr/...` paths managed by configuration management over
  user-writable locations like `/tmp` or `$HOME` subdirectories. A `bin =
  "/tmp/foo/runspec"` in a checked-in TOML is a red flag worth questioning.

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

## Where this fits in the changelog

| Change | Version |
|---|---|
| `runspec-registry` removed; replaced by `[config.jump-hosts]` model | [0.7.0](changelog.md#070-2026-05-18) |
| `--list-jump-hosts` flag, locked `bin` basename, `RUNSPEC_CONFIG` forwarding | [0.9.0](changelog.md#090-2026-05-19) |

---

## See also

- [CLI Reference: `runspec jump`](cli.md#runspec-jump)
- [Format Reference: Remote execution](format.md#remote-execution)
- [Agent Integration](agents.md)
