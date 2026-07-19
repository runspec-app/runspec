# Format reference

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
discoverable    = true                         # optional, default true; false hides from discovery
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

### `discoverable`

Declares **whether** the runnable is listed by the discovery surfaces. A plain
boolean. Where `serve` controls *the context* (`local` vs `remote`),
`discoverable` controls whether it is listed at all — they compose.

| Value | Meaning |
|---|---|
| `true` (default) | Listed by `runspec serve` (MCP tools) and `runspec local`. |
| `false` | Hidden from all discovery surfaces — an internal helper. Still parseable and still runnable when invoked directly. |

`runspec serve` skips a `discoverable = false` runnable (the way to keep an
internal helper off the agent surface even though its package is installed);
`runspec local` still lists everything, annotating a hidden one as `[hidden]`, and
emits `x-discoverable: false` for it in the agent schemas (omitted otherwise).
Metadata only — it adds no runtime behaviour to the runnable.

Marking a runnable for a **self-service** audience (e.g. a help-desk visitor
catalog) is *not* a `runspec.toml` concern — the catalog's consumer (e.g.
runspec-console) designates the self-service subset in its own git-managed config.

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
| `pattern` | string | Regex the value must fully match. `str` type only. |
| `min-length` | int | Minimum string length. `str` type only. |
| `max-length` | int | Maximum string length. `str` type only. |
| `multiple` | bool | Accept multiple values (repeated-flag style). |
| `delimiter` | string | Split a single value by this character. |
| `short` | string | Short flag alias, e.g. `"-v"`. Must be unique within a runnable; `-h` is reserved. |
| `env` | string \| array | Environment variable fallback(s), checked after the CLI and before the default. |
| `deprecated` | string | Deprecation message shown on use. |
| `autonomy` | string | Per-arg autonomy override. Most restrictive level wins. |
| `ui` | string | Form control hint. Inferred from type if omitted. On a `str` arg: `"date"`/`"datetime"`/`"time"`/`"month"` request a date / date-time / time / month picker (value an ISO-8601 string) and `"color"` a colour picker (`#rrggbb`). On an `int`/`float` arg with a `range`, `"slider"` renders a slider. |
| `hint` | string | Placeholder text for a UI form field. Advisory for `str`/`path`/`password`; never affects parsing. |
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

### String validation (`pattern`, `min-length`, `max-length`)

`str` args can declare format constraints. `pattern` is a regex the value must
**fully** match (`re.fullmatch` semantics — anchored at both ends, so you don't
need `^`/`$`); `min-length`/`max-length` bound the character count:

```toml
[ticket.args]
jira-key = {type = "str", pattern = "[A-Z]+-[0-9]+", description = "e.g. PROJ-123"}
slug     = {type = "str", min-length = 3, max-length = 40}
```

```bash
ticket --jira-key PROJ-123     # ok
ticket --jira-key proj-123     # ✗  Invalid value for --jira-key: 'proj-123'
                               #    Expected: a value matching pattern '[A-Z]+-[0-9]+'
```

These apply to the `str` type only and are ignored for other types. When you
emit a schema (`runspec local --format mcp`), they map to the native JSON
Schema keywords `pattern`, `minLength`, and `maxLength`, so MCP hosts and
survey forms enforce them too.

!!! note "Availability"
    String validation was added in **runspec 0.22.0** and **node-0.18.0**. The
    Node pack anchors `pattern` as `^(?:…)$` to match Python's `re.fullmatch`
    exactly, including patterns with a top-level alternation.

### Secret arguments (`password`)

The `password` type marks an argument whose value is a secret — a password,
token, or API key. It coerces and validates exactly like `str` (so `pattern` /
`min-length` / `max-length` still apply), but it is handled differently
everywhere a value could leak:

```toml
[deploy.args]
db-password = {type = "password", description = "Database password"}
api-token   = {type = "password", env = "DEPLOY_API_TOKEN", required = false}
```

- **Refused on the command line.** `deploy --db-password hunter2` is a hard
  error — a secret on the CLI lands in shell history and the process table
  (`ps`). The value must come from the environment instead.
- **Resolved from the environment.** The automatic
  `RUNSPEC_<RUNNABLE>_ARG_<NAME>` variable, a declared `env` alias, or
  `.runspec_env` — the normal resolution chain minus the CLI tier.
- **Prompted interactively.** When a required `password` is still missing and
  the command is run in a terminal, runspec prompts for it without echoing
  input. Non-interactive and agent runs don't prompt.
- **Omitted from agent schemas.** `runspec local --format mcp` (and the other
  agent formats) leave `password` args out of `inputSchema`, so an agent is
  never asked to supply a secret.
- **Kept out of logs.** The value is redacted from the run-summary audit record.

```bash
deploy --db-password hunter2              # ✗  refused
RUNSPEC_DEPLOY_ARG_DB_PASSWORD=… deploy   # ok — from the environment
deploy                                    # prompts "db-password:" (no echo)
```

In **runspec-console**, a `password` arg renders as a masked form field; the
value you type is sent to the runnable as the `RUNSPEC_<RUNNABLE>_ARG_<NAME>`
environment variable (local subprocess env, or the SSH command's env prefix for
remote hosts) — never as a command-line argument. No persistent secret store is
involved; the value is per-invocation and transient.

!!! note "Availability"
    The `password` type was added in **runspec 0.30.0** and **node-0.27.0**.

### Form hints (`hint`)

The `hint` field supplies placeholder text for a UI form field — an example
value or format reminder shown in the empty control. It is advisory: runspec
never reads it during parsing or validation, and it has no effect on the command
line. It pairs naturally with `str`, `path`, and `password` args:

```toml
[deploy.args]
host        = {type = "str",      hint = "web-01.prod.example.com"}
config      = {type = "path",     hint = "./deploy.yaml"}
db-password = {type = "password", hint = "from the vault"}
```

runspec-console renders `hint` as the input's placeholder.

!!! note "Availability"
    The `hint` field was added in **runspec 0.30.0** and **node-0.27.0**.

### Multiple values (`multiple`, `delimiter`)

`multiple = true` collects repeated flags (`--tag a --tag b`) into a list;
add `delimiter` to also split a single value (`--fields a,b,c`). The arg's
**type is applied to each item**, and the parsed value is a **list of coerced
items** — `multiple` with `type = "int"` yields `[1, 2, 3]`, not strings.

Per-item coercion means every per-item check runs **on each element**:
`pattern` / `min-length` / `max-length` for `str`, `range` for numbers,
`options` for `choice`. When items fail, the error reports *which* ones by
position and value, validating the whole list before reporting:

```toml
[deploy.args]
ticket = {type = "str", multiple = true, pattern = "[A-Z]+-[0-9]+"}
```

```bash
deploy --ticket PROJ-1 --ticket bad-2 --ticket nope
# ✗  --ticket: 2 of 3 item(s) failed validation:
#
#    • item 2 ('bad-2'):
#      ✗  Invalid value for --ticket: 'bad-2'
#         Expected: a value matching pattern '[A-Z]+-[0-9]+'
#         Got: 'bad-2'
#    • item 3 ('nope'):
#      ...
```

!!! note "Availability"
    Per-item coercion and validation for `multiple` args landed in
    **runspec 0.24.0** and **node-0.19.0**. (Earlier versions stringified the
    list instead of coercing each element.)

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

The canonical definition of these rules lives in
[`spec/SPEC.md`](https://github.com/JasonFinestone/runspec/blob/main/spec/SPEC.md);
every language pack is tested against it, and the table above mirrors it.

---

## Types

| Type | Description |
|---|---|
| `str` | Unicode string |
| `password` | Secret string — masked in UIs, refused on the command line, omitted from agent schemas, redacted from logs. See [Secret arguments](#secret-arguments-password). |
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

### Group members must be optional

A group constraint governs *which combination* of its members is allowed, so each
member must be an **optional** arg — `required = false`, with **no `default` and
no `env`**. Two reasons:

- runspec infers `required = true` for any arg with no default, so a bare member
  would be individually required and fight the group.
- group validation runs **after** defaults and env fallbacks are applied, and a
  member counts as "provided" whenever its value is not unset — so a member with
  a `default` (or a set `env` var) always counts, which breaks `exclusive` /
  `exactly-one`.

`flag` args are ideal members: they default to `false` and only count as provided
when actually passed.

```toml
[fetch.args]
url  = {type = "str",  required = false}
file = {type = "path", required = false}

[fetch.groups.source]
exactly-one = true
args        = ["url", "file"]
```

### Help and testing

`--help` lists every group under a **Constraints:** section (e.g.
`exactly one of: --url, --file`), so the relationship is visible without
triggering a validation error. The [`ParseHarness.smoke()`](testing.md) helper is
group-aware: it includes at most one member of an `exclusive` / `exactly-one`
group when building a valid invocation, so a fully-populated `values` map doesn't
trip the constraint.

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

### Requiring a subcommand (`require-command`)

By default a runnable with subcommands can still run "bare" (no subcommand).
Set `require-command = true` on the parent to make choosing one mandatory —
the equivalent of `argparse`'s `add_subparsers(required=True)`, Click groups,
or `git` / `docker` / `kubectl`:

```toml
[db]
description     = "Database administration"
require-command = true        # `db` on its own is an error

[db.commands.migrate]
description = "Apply pending migrations"

[db.commands.seed]
description = "Seed the database with fixtures"
```

Invoking the level with no command — or a token that is not one of its
commands — errors with the list of available commands (a mistyped token also
gets a `Did you mean` suggestion). It is a **parent-level** flag and applies at
any nesting depth: a nested command that itself declares `require-command`
enforces its own children. Enforcement happens only when parsing real CLI
arguments — `load_spec()` / `loadSpec()` introspection and schema emission are
unaffected, so agent tooling still sees the runnable.

!!! note "Availability"
    `require-command` was added in **runspec 0.23.0** and **node-0.18.0**.

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

### `enforce_run_as`

`run_as` is applied by an executor (`runspec serve`, an SSH client, the console)
*before* the process starts, so under one the runnable is already the target
user. When a runnable is invoked directly — its installed entry point run by
hand, from cron, or imported — nothing escalates and nothing checks who is
running it. `enforce_run_as` closes that gap: on `parse()`, the resolved
`run_as` user is compared (by uid) to the current process's effective user.

```toml
[deploy]
run_as         = "oracle"
enforce_run_as = "error"    # default — also: "warn", "off"
```

| Value | On mismatch |
|---|---|
| `"error"` (default) | `parse()` exits non-zero — the runnable does not run. |
| `"warn"` | A warning is written to stderr; the runnable continues. |
| `"off"` | No check. |

It is a no-op when `run_as` resolves to the empty string (no escalation
intended) or on a platform with no POSIX uid model (e.g. Windows). The default
can be set project-wide via `[config] enforce_run_as`; a runnable's own value
wins.

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

For a small, flat metadata bag, an inline TOML table keeps it on one line with
the argument definition — no separate `[...meta]` section needed:

```toml
[deploy.args.timeout]
default = 30
meta = { unit = "seconds", help-url = "https://wiki/timeouts" }
```

```python
args = parse()
print(args.timeout.meta["unit"])   # "seconds"
```

The section form and the inline form are equivalent — runspec treats `meta` as
pass-through either way. Reach for inline when the table is short; use a
`[...meta]` section when it spans many keys or nests deeply.

### Combining two values

`meta` is keyed by a single argument's value, and runspec does no
interpolation — there is no `"${region}-${tier}"` template syntax. When a
value depends on two arguments, compose the lookup in your own code. Nest the
table and index it twice:

```toml
[deploy.args.region.meta]
us-east = {web = "10.0.1.0/24", db = "10.0.2.0/24"}
us-west = {web = "10.1.1.0/24", db = "10.1.2.0/24"}
```

```python
args = parse()
subnet = args.region.meta[args.region.value][args.tier.value]
```

Or build a composite key from both values:

```toml
[deploy.args.target.meta]
"us-east|web" = {subnet = "10.0.1.0/24"}
"us-east|db"  = {subnet = "10.0.2.0/24"}
```

```python
args = parse()
info = args.target.meta[f"{args.region.value}|{args.tier.value}"]
```

Because `meta` is pure pass-through, any composition — string formatting like
`f"View {args.jira_key.value}"`, joining multiple values, computed defaults —
happens in your runnable, not in `runspec.toml`.

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

---

**Next:** [Migrating from argparse](migration.md) — arriving from argparse? Start here.
