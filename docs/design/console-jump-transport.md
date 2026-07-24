# runspec-console: system-ssh jump transport for legacy hosts

Status: **implemented** (runspec-console — this branch)
Branch: `claude/runspec-console-centos6-ssh-imiz1x`

## Problem

The console reaches every fleet host over **paramiko** (`executor._make_ssh_client`,
pooled in `ssh_pool.py`). paramiko **4.0 removed all SHA-1 and DSA algorithms**,
and the console pins `paramiko>=3.0`, which today resolves to **paramiko 5.x**. On
5.x these are gone entirely — not merely deprioritised:

```
Transport._preferred_keys → ('ssh-ed25519', 'ecdsa-*', 'rsa-sha2-512', 'rsa-sha2-256')
Transport._key_info        → no 'ssh-rsa', no 'ssh-dss'
paramiko.DSSKey            → deleted
```

CentOS 6 ships OpenSSH 5.3, which offers **only** `ssh-rsa` (RSA/SHA-1) and
`ssh-dss` (DSA) host keys and only accepts `ssh-rsa`/`ssh-dss` pubkey-auth
signatures (it predates ed25519/ecdsa/rsa-sha2). So on paramiko 5 there is no
overlap on either the host-key or the auth-signature negotiation → the handshake
fails. Key exchange and ciphers are **not** the blocker (`diffie-hellman-group-
exchange-sha256` and `aes*-ctr` still intersect); only the host-key + pubkey
signature algorithms are.

Because paramiko 5 *removed the implementations*, there is no in-process knob
(`disabled_algorithms` only disables; you can't re-enable code that isn't there),
and a paramiko-side ProxyJump / `direct-tcpip` tunnel doesn't help — the final
handshake to CentOS 6 is still performed by paramiko.

## Insight

`runspec jump` already solves the same problem for its MCP transport: it shells
out to the **system `ssh` binary** and lets `~/.ssh/config` supply the connection
(`jump.py` — including `ProxyJump` and `HostKeyAlgorithms +ssh-rsa`). The system
OpenSSH client *can* speak the legacy algorithms; paramiko can't.

The console's fleet transport is one narrow interface — `runner(ssh_target,
command) → (code, out, err)` — and everything above it (autonomy gating, audit,
streaming, the agent's `run_runnable`) is transport-agnostic. Only the runner is
hardwired to paramiko. So the fix is a **second transport** for flagged hosts that
uses system `ssh` instead.

## Design

A host entry gains one key:

```toml
[[host]]
name          = "old-centos6-1"
ssh           = "svc@old-centos6-1"
jump          = "bastion"          # NEW
runspec_paths = ["/opt/venvs/ops/bin/runspec"]
```

`jump` does two things:

1. **Selects the system-ssh transport** for this host (bypasses the paramiko
   pool entirely — no keepalive/backoff/channels; a per-call `ssh` subprocess).
2. **Sets the ProxyJump hop** — `ssh -J <jump> …`.

Everything else (the target's legacy `HostKeyAlgorithms`/`KexAlgorithms`, user,
port, further ProxyJump chaining) comes from `~/.ssh/config` for the target,
exactly the contract `runspec jump` already uses. Explicit host-entry
`identityFile`/`port` (and the global `[ssh] user`/`identityFile`) are passed as
flags and override ssh_config.

```
# ~/.ssh/config on the console machine
Host old-centos6-1
    HostKeyAlgorithms      +ssh-rsa
    PubkeyAcceptedAlgorithms +ssh-rsa
    KexAlgorithms          +diffie-hellman-group14-sha1,diffie-hellman-group-exchange-sha1
```

### Transport (`executor.py`)

- `_system_ssh_argv(ssh_target, jump, identity_file, cfg)` builds
  `["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=N", "-i", key, "-p", port,
  "-J", jump, target]`. `BatchMode=yes` forbids interactive prompts (a password
  prompt would hang a headless run) — key/agent auth only, like `runspec jump`.
- `ssh_jump_run(ssh_target, command, *, jump, …) -> (code, out, err)` mirrors
  `ssh_run` so it drops into the discovery / connectivity **runner** slot.
- `run_remote(…, jump="")` grows a `jump` param; when set it wraps the same
  `remote_cmd` string (built identically to the paramiko path — become/secrets/
  cred-env all intact) in the ssh argv and streams via the existing `_stream`
  subprocess helper. The remote command is one argv element executed by the
  remote login shell, exactly as paramiko's `exec_command` does, so quoting is
  unchanged.

### Routing (three call sites)

| Path | Site | Behaviour when `jump` set |
|---|---|---|
| Discovery | `bridge_internals._discover_one` | runner = `ssh_jump_run` bound to `jump` (instead of the pool runner) |
| Connectivity | `bridge_internals._check_connected` | `ssh_jump_run(ssh, "true", jump=…)`; `code == 0` = connected |
| Execution (Forms) | `bridge_invoke` | no `session_factory` (pool skipped); `run_remote(…, jump=…)` |
| Execution (agent) | `bridge_autonomy` | `run_remote(…, jump=…)` (already connect-per-call) |
| Copy key (bootstrap) | `bridge_config_ssh._push_key_over_password` | `_push_key_over_jump` — `sshpass` over `ssh -J` if present, else a manual `ssh-copy-id` command |

### Copy-key bootstrap (added after the first cut)

The "Copy key to host" / "Copy key to all hosts" bootstrap authenticates with a
**password** to install this machine's public key — but the regular jump
transport is `BatchMode=yes` (key/agent only), and password auth *before* any key
exists is a chicken-and-egg it can't do. So `_push_key_over_password` diverts a
`jump` host to `_push_key_over_jump`:

- **`sshpass` present** → `executor.ssh_jump_copy_id` feeds the password to system
  `ssh -J` via the `SSHPASS` env var (never argv). It forces
  `PubkeyAuthentication=no` (so the password is what's actually tested) and
  `StrictHostKeyChecking=accept-new` (mirrors the console's TOFU default).
- **`sshpass` absent** (stock Windows) → returns the exact `ssh-copy-id -o
  ProxyJump=… -i <key> <target>` command (`executor.jump_copy_id_command`) for the
  operator to run once in a terminal, instead of a confusing paramiko error. After
  that one-time push, the key-auth jump transport connects normally.

Non-`jump` hosts are untouched — still paramiko password auth.

### Editor round-trip

`jump` joins `_MANAGED_HOST_KEYS`; `get_jump_hosts`/`save_jump_hosts` surface and
persist it; `validate_editor_hosts` requires it to be a non-empty string backed by
an ssh/hostname target. The Hosts editor gets a **Jump host (ProxyJump)** field,
and the agent's `manage_hosts` config tool exposes `jump`.

## Scope / limits (first cut)

- **In scope:** connectivity probe, discovery (`runspec local`), and runnable
  execution (Forms + agent, streamed output, `run_as`/secrets/cred-env) for
  `jump` hosts.
- **Out of scope:** SFTP file transfer and ad-hoc `direct-tcpip` to these hosts —
  those are paramiko-channel features; they'd need `scp`/`sftp` subprocess
  equivalents. They stay on the paramiko path and simply won't work for a `jump`
  host until ported.
- **Requirement:** an OpenSSH client on PATH on the **console machine** (Windows
  10+ has it; may need enabling) with the target's legacy algorithms in
  `~/.ssh/config`. Same assumption `runspec jump` already makes.
- **DSA-only targets:** OpenSSH 9.8 removed DSA outright, so `ssh-dss` can't be
  re-enabled on a very new client. Prefer an RSA host key on the target
  (`ssh-keyscan -t rsa <host>` to confirm); `ssh-rsa`/SHA-1 stays re-enablable via
  `+ssh-rsa`.

## Alternatives considered

- **Pin `paramiko<4`.** Restores `ssh-rsa`/`ssh-dss` fleet-wide with no code, but
  freezes the whole fleet's transport below paramiko 4/5. Rejected: a per-host
  opt-in that keeps modern hosts on modern paramiko is cleaner.
- **Route through `runspec jump`'s MCP path.** Reuses its transport but fights the
  console's own autonomy/audit/streaming stack, which sits on the
  `(code,out,err)` runner, not MCP `tools/call`. A plain system-ssh runner drops
  in with far less surface.
- **Auto-inject `-o HostKeyAlgorithms=+ssh-rsa`.** Would make a `jump` host work
  with zero ssh-config editing, but bakes an algorithm policy into the app.
  Deferred; `~/.ssh/config` is the honest home for it and matches `runspec jump`.
