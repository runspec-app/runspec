# runspec Format Specification

Version: 1  
Status: Draft

This document is the canonical definition of the runspec format.
All language pack implementations are tested against this specification.

---

## Overview

A runspec defines the interface of anything runnable — a script, application,
or MCP tool. It lives in `runspec.toml` inside the package directory, alongside
the code it describes.

```
mypkg/
  runspec.toml    ← lives here, not at the project root
```

This location means build backends include it automatically as package data,
and `importlib.metadata` can locate it after install — no extra configuration needed.

---

## File Lookup Order

Implementations must use the following lookup strategy depending on context:

**Runnable discovery (`runspec local`, `runspec serve`):**
1. Locate via `importlib.metadata` — find `runspec.toml` in installed package files
2. Any package that declares `runspec` as a dependency is a candidate
3. Packages must be installed (`pip install` or `pip install -e .`) to be visible.
   There is no filesystem-scanning fallback; install is the convention.

**Local config lookup (`runspec jump` for `[config.jump-hosts]`):**
1. Walk up from cwd, return the first `runspec.toml` found

---

## Top-Level Structure

```toml
[config]     # optional project-wide defaults
[<name>]     # one section per runnable
```

---

## The `[config]` Section

Project-wide defaults. All fields are optional.

| Field | Type | Default | Description |
|---|---|---|---|
| `autonomy-default` | string | `"confirm"` | Autonomy when unspecified on a script |
| `lang` | string | — | Preferred language for `runspec generate` |
| `name` | string | — | MCP server name reported by `runspec serve`. Defaults to the venv directory name. |
| `version` | string | `"1"` | runspec spec version |
| `jump-hosts` | table | — | Per-alias jump host config. See [Jump Hosts](#jump-hosts). |
| `logging` | table | — | Logging configuration. See [Logging](#logging). |

### Jump Hosts

`[config.jump-hosts.<alias>]` declares a target for `runspec jump`. The
alias is the dict key — `runspec jump prod` looks up
`[config.jump-hosts.prod]`. Each alias entry accepts these fields:

| Field | Type | Default | Env fallback | Description |
|---|---|---|---|---|
| `host` | string | the alias | — | Hostname or IP. Usually matches a `Host` entry in `~/.ssh/config`; can be a literal hostname when ssh-config isn't involved. |
| `bin` | string | `"runspec"` | `RUNSPEC_JUMP_BIN` | Path to the `runspec` binary on the remote. Basename **must** be `runspec` (or `runspec.exe` on Windows) — the field is locked to runspec and cannot be redirected to other executables. |
| `user` | string | — | — | SSH user (becomes `user@host`). |
| `port` | int | `22` | — | SSH port. Only emitted as `-p N` when non-default. |
| `ssh-key` | string | — | — | Path to private key (becomes `-i <path>`). |
| `use-ssh-config` | bool | `true` | — | When `false`, ssh runs with `-F /dev/null` and ignores `~/.ssh/config` entirely. |
| `ssh-options` | array of string | `[]` | — | Extra `-o KEY=VALUE` options passed through to ssh. Each item becomes one `-o` flag. |

#### ssh argv construction

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

Explicit fields (`port`, `ssh-key`) appear in argv before `ssh-options`,
so on conflict the explicit field wins. If you specify both `port = 2222`
and `ssh-options = ["Port=99"]`, the connection uses port 2222.

#### Trust model

`runspec jump` ultimately executes whatever binary lives at the resolved
`bin` path on the remote. The format provides three forms of intent
enforcement, but no cryptographic protection:

1. **`bin` basename is locked to `runspec` / `runspec.exe`.** A naive
   redirection (e.g. `bin = "/usr/bin/cat"`) is rejected before SSH runs.
2. **MCP handshake required.** Any process that doesn't speak JSON-RPC
   over stdio fails the `initialize` exchange and the call aborts.
3. **`stderr` is streamed live.** Anything the remote process writes to
   stderr (including pre-exec output from a wrapper script) appears in
   the user's terminal in real time, not hidden behind a log file.

These cover accidents (typos, stale values, wrong paths), not adversaries.
The format **cannot** distinguish a real `runspec` binary from a wrapper
script named `runspec` that runs malicious code and then `exec`s the real
binary. Anyone with write access to either the local `runspec.toml` or
the remote filesystem can route execution through a wrapper that's
indistinguishable from a legitimate binary at the MCP layer.

If your threat model includes that, the defenses live above runspec:

- **Treat `runspec.toml` like shell config.** Audit changes via PR review;
  don't accept TOMLs from untrusted sources without reading them.
- **Lock down remote filesystem permissions.** The remote should not be
  writable by anyone who isn't trusted to run code as you (the standard
  SSH trust assumption).
- **Pin `bin` to absolute paths under controlled directories.** Prefer
  `/opt/...` or `/usr/...` paths managed by configuration management
  (Ansible, Salt, etc.) over user-writable locations like `/tmp` or
  `$HOME` subdirectories. A `bin = "/tmp/foo/runspec"` in a checked-in
  TOML is a red flag worth questioning.
- **Audit the runspec install on the remote.** The same way you'd audit
  any binary you `pip install`. The package itself is not signed; rely
  on PyPI's chain of trust and your venv's integrity.

The `bin` field is documented as the runspec executable path. Treating
it as anything else — even temporarily, even "just to test something" —
breaks the trust model. The basename check exists to make that
violation impossible to accidentally encode in a TOML; it does not make
the field tamper-proof against a deliberate local actor.

#### Cross-platform notes

`runspec jump` invokes the system `ssh` binary. This works identically on:

- **Linux / macOS** — OpenSSH is the system default.
- **Windows 10 (1809+) and Windows 11** — built-in OpenSSH Client at
  `C:\Windows\System32\OpenSSH\ssh.exe`, on PATH by default. The
  ssh-config lives at `C:\Users\<you>\.ssh\config`. PuTTY / plink /
  MobaXterm coexist on the same machine but are not used by runspec —
  the protocol-level requirement is OpenSSH semantics.

If `Get-Command ssh` doesn't find anything on a Windows machine, the
OpenSSH Client capability is disabled. Enable it (admin) with:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
```

#### Typical usage patterns

**Rely on ssh-config** — the cleanest setup. Put per-host config in
`~/.ssh/config`, give the alias the same name as the `Host` entry:

```toml
[config.jump-hosts.prod-app]
# everything (user, port, key, ProxyJump) comes from ssh-config
```

**Literal hostname with alias-as-label** — when the alias is a friendly
project name but ssh-config doesn't have it:

```toml
[config.jump-hosts.shorty]
host = "actual.hostname.internal.example.com"
```

**Ignore ssh-config entirely** — useful in CI or shared environments:

```toml
[config.jump-hosts.ci-target]
host           = "10.0.0.5"
user           = "deploy"
ssh-key        = "/secrets/deploy_key"
use-ssh-config = false
```

**Pass-through options** — for everything ssh-config supports but TOML
doesn't have a dedicated field for:

```toml
[config.jump-hosts.bastion-fronted]
host        = "internal.example.com"
ssh-options = [
  "ProxyJump=bastion.example.com",
  "ConnectTimeout=10",
  "ServerAliveInterval=30",
]
```

### Logging

When `[config.logging]` is present, the language pack configures the stdlib
logging system automatically when `parse()` is called. No additional imports
or setup calls are required in the runnable — `logger = logging.getLogger(__name__)`
just works.

| Field | Type | Default | Description |
|---|---|---|---|
| `rotate` | string | `"midnight"` | Rotation policy: `"N MB"`, `"N KB"`, `"N GB"` (size-based), `"daily"`, `"midnight"`, `"weekly"` (time-based). |
| `keep` | int | `7` | Number of rotated backup files to retain. |
| `summary` | bool | `true` | Emit one run-summary record (file) + one stderr line at process exit. See *Run summary* below. |

There is no `level` knob. Console routing is fixed (see below); the file
handler follows the same `--debug` toggle as stdout — INFO by default,
DEBUG when `--debug` is set. Silencing INFO on the console would break
agent responses (stdout is the MCP tool response body), so the threshold is
not configurable. Use the auto-added `--debug` flag to *raise* verbosity
everywhere at once.

Log file path is always: `{installation_root}/logs/{runnable_name}.log`.
Fallback when the root is not writable: `{home}/logs/{runnable_name}.log`.
One logs directory per environment — survives reinstalls and avoids
scattering logs across package directories.

| Language pack | `installation_root` |
|---|---|
| Python (`runspec`) | `sys.prefix` — the venv root |
| Node (`runspec-node`) | nearest ancestor `package.json`, skipping `node_modules` — the project root |

#### Console routing

A single `logger.X` call works in both CLI mode and agent mode — the language
pack routes by record level so the dev never has to branch on context:

| Level | Stream | Format | Notes |
|---|---|---|---|
| `DEBUG` | stdout | `DEBUG file.py:42: message` | Only included when `--debug` is passed — applies to the file handler too |
| `INFO` | stdout | `message` | Plain — reads like `print()` |
| `WARNING` | stderr | `WARNING: message` | Prefixed so it stands out |
| `ERROR` | stderr | `ERROR: message` | Prefixed so it stands out |
| `CRITICAL` | stderr | `CRITICAL: message` | Prefixed so it stands out |

The file handler is unconditional and always JSON. Its level follows the
same `--debug` toggle as stdout — INFO by default, DEBUG when `--debug` /
`RUNSPEC_DEBUG=1` is set. One knob: stdout and the audit file move
together. Defaulting to INFO keeps third-party library DEBUG noise out of
the log file. Stderr is independent and stays pinned at WARNING.

The split matches Unix stream conventions, which makes the same `logger.info()`
call do the right thing in two very different contexts:

* **CLI mode** — stdout shows the human-readable output and remains pipeable;
  stderr carries warnings/errors that wouldn't corrupt a downstream pipe.
* **Agent mode** (`RUNSPEC_AGENT=1`) — `runspec serve` captures stdout as the
  MCP tool response, so `logger.info` calls appear in the response the agent
  sees. Stderr is captured separately and surfaced when the runnable exits
  non-zero.

> **Pipe-emitting runnables.** A runnable that writes machine-readable data on
> stdout for piping (e.g. `my-tool | jq`) must keep stdout clean. Use `print()`
> (or its equivalent) for the data payload, and only call `logger.warning` and
> above — those go to stderr and don't corrupt the pipe. `logger.info` would
> land on stdout and mix with the data.

#### `--debug` flag

When `[config.logging]` is present, a `--debug` flag is auto-added to every
runnable (also settable via `RUNSPEC_DEBUG=1`). Passing it includes DEBUG
records — on stdout *and* in the audit file — and tracebacks on stdout.
One knob; both surfaces move together:

```
my-script --debug
RUNSPEC_DEBUG=1 my-script
```

The flag only *adds* visibility; it never silences anything. The `debug` name
is reserved when `[config.logging]` is present — your runnable cannot define
its own argument by that name.

#### Run summary

When `summary = true` (default), the language pack records one summary at
process exit — without any user code. It does two things:

1. **Audit log:** one JSON record on the `runspec.runsummary` logger,
   written to the audit file (and only the audit file — the console
   handlers filter this logger out).
2. **Human line:** one short line to stderr.

The audit record is fixed-schema:

```json
{"ts": "...", "level": "INFO", "logger": "runspec.runsummary",
 "message": "run completed",
 "extra": {
   "event": "run_summary",
   "runnable": "compress",
   "command_path": [],
   "duration_ms": 1483,
   "exit_code": 0,
   "agent": false,
   "autonomy": "confirm",
   "exception": null,
   "events": {"DEBUG": 0, "INFO": 12, "WARNING": 2, "ERROR": 0, "CRITICAL": 0}
 }}
```

The stderr line reads like a database client's closing message:

```
runspec: compress completed in 1.48s — 12 events (2 warnings, 0 errors)
runspec: compress failed in 0.91s — exit 1, ValueError — 5 events (1 warning, 1 error)
```

A `--no-summary` flag is auto-added alongside `--debug` and suppresses
both the record and the stderr line for the invocation. The
`RUNSPEC_NO_SUMMARY=1` environment variable does the same. The
`no-summary` name is reserved when `[config.logging]` is present.

In agent mode, `runspec serve` returns the same information per call via
the MCP `_meta` extension:

```json
"result": {
  "content": [...],
  "isError": false,
  "_meta": {"runspec": {"tool": "compress", "duration_ms": 1483, "exit_code": 0}}
}
```

`_meta` is the MCP-standard extension point — clients that don't read it
ignore the block. The tool response body is unaffected.

#### Sensitive data filtering

All log output (console and file) is filtered for sensitive data before emission.
Passwords, tokens, API keys, bearer/basic auth headers, URL credentials, and
common JSON/form-encoded credential fields are replaced with `[REDACTED]`.
Filter errors are silent — a failing pattern never suppresses the log record.

#### Example

```toml
[config.logging]
rotate = "midnight"
keep   = 7
```

---

## Runnable Definition

Runnables are defined at the top level of `runspec.toml`. The reserved name
`config` is excluded — everything else is a runnable.

```toml
[<name>]
description     = "Human and agent readable description"  # recommended
autonomy        = "confirm"                               # optional
autonomy-reason = "Why this level was chosen"             # optional
output          = "text"                                  # optional
hosts           = ["host1", "host2"]                      # optional, see Remote Execution
run_as          = "username"                              # optional, see Remote Execution
become_method   = "sudo"                                  # optional, default "sudo"
become_flags    = "-H"                                    # optional
examples        = [...]                                   # optional, see Examples
```

### `examples`

A list of usage examples rendered by `--help`. Canonical form is inline TOML
tables with `cmd` (required) and `description` (optional):

```toml
[mytool]
examples = [
  {cmd = "mytool",                description = "Run with defaults"},
  {cmd = "mytool --verbose",      description = "Show debug output"},
  {cmd = "mytool --input data.csv"},
]
```

Bare strings are accepted as shorthand for `{cmd = <string>}`. Examples are
declarative; the spec layer never executes them.

### `output`

Declares what the runnable writes to stdout. Used by agent frameworks and the
`runspec serve` layer to interpret the tool's response.

| Value | Meaning |
|---|---|
| `text` | Human-readable output (default) |
| `json` | Structured JSON — agent can parse and act on the response |
| `html` | HTML output (reserved for future UI use) |

Default when unspecified: `"text"`.

### Reserved Names

`config` is the only reserved name. A runnable cannot be named `config`.
`runspec local` will report an error if this name is used.

### Autonomy Levels

| Level | Meaning |
|---|---|
| `autonomous` | Agent runs freely, no confirmation needed |
| `confirm` | Agent must present intent and await human approval |
| `supervised` | Agent runs, human reviews output before it is acted on |
| `manual` | Agent cannot invoke — human only |

Default when unspecified: value of `[config] autonomy-default`, else `"confirm"`.

#### Who autonomy is for

Autonomy is **a contract for agent invocation, not a directive for human users.** A human typing a command at the terminal has already chosen the action by typing it — autonomy doesn't ask them to confirm anything extra. The four levels describe how an MCP host (or other agent runtime) should gate the *agent's* call.

Conforming MCP hosts read the `x-autonomy` field on the tool schema (emitted by `runspec local --format mcp` and by `runspec serve`'s `tools/list`) and gate accordingly:

- `autonomous` — invoke without asking
- `confirm` — present the planned call to the human, wait for approval
- `supervised` — invoke, then show the result before the agent acts on it
- `manual` — never invoke

Hosts that don't honour `x-autonomy` are free to ignore it. The spec does not force any particular gating behaviour at the host layer — that's a quality-of-implementation concern of each MCP host.

#### Tool-side enforcement (recommended for destructive actions)

Because host gating is not universal, runnables that perform destructive actions should also enforce autonomy themselves at runtime. Two properties on the parsed `RunSpec` make this straightforward:

- `args.runspec_agent` — `True` when invoked by an MCP server (`runspec serve` sets `RUNSPEC_AGENT=1` automatically)
- `args.runspec_autonomy` — the effective autonomy after escalation

The recommended pattern for a destructive flag:

```python
if args.delete:
    if args.runspec_agent and args.runspec_autonomy != "autonomous":
        raise SystemExit("✗ --delete requires autonomy='autonomous' for agent invocation")
    # ... proceed
```

This refuses agent invocation of a destructive action unless the runnable's declared autonomy explicitly permits unattended execution. Human invocation is unaffected — a human running the command at a terminal has already chosen the action by passing the flag.

---

## Remote Execution

These fields control how `runspec jump` and compatible SSH clients run the tool on a remote host.

### `hosts`

Restricts a runnable to specific machines. `runspec serve` checks this against the
current hostname at startup — tools not matching are excluded from the MCP tool list.
Absent means available everywhere it is installed.

```toml
[parse-app-logs]
hosts = ["logserver-01", "logserver-02"]
```

### `run_as`

The user to run the command as on the remote host. Four forms are supported.

**1. Simple string** — same user on all hosts:
```toml
run_as = "oracle"
```

**2. Environment variable** — resolved by `runspec serve` at startup. Set per host
group via Ansible `group_vars` or equivalent config management:
```toml
run_as = "$ORACLE_RUN_AS"
```

**3. Per-host exact match**:
```toml
run_as.default = "oracle"
run_as.hosts."special-box-01" = "dba"
run_as.hosts."special-box-02" = ""    # no privilege escalation on this host
```

**4. Pattern matching** — Python `re.fullmatch()`, evaluated top to bottom,
first match wins (TOML insertion order preserved):
```toml
run_as.default = "oracle"
run_as.patterns."[lg]pexp[0-9]*" = "orasvc"
run_as.patterns."prod[0-9]*"     = "produser"
```

Forms 3 and 4 may be combined. Resolution order:

1. Exact match in `hosts`
2. First matching pattern in `patterns`
3. `default`
4. No privilege escalation

An empty string (`""`) explicitly means no privilege escalation for that host or pattern.

Invalid regex patterns cause `runspec serve` to exit with a clear error at startup.
`runspec local` also validates all patterns at startup.

### `become_method`

The privilege escalation method. Default: `"sudo"`.

| Value | Command constructed |
|---|---|
| `sudo` | `sudo {flags} -u {user} {command} {args}` |
| `su` | `su {flags} -c "{command} {args}" {user}` |
| `pbrun` | `pbrun {flags} -u {user} {command} {args}` |
| `dzdo` | `dzdo {flags} -u {user} {command} {args}` |

Note: `su` uses a distinct syntax — the command and args are wrapped in a `-c` string.

### `become_flags`

Optional flags passed to the become method. Example: `"-H"` resets HOME to the target
user's home directory under `sudo`.

### Command building

Executing clients build the final remote command as follows. All interpolated values
must be shell-escaped (Python: `shlex.quote()`).

| `run_as` | `env` args | Command constructed |
|---|---|---|
| absent | absent | `{command} {args}` |
| absent | present | `KEY=val … {command} {args}` |
| present | absent | `{become_method} {become_flags} -u {user} {command} {args}` |
| present | present | `{become_method} {become_flags} -u {user} env KEY=val … {command} {args}` |

When both `run_as` and `env` fields are present, `env(1)` is used after privilege
escalation so that variables are set in the target user's process context. This avoids
requiring `sudo -E` or `env_keep` in sudoers, and requires no changes to `sshd_config`
on target hosts.

`su` uses its own syntax and does not follow the `-u` pattern — see the table above.

---

## Argument Definition

Arguments live under `[<name>.args]`.

### Three levels of verbosity

**Bare value** — type and default inferred from the value:
```toml
verbose = false    # flag, default false
workers = 4        # int, default 4
```

**Inline table** — explicit fields on one line:
```toml
input-dir = {type = "path"}
quality   = {default = 85, range = [1, 100]}
format    = {options = ["json", "csv"], default = "json"}
```

**Full block** — for args needing prose descriptions:
```toml
[<name>.args.quality]
default     = 85
range       = [1, 100]
description = "Controls output quality. Values below 60 rarely useful."
```

### Argument Fields

| Field | Type | Description |
|---|---|---|
| `type` | string | See Types section. Inferred if omitted. |
| `default` | any | Default value. Absence implies `required = true`. |
| `required` | bool | Explicit required flag. Inferred from missing default. |
| `description` | string | Human and agent readable. Doubles as form label. |
| `options` | array | Valid choices. Infers `type = "choice"`. |
| `range` | [min, max] | Valid range for numeric types. |
| `multiple` | bool | Accept multiple values (repeated flag style). |
| `delimiter` | string | Split a single value by this delimiter. |
| `short` | string | Short flag alias e.g. `"-v"`. |
| `env` | string | Environment variable fallback. |
| `deprecated` | string | Deprecation message shown on use. |
| `autonomy` | string | Per-arg autonomy override. Escalates if more restrictive. |
| `ui` | string | Form control hint. Inferred from type if omitted. |
| `meta` | table | Developer-defined metadata. Pass-through — runspec never interprets the contents. |
| `position` | int | 1-based positional index. Makes the arg positional rather than a `--flag`. |

### Default Values for Argument Fields

What you actually get when a field is omitted. Two layers of defaulting are
applied: the **loader** normalises the raw TOML into a uniform dict, then
**inference** fills in derived fields (`type`, `required`) based on the
fields that *are* present.

| Field | If omitted, you get | Notes |
|---|---|---|
| `type` | inferred from `default`'s Python type → bool→`flag`, int→`int`, float→`float`, str→`str`; or from `options` → `choice`; final fallback `str` | Inference rules table earlier in this document. |
| `default` | `None` (except `rest` type, which defaults to `[]`) | |
| `required` | `True` if no `default` and `type` is not `flag` or `rest`; `True` if `type = "path"` and no `default`; `False` otherwise | This is what makes `name = {type = "str"}` a required arg with no default. To make it optional, set `required = false` or give it a `default`. |
| `description` | `None` | Recommended in practice — agents lean on this. |
| `options` | `None` | Setting this also infers `type = "choice"`. |
| `range` | `None` | |
| `multiple` | `False` | |
| `delimiter` | `None` | When set, a single value is `.split(delimiter)`. |
| `short` | `None` | |
| `env` | `None` | When set, the named env var is checked before the spec default. |
| `deprecated` | `None` | Set to a string message; shown on use. |
| `autonomy` | inherits `[config] autonomy-default` (which itself defaults to `"confirm"`) | Per-arg overrides escalate the runnable's effective autonomy if more restrictive. |
| `ui` | derived from `type` at render time (text/number/checkbox/dropdown/etc.) | See "Form Control Inference". |
| `meta` | `None` | Pass-through — runspec never reads it. |
| `position` | `None` (i.e. the arg is a `--flag`, not a positional) | 1-based when set. |

The two non-obvious defaults to remember:

1. **No `default` → `required = true`** for everything except `flag` and `rest` types.
2. **`type = "path"` is always required unless given a `default`** — paths almost never have a sensible fallback.

These defaults are not configurable at the `[config]` level. The single project-wide arg-related setting is `[config] autonomy-default`, which only affects the autonomy inheritance rule above. Keeping the other defaults stable across projects is intentional: it makes any `runspec.toml` predictable to read.

### Uniqueness and Reserved Values

The following constraints are enforced at parse time and cause a clean error
(not silent shadowing) when violated:

- **`short` must be unique within a runnable.** Two args declaring the same
  short flag (e.g. `short = "-v"`) is an error.
- **`-h` is reserved for `--help`.** An arg may not declare `short = "-h"`.
- **`position` must be unique within a runnable.** Two args declaring the
  same `position = N` is an error.
- **At most one `rest` arg per runnable.** Two args with `type = "rest"`
  is an error.

### Positional Arguments

Args with `position = N` are populated from non-flag tokens in declaration
order rather than via `--name`. Positions are 1-based and must be unique
within a runnable.

```toml
[deploy.args]
target  = {type = "str", description = "Target host", position = 1}
release = {type = "str", description = "Release tag", position = 2, required = false}
```

```bash
deploy prod v1.2.3       # target=prod, release=v1.2.3
deploy prod              # target=prod, release=None
```

### Pass-through Arguments (`type = "rest"`)

A single arg with `type = "rest"` captures everything after a literal `--`
token as a list of strings. Used to forward args to a wrapped command.

```toml
[wrap.args]
extra = {type = "rest", description = "Args passed to the wrapped command"}
```

```bash
wrap -- --foo bar --baz       # extra = ["--foo", "bar", "--baz"]
wrap                           # extra = []
```

`rest`-type args are never required and default to `[]`.

---

## Inference Rules

Implementations must apply these rules in order:

| Condition | Inference |
|---|---|
| `default = <integer>` | `type = "int"` |
| `default = <float>` | `type = "float"` |
| `default = <string>` | `type = "str"` |
| `default = true` or `default = false` | `type = "flag"` |
| `options = [...]` present | `type = "choice"` |
| No `default` and no explicit `required = false` | `required = true` |
| `type = "path"` with no default | `required = true` |

---

## Types

| runspec type | Description |
|---|---|
| `str` | Unicode string |
| `int` | Integer |
| `float` | Floating point number |
| `bool` | Boolean (true/false) |
| `flag` | Boolean switch — presence means true |
| `path` | File system path |
| `choice` | One of a declared set of options |
| `rest` | List of strings captured after a literal `--` separator. At most one per runnable. |

Language packs may register additional types via the type registry.

---

## Value Resolution Order

For every argument, implementations must resolve the value in this order:

1. Explicit CLI argument — highest priority
2. `RUNSPEC_ARG_<ARGNAME>` environment variable (automatic for every arg)
3. `env` aliases (developer-declared list, checked in order)
4. Default from spec
5. Error: required — if nothing above matched and `required = true`

---

## Runtime Environment Variables

When `runspec serve` executes a tool locally, or when `runspec jump` runs a tool
on a jump box, all resolved argument values are injected as environment variables
before the process starts. This makes any runnable language — bash, Python, Node, or anything
else — work without a language-specific library.

### Naming convention

Per-arg variables use the `RUNSPEC_ARG_` prefix:

```
RUNSPEC_ARG_<ARG_NAME_UPPERCASED>
```

Hyphens and underscores in the arg name both become underscores. Examples:

| Arg name | Environment variable |
|---|---|
| `env` | `RUNSPEC_ARG_ENV` |
| `dry-run` | `RUNSPEC_ARG_DRY_RUN` |
| `input_file` | `RUNSPEC_ARG_INPUT_FILE` |
| `max-retries` | `RUNSPEC_ARG_MAX_RETRIES` |

These variables serve two purposes:
- **Input (user-settable):** Set `RUNSPEC_ARG_QUALITY=95` in your shell to make that your persistent default whenever no CLI arg is passed.
- **Output (runtime-injected):** `runspec serve` and `runspec jump` inject resolved values into the subprocess environment so bash/node/any-language scripts can read them directly without a library.

The `env` field on an arg declares additional aliases checked after `RUNSPEC_ARG_*`. Accepts a string or list of strings:

```toml
[compress.args.quality]
env = ["CI_QUALITY", "ANSIBLE_QUALITY"]  # also checked if RUNSPEC_ARG_QUALITY unset
```

### Serialisation

| Arg type | Value | Env var |
|---|---|---|
| `flag` | not passed (default false) | `0` |
| `flag` | passed | `1` |
| `bool` | `false` | `0` |
| `bool` | `true` | `1` |
| `str`, `int`, `float`, `path`, `choice` | any value | natural string form |
| `multiple = true` | list of values | newline-delimited string |

The same formats are accepted on the input side — a user setting `RUNSPEC_ARG_DRY_RUN=1` or `RUNSPEC_ARG_DRY_RUN=true` both coerce correctly to a flag.

Defaults from the spec are always set, even when the caller did not pass the arg
explicitly. A required arg that was not provided is not set (this is a validation
error — the tool should never be reached).

### Reserved variables

| Variable | Set when | Purpose |
|---|---|---|
| `RUNSPEC_AGENT=1` | always (every `runspec serve` invocation) | The runnable can branch on agent vs. human invocation. |
| `RUNSPEC_CONFIG=/abs/path/to/runspec.toml` | `runspec serve` subprocesses the runnable | Points `parse()` at the spec file directly, bypassing the lookup cascade. Without this, the subprocess inherits SSH's `$HOME` cwd and the cwd-walk fallback would fail. |

`parse()` consults `RUNSPEC_CONFIG` after an explicit `config_path=` arg, then
walks up from the caller's package directory, then from cwd. So the
resolution order in language packs is:

1. Explicit `config_path` passed to `parse()`
2. `RUNSPEC_CONFIG` environment variable
3. Walk up from the caller's `__file__` directory (the package directory
   where `runspec.toml` was shipped — found via call-stack introspection).
   This makes installed entry points work from any working directory.
4. Walk up from cwd looking for `runspec.toml` (ad-hoc usage and back-compat)
5. Error

### Example

```toml
[backup-logs]
description = "Back up application logs"
autonomy    = "confirm"

[backup-logs.args]
env     = {type = "choice", options = ["prod", "staging"]}
days    = {type = "int", default = 7}
dry-run = {type = "flag", default = false}
```

```bash
#!/bin/bash
# env vars are already set and validated — no parsing needed
if [ "$RUNSPEC_ARG_DRY_RUN" = "1" ]; then
    echo "Would back up $RUNSPEC_ARG_DAYS days of $RUNSPEC_ARG_ENV logs"
    exit 0
fi
aws s3 sync "/var/log/app/$RUNSPEC_ARG_ENV" "s3://logs-$RUNSPEC_ARG_ENV" \
    --delete --include "*.log"
```

---

## Groups

Groups define relationships between arguments.
They live under `[<name>.groups.<group-name>]`.

### Group Types

**Mutually exclusive** — at most one arg from the group may be provided:
```toml
[<name>.groups.output]
exclusive = true
args      = ["format", "raw"]
```

**Mutually inclusive** — if any arg is provided, all must be:
```toml
[<name>.groups.auth]
inclusive = true
args      = ["username", "password"]
```

**At least one** — one or more must be provided:
```toml
[<name>.groups.input]
at-least-one = true
args         = ["input-file", "input-dir", "input-glob"]
```

**Exactly one** — strictly one must be provided:
```toml
[<name>.groups.mode]
exactly-one = true
args        = ["fast", "balanced", "quality"]
```

**Conditional** — if one arg is provided, others become required:
```toml
[<name>.groups.upload]
if       = "upload"
requires = ["bucket", "region"]
```

### Escalation Rule

The most restrictive autonomy level wins across all provided args.
An agent can never escalate beyond what the spec allows.

---

## Subcommands

Subcommands live under `[<name>.commands.<subcommand>]`.
Each subcommand has its own `args`, `groups`, `autonomy`, and `description`.

```toml
[pipeline.commands.run]
description = "Run the pipeline"
autonomy    = "confirm"

[pipeline.commands.run.args]
input = {type = "path"}

[pipeline.commands.validate]
description = "Validate without running"
autonomy    = "autonomous"
```

---

## Form Control Inference

When `ui` is not declared, implementations should infer the form control:

| Type / Property | Default control |
|---|---|
| `str` | Text input |
| `int` or `float` | Number input |
| `path` | File/directory picker |
| `bool` or `flag` | Checkbox |
| `options` ≤ 4 items | Radio group |
| `options` > 4 items | Dropdown |
| `range` present | Slider |
| `multiple = true` | Multi-select |

---

## Emitted Schema Fields

When emitting to agent formats, implementations must include:

- `name` — runnable name
- `description` — runnable description
- `x-autonomy` — autonomy level
- `x-autonomy-reason` — reason if provided
- `x-output` — output type (`"text"` if not declared)
- `inputSchema` — JSON Schema object of all arguments

---

## Compliance

All implementations are tested against the fixtures in
`tests/integration/fixtures/`. A conforming implementation must produce
output that matches the canonical JSON in `tests/integration/compliance/`
for every fixture.
