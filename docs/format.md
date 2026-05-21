# Format Reference

The runspec format is TOML. It is identical across Python and Node — only the
install step differs. This page is the working reference; the canonical spec
lives at [`spec/SPEC.md`](https://github.com/JasonFinestone/runspec/blob/main/spec/SPEC.md).

---

## File location

`runspec.toml` lives **inside your package directory**, alongside the code it
describes:

```
mypkg/
  __init__.py
  runspec.toml    ← here, not at the project root
```

This location means build backends include it automatically as package data,
and `importlib.metadata` / `node_modules/.bin` discovery can locate it after
install with no extra configuration.

**File lookup:** `parse()` walks up from `cwd` to find the first `runspec.toml`.
For server contexts, `RUNSPEC_CONFIG=/abs/path/to/runspec.toml` overrides the
walk — `runspec serve` sets it automatically when spawning subprocesses.

---

## Top-level structure

```toml
[config]     # optional project-wide defaults
[<name>]     # one section per runnable — anything that's not `config`
```

`config` is the only reserved name. Every other top-level section defines a
runnable. The section name is what users type on the command line.

---

## The `[config]` section

Project-wide defaults. All fields optional.

| Field | Type | Default | Description |
|---|---|---|---|
| `autonomy-default` | string | `"confirm"` | Autonomy when unspecified on a runnable |
| `lang` | string | — | Preferred language for `runspec init` code stubs |
| `name` | string | venv dir name | MCP server name reported by `runspec serve` |
| `version` | string | `"1"` | runspec spec version |
| `jump-hosts` | table | — | Per-alias jump host config. See [Jump Hosts](jump-hosts.md). |
| `logging` | table | — | Logging configuration. See [Logging](logging.md). |

```toml
[config]
autonomy-default = "confirm"
lang             = "python"
version          = "1"
```

`[config.jump-hosts]` and `[config.logging]` get their own pages — they're
substantial enough that documenting them inline would bury the rest of the
reference.

---

## Runnable definition

Every top-level section except `[config]` is a runnable.

```toml
[deploy]
description     = "Deploy to production"       # recommended
autonomy        = "manual"                     # optional
autonomy-reason = "Irreversible operation"     # optional
output          = "text"                       # optional
hosts           = ["web-01", "web-02"]         # optional, see Remote Execution
run_as          = "deploy"                     # optional, see Remote Execution
become_method   = "sudo"                       # optional, default "sudo"
become_flags    = "-H"                         # optional
examples        = [...]                        # optional, see below
```

### `description`

Shown in `--help` and in every emitted agent schema. Strongly recommended —
agents lean on this to decide whether a tool fits the task.

### `autonomy` levels

Declares how much trust an agent runtime should have when invoking this
runnable. **It's a contract for agent invocation, not a directive for human
users** — a human typing the command has already chosen the action.

| Level | Meaning |
|---|---|
| `autonomous` | Run freely — no confirmation needed |
| `confirm` | Present the planned call, wait for human approval |
| `supervised` | Run, then show the result before acting on it |
| `manual` | Never invoke — human only |

Falls back to `[config] autonomy-default`, then `"confirm"`.

### `output`

Declares what the runnable writes to stdout. Surfaces as `x-output` in every
emitted schema so agents know whether to display or parse the response.

| Value | Meaning |
|---|---|
| `text` | Human-readable output (default) |
| `json` | Structured JSON — agent can parse and act on it |
| `html` | HTML output (reserved for future UI use) |

### `examples`

A list rendered by `--help`. Inline TOML tables with `cmd` (required) and
optional `description`:

```toml
[mytool]
examples = [
  {cmd = "mytool",                description = "Run with defaults"},
  {cmd = "mytool --verbose",      description = "Show debug output"},
  {cmd = "mytool --input data.csv"},
]
```

Bare strings are accepted as shorthand: `examples = ["mytool", "mytool --verbose"]`.
Examples are declarative — the spec layer never executes them.

---

## Argument definition

Arguments live under `[<name>.args]`.

### Three levels of verbosity

**Bare value** — type and default inferred:

```toml
[greet.args]
verbose = false    # flag, default false
workers = 4        # int, default 4
label   = "main"   # str, default "main"
```

**Inline table** — explicit fields on one line:

```toml
[greet.args]
input-dir = {type = "path"}
quality   = {default = 85, range = [1, 100]}
format    = {options = ["json", "csv"], default = "json"}
```

**Full block** — for args that need prose descriptions or many fields:

```toml
[greet.args.quality]
default     = 85
range       = [1, 100]
description = "Output quality. Values below 60 are rarely useful."
```

### Argument fields

| Field | Type | Description |
|---|---|---|
| `type` | string | Argument type. Inferred from `default` or `options` if omitted. |
| `default` | any | Default value. Absence implies `required = true`. |
| `required` | bool | Override the inferred required flag. |
| `description` | string | Shown in `--help` and emitted schemas. |
| `options` | array | Valid choices. Infers `type = "choice"`. |
| `range` | [min, max] | Valid range for numeric types. |
| `multiple` | bool | Accept multiple values (repeated-flag style). |
| `delimiter` | string | Split a single value by this character. |
| `short` | string | Short flag alias, e.g. `"-v"`. Must be unique within a runnable; `-h` is reserved. |
| `env` | string | Environment variable fallback. |
| `deprecated` | string | Deprecation message shown on use. |
| `autonomy` | string | Per-arg autonomy override. Most restrictive level wins. |
| `ui` | string | Form control hint. Inferred from type if omitted. |
| `meta` | table | Developer-defined pass-through metadata. runspec never reads it. |
| `position` | int | 1-based positional index. Makes the arg positional rather than a `--flag`. |

### Positional arguments

Set `position = N` to make an arg positional. Positions must be unique within
a runnable:

```toml
[deploy.args]
target  = {type = "str", description = "Target host", position = 1}
release = {type = "str", description = "Release tag", position = 2, required = false}
```

```bash
deploy prod v1.2.3       # target=prod, release=v1.2.3
deploy prod              # target=prod, release=None
```

### Pass-through arguments (`type = "rest"`)

One arg per runnable can have `type = "rest"`. It captures everything after a
literal `--` token as a list of strings — useful when wrapping another command:

```toml
[wrap.args]
extra = {type = "rest", description = "Args passed to the wrapped command"}
```

```bash
wrap -- --foo bar --baz       # extra = ["--foo", "bar", "--baz"]
wrap                          # extra = []
```

`rest` args are never required and default to `[]`. This is exactly how
`runspec jump <alias> <tool> -- <tool-args…>` works.

---

## Inference rules

When fields are omitted, runspec infers them. Rules apply in this order:

| Condition | Inference |
|---|---|
| `options = [...]` present | `type = "choice"` |
| `default = true` or `false` | `type = "flag"` |
| `default = <integer>` | `type = "int"` |
| `default = <float>` | `type = "float"` |
| `default = <string>` | `type = "str"` |
| No `default`, no `required = false` | `required = true` |
| `type = "path"` with no default | `required = true` |

!!! note
    `options` is checked before `default` — if both are present, `type = "choice"` wins.
    Bool is checked before int — `false` and `true` are flags, not integers.

---

## Types

| Type | Description |
|---|---|
| `str` | Unicode string |
| `int` | Integer |
| `float` | Floating point number |
| `bool` | Boolean (`true` / `false`) |
| `flag` | Boolean switch — presence means `true` |
| `path` | File system path |
| `choice` | One of a declared set of `options` |
| `rest` | List of strings captured after a literal `--` separator. At most one per runnable. |

Language packs may register additional types. See
[`register_type()`](python.md#register_type) /
[`registerType()`](node.md#registertype).

---

## Value resolution order

For every argument, values are resolved in this order:

1. Explicit CLI argument — highest priority
2. Environment variable (if `env` declared)
3. Config file value *(future)*
4. Default from spec
5. Error: required — if nothing matched and `required = true`

---

## Environment variable fallbacks

The `env` field lets an argument read from an environment variable when
nothing is passed on the command line:

```toml
[deploy.args]
server  = {type = "str",  env = "DEPLOY_SERVER"}
api-key = {type = "str",  env = "DEPLOY_API_KEY",  autonomy = "manual"}
region  = {type = "str",  env = "AWS_REGION",       default = "us-east-1"}
dry-run = {default = false}
```

### CI/CD pattern

Set variables once at the project level — your pipeline file stays identical
across every project, and each project controls its own behaviour through
environment variables:

=== "GitLab CI"

    ```yaml
    # .gitlab-ci.yml — shared across all projects
    deploy:
      script: deploy
    ```

    In **Project → Settings → CI/CD → Variables**, set:

    ```
    DEPLOY_SERVER  = web-01
    DEPLOY_API_KEY = <secret>
    AWS_REGION     = eu-west-1
    ```

=== "GitHub Actions"

    ```yaml
    - name: Deploy
      run: deploy
      env:
        DEPLOY_SERVER: web-01
        DEPLOY_API_KEY: ${{ secrets.DEPLOY_API_KEY }}
        AWS_REGION: eu-west-1
    ```

=== "Ansible"

    ```yaml
    - name: Deploy application
      command: deploy
      environment:
        DEPLOY_SERVER: "{{ deploy_server }}"
        DEPLOY_API_KEY: "{{ vault_deploy_api_key }}"
        AWS_REGION: "{{ aws_region }}"
    ```

!!! tip
    Combine `env` with `autonomy = "manual"` for secrets. The arg is still
    readable from the environment, but agents are blocked from passing it —
    the value has to come from the operator.

### Runtime env injection

When a runnable is invoked via `runspec serve` or `runspec jump`, every
resolved argument is also exported as `RUNSPEC_<ARG_NAME_UPPERCASED>` before
the process starts. Bash, Python, Node, or anything else can read those
without a language-specific library. Hyphens become underscores;
`flag`/`bool` values become `0`/`1`; `multiple = true` lists are
newline-delimited. `RUNSPEC_AGENT=1` is set on every `serve` invocation.

---

## Groups

Groups define relationships between arguments and are validated after
individual args pass.

```toml
[<name>.groups.<group-name>]
exclusive = true
args      = ["format", "raw"]
```

### Group types

**Exclusive** — at most one arg from the group may be provided:

```toml
[pipeline.groups.output-format]
exclusive = true
args      = ["format", "raw"]
```

**Inclusive** — if any arg is provided, all must be:

```toml
[pipeline.groups.auth]
inclusive = true
args      = ["api-key", "api-endpoint"]
```

**At least one** — one or more from the group must be provided:

```toml
[pipeline.groups.input]
at-least-one = true
args         = ["input-file", "input-dir", "input-glob"]
```

**Exactly one** — strictly one must be provided:

```toml
[pipeline.groups.mode]
exactly-one = true
args        = ["fast", "balanced", "quality"]
```

**Conditional** — if a trigger arg is provided, other args become required:

```toml
[pipeline.groups.upload]
if       = "upload"
requires = ["bucket", "region"]
```

---

## Subcommands

Subcommands live under `[<name>.commands.<subcommand>]`. Each has its own
`args`, `groups`, `autonomy`, `description`, and `examples`:

```toml
[pipeline]
description = "Process and validate data pipeline files"
autonomy    = "confirm"

[pipeline.commands.run]
description     = "Run the pipeline"
autonomy        = "confirm"
autonomy-reason = "Writes output files and may call external APIs"

[pipeline.commands.run.args]
input   = {type = "path"}
dry-run = {default = false}

[pipeline.commands.validate]
description = "Validate without running"
autonomy    = "autonomous"

[pipeline.commands.validate.args]
input  = {type = "path"}
strict = {default = false}
```

`runspec serve` flattens nested subcommands into MCP tools with
underscore-joined names (e.g. `portal-api_orders_get-list`).

---

## Logging configuration

When `[config.logging]` is present, `parse()` configures stdlib logging
automatically and auto-injects a `--debug` flag. The full reference is on
the dedicated [Logging](logging.md) page; the short form:

```toml
[config.logging]
rotate = "midnight"    # daily | midnight | weekly | "10 MB" | "1 GB" | …
keep   = 7
```

File logs go to `{package_dir}/logs/{runnable}.log` as JSON at DEBUG, with
midnight rotation and 7-day retention. Sensitive data is redacted on every
log line. See [Logging](logging.md) for the full picture.

---

## Remote execution

These fields control how `runspec jump` runs a tool on a remote host. The
full reference — including all four `run_as` shapes — is on the
[Jump Hosts](jump-hosts.md) page.

### `hosts`

Restricts a runnable to specific machines. `runspec serve` checks the current
hostname at startup; tools that don't match are excluded from the MCP tool
list. Absent means available everywhere.

```toml
[parse-app-logs]
description = "Parse and summarise application logs"
autonomy    = "confirm"
hosts       = ["logserver-01", "logserver-02"]
```

### `run_as`, `become_method`, `become_flags`

Privilege escalation for remote execution. Four `run_as` shapes are supported
(plain string, env var, per-host map, regex patterns):

```toml
[deploy]
run_as        = "oracle"
become_method = "sudo"      # default — also: su, pbrun, dzdo
become_flags  = "-H"
```

```toml
# Per-host map
run_as.default = "oracle"
run_as.hosts."special-box-01" = "dba"
run_as.hosts."special-box-02" = ""        # no escalation on this host

# Regex patterns (top to bottom, first match wins)
run_as.default = "oracle"
run_as.patterns."[lg]pexp[0-9]*" = "orasvc"
run_as.patterns."prod[0-9]*"     = "produser"
```

See [Jump Hosts](jump-hosts.md) for the full resolution table and the
remote-command construction rules.

---

## Developer metadata

The `meta` field attaches arbitrary structured data to any argument. runspec
passes it through untouched — never validated, never interpreted.

A common use case is associating choice values with lookup data needed at
runtime:

```toml
[deploy.args.server]
options = ["web-01", "web-02", "db-01"]

[deploy.args.server.meta]
web-01 = {datacenter = "us-east", tier = "web"}
web-02 = {datacenter = "us-west", tier = "web"}
db-01  = {datacenter = "eu-central", tier = "db"}
```

```python
args = parse()
info = args.server.meta[args.server.value]
print(info["datacenter"])   # "us-east"
```

Lookup data lives in the same place as the argument definition — no separate
config files, no hardcoded mappings.

---

## Complete example

A realistic runnable exercising most features. This is the
`tests/integration/fixtures/complex.toml` shared compliance fixture — every
language pack must produce identical schemas from it.

```toml
[config]
autonomy-default = "confirm"
lang             = "python"
version          = "1"

[pipeline]
description = "Process and validate data pipeline files"
autonomy    = "confirm"

[pipeline.commands.run]
description     = "Run the pipeline against one or more input files"
autonomy        = "confirm"
autonomy-reason = "Writes output files and may call external APIs"

[pipeline.commands.run.args]
input      = {type = "path"}
tag        = {type = "str", multiple = true}
fields     = {type = "str", multiple = true, delimiter = ","}
format     = {options = ["json", "csv", "parquet"], default = "json"}
workers    = {default = 4, range = [1, 32]}
batch-size = {default = 1000, range = [1, 100000]}
dry-run    = {default = false}
verbose    = {default = false, short = "-v"}
strict     = {default = false}
api-key    = {type = "str", env = "PIPELINE_API_KEY", autonomy = "manual"}
timeout    = {default = 30, range = [1, 300]}
threads    = {default = 4, deprecated = "use --workers instead"}

[pipeline.commands.run.groups.input-format]
exclusive = true
args      = ["format", "raw"]

[pipeline.commands.run.groups.api-auth]
inclusive = true
args      = ["api-key", "api-endpoint"]

[pipeline.commands.validate]
description = "Validate pipeline config and input files without running"
autonomy    = "autonomous"

[pipeline.commands.validate.args]
input  = {type = "path"}
schema = {type = "path"}
strict = {default = false}
format = {options = ["json", "csv", "parquet"], default = "json"}
```

What it exercises, top to bottom:

- A `[config]` block with a project-wide `autonomy-default`.
- Two subcommands (`run`, `validate`) with separate args and groups.
- A path-typed required arg (`input`), inferred required by the
  `type = "path"` rule.
- `multiple = true` (the `--tag` flag may repeat).
- `multiple = true` *and* `delimiter` (the `--fields` flag splits one value).
- A `choice` arg (`format`) — inferred from `options`.
- `range` on numeric args (`workers`, `batch-size`, `timeout`).
- A `flag` with a short alias (`verbose` / `-v`).
- An `env` fallback on a secret arg (`api-key`) combined with
  `autonomy = "manual"` to block agent invocation.
- A `deprecated` arg (`threads`) — usable but emits a warning.
- An **exclusive** group (`input-format`) and an **inclusive** group (`api-auth`).
- A subcommand-level autonomy override (`validate` is `autonomous` even
  though the parent is `confirm`).

`runspec local --format mcp` against this spec emits two tools
(`pipeline_run` and `pipeline_validate`) with full input schemas, autonomy
levels, and the inclusive/exclusive constraints described inline.
