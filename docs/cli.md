# CLI Reference

The `runspec` binary ships with both `runspec` (Python) and `runspec-node`.
It scaffolds new projects, validates configs, emits agent schemas, runs a
live MCP server, and executes tools on remote hosts.

```
runspec <command> [options]
```

!!! tip "It's runspec all the way down"
    Both CLIs define their own command surface in `runspec.toml`. When you
    run `runspec --help`, you're seeing the same renderer your CLI gets —
    the same argument validation, the same `examples` field, the same
    inference. The bundled specs live at:

    - `packages/python/runspec/runspec/runspec.toml`
    - `packages/node/src/runspec.toml`

    Read either as a worked example of a non-trivial spec: subcommands,
    `examples`, `position`, `rest`, `choice`, short flags, and a `[config]`
    block that suppresses autonomy display on the developer-facing menu.

---

## Top-level options

| Option | Description |
|---|---|
| `-V`, `--version` | Print package version and exit |
| `-h`, `--help` | Show help (also shown when no command is given) |

---

## `runspec init`

Scaffold a new runnable: write `runspec.toml` and a working code stub.

```bash
runspec init [--name <name>] [--lang <lang>] [--example] \
             [--write-project] [--project-dir <dir>] [--force]
```

| Flag | Default | Description |
|---|---|---|
| `-n`, `--name <name>` | current directory name | Name for the initial runnable |
| `--lang <lang>` | `python` (Python CLI) / `typescript` (Node CLI) | Code stub language: `python`, `typescript`, `javascript` |
| `-e`, `--example` | off | Generate worked example runnables (`clean` + `scan`) with confirmation prompts, conditional deletion, autonomy escalation |
| `-w`, `--write-project` | off | **Python CLI only.** Also generate `pyproject.toml`, `__init__.py`, `.gitignore`, and `CLAUDE.md` in the parent directory |
| `-d`, `--project-dir <dir>` | parent directory | Where `--write-project` lays down its files |
| `--force` | off | Bypass the cwd safety check (don't refuse if `pyproject.toml` is already present) |

If `runspec.toml` already exists, `init` exits with an error — it will not
overwrite or merge. If a code stub already exists, it is skipped with an
informational message.

### What gets written

=== "Bare init"

    ```
    greet/
      runspec.toml
      greet.py            # or greet.ts / greet.js depending on --lang
    ```

=== "init --example"

    ```
    sandbox/
      runspec.toml        # defines clean + scan runnables
      clean.py            # destructive op with autonomy/confirmation
      scan.py             # read-only op marked autonomy = "autonomous"
    ```

=== "init --write-project (Python)"

    ```
    .                     # parent of cwd, by default
    ├── pyproject.toml    # [project.scripts] already wired up
    ├── .gitignore
    ├── CLAUDE.md         # project memory for Claude Code
    └── greet/
        ├── __init__.py
        ├── runspec.toml
        └── greet.py
    ```

`--lang typescript` and `--lang javascript` generate a `.ts` / `.js` stub
respectively. The Node CLI supports the same `--lang` values but `python` is
not in its menu (use the Python CLI to scaffold a Python project).

### Examples (from the bundled spec)

```bash
runspec init                                              # use cwd name as the runnable name
runspec init --name deploy                                # scaffold a runnable called 'deploy'
runspec init --example                                    # generate worked example (clean + scan)
runspec init --example --write-project                    # also generate pyproject.toml
runspec init --write-project --project-dir /tmp/myproject # write project files to a specific path
runspec init --name myapp --lang typescript               # use TypeScript code stub
```

---

## `runspec local`

List all runspec-aware runnables installed in this environment, with inline
validation. Also emits tool schemas for agents.

```bash
runspec local [--format <fmt>] [--runnable <name>]
```

| Flag | Default | Description |
|---|---|---|
| `-f`, `--format <fmt>` | `text` | Output format: `text`, `json`, `mcp`, `openai`, `anthropic` |
| `-r`, `--runnable <name>` | all | Filter to one runnable by name |

**Discovery** uses `importlib.metadata` (Python) or `node_modules/.bin/`
(Node) — no filesystem walk, no guessing. Runnables must be installed
(`pip install -e .` / `npm install`) to appear.

### Text output (default)

Shows installed runnables grouped by config file, with autonomy levels and
any config issues inline:

```bash
runspec local
```

```
Found 3 installed runnable(s):

  /home/user/project/mypkg/runspec.toml
    deploy       Deploy the application to an environment  [confirm]
    backup-logs  Back up application logs to S3            [manual]
    process      Process input files                       [confirm]

Issues:

  ℹ  'process' autonomy not declared — defaulting to 'confirm'
  ✗  'process.api-key' is required but has no description

Run 'runspec local --format mcp' to emit MCP tool schemas.
```

Exits with code 1 if any errors are found — usable as a CI gate:

```yaml
# .github/workflows/ci.yml
- name: Validate runspec
  run: runspec local
```

### Schema output

Emit all installed runnables as tool schemas for agent frameworks:

```bash
runspec local --format mcp          # MCP tool list (the standard schema format)
runspec local --format openai       # OpenAI tool calling format
runspec local --format anthropic    # Anthropic tool use format
runspec local --format json         # raw discovery JSON for tooling
```

Target a single runnable:

```bash
runspec local --format mcp --runnable deploy
```

Every emitted tool carries:

| Field | Source |
|---|---|
| `name` | Runnable name |
| `description` | `description` field |
| `x-autonomy` | Effective autonomy level |
| `x-autonomy-reason` | `autonomy-reason` if declared |
| `x-output` | `output` field (`"text"` if not declared) |
| `inputSchema` | JSON Schema for all args |

---

## `runspec serve`

Start a live MCP stdio server for the current environment.

```bash
runspec serve
```

No arguments. Reads the runspec config (walking up to find `runspec.toml`),
then starts a [Model Context Protocol](https://modelcontextprotocol.io)
server over stdin/stdout. Every installed runnable is exposed as an MCP
tool. When an agent calls a tool, `serve` runs the corresponding script and
streams the output back.

Zero extra dependencies — the protocol is JSON-RPC 2.0 newline-delimited over
stdin/stdout (plain stdlib).

### Discovery

| Pack | How runnables are found |
|---|---|
| Python | `importlib.metadata` — installed packages that declare `runspec` as a dependency |
| Node | Filesystem scan of cwd and subdirectories, plus `node_modules/.bin/` |

For Python, `pip install -e .` is the convention for making a package
visible during development.

### Subcommand flattening

Runnables that declare subcommands (`[<name>.commands.<sub>]`) are
automatically expanded into flat MCP tools with underscore-joined names:

```toml
[portal-api.commands.orders.commands.get-list]
description = "List orders"
autonomy    = "confirm"
```

Becomes the MCP tool `portal-api_orders_get-list`, with command
`[portal-api, orders, get-list, …args]` assembled at invocation time.

### Host filtering

If a runnable declares a `hosts` field, `serve` checks the current machine's
hostname at startup. Tools that don't match are excluded from the MCP tool
list. See [Jump Hosts](jump-hosts.md) for the remote-execution model.

### Environment variables on every invocation

Before running a script, `serve` injects:

```
RUNSPEC_<ARG_NAME_UPPERCASED>=<value>   # for every arg declared in the spec
RUNSPEC_AGENT=1                          # always, so the runnable can branch
RUNSPEC_CONFIG=/abs/path/to/runspec.toml # so parse() finds the spec in the subprocess
```

Hyphens become underscores; `flag`/`bool` values are `0` or `1`;
`multiple = true` lists are newline-delimited. Defaults are always set even
when the caller didn't pass the arg.

### Connecting to Claude Desktop

```json
{
  "mcpServers": {
    "analytics-pipeline": {
      "command": "/home/user/envs/analytics-pipeline/bin/runspec",
      "args": ["serve"],
      "cwd": "/home/user/projects/analytics"
    }
  }
}
```

On Windows:

```json
{
  "mcpServers": {
    "analytics-pipeline": {
      "command": "C:\\envs\\analytics-pipeline\\Scripts\\runspec.exe",
      "args": ["serve"],
      "cwd": "C:\\projects\\analytics"
    }
  }
}
```

See [Agent Integration](agents.md) for autonomy gating, schema fields, and
the `RUNSPEC_AGENT` pattern.

---

## `runspec jump`

List jump hosts, list tools on a jump host, or run a tool via SSH+MCP.

```bash
runspec jump [--list-jump-hosts] [--format <fmt>]
             [<jump-host> [<tool>] [-- <tool-args>...]]
```

| Flag / Argument | Description |
|---|---|
| `-l`, `--list-jump-hosts` | List jump hosts configured in `[config.jump-hosts]` |
| `-f`, `--format <fmt>` | Output format for listings: `text` (default) or `json` |
| `<jump-host>` | Alias from `[config.jump-hosts.<alias>]` (positional) |
| `<tool>` | Tool to run on the remote (positional) |
| `-- <tool-args>...` | Args forwarded to the remote tool (`rest`-type) |

!!! info "Python today; Node soon"
    `runspec jump` is fully implemented in the Python CLI. The Node CLI
    currently directs `jump` invocations to the Python package. Use
    `pip install runspec` alongside `runspec-node` if you need remote
    execution from a Node project.

### Discover what's available

```bash
runspec jump --list-jump-hosts                  # list configured aliases
runspec jump --list-jump-hosts --format json    # same, as JSON
runspec jump myserver                           # list tools available on myserver
```

### Run a tool

```bash
runspec jump myserver deploy -- --env prod
runspec jump prod-app backup-logs -- --days 14 --dry-run
```

Everything after `--` is forwarded to the tool on the remote.

### How it works

1. Reads `[config.jump-hosts.<alias>]` from the nearest `runspec.toml`.
2. Builds the SSH command (`ssh -o BatchMode=yes [-p N] [-i KEY]
   [-o OPT…] user@host runspec serve`).
3. Speaks MCP JSON-RPC over stdin/stdout to the remote `runspec serve`.
4. Invokes `tools/list` (without `<tool>`) or `tools/call` (with `<tool>`
   and tool args).
5. Streams the response back to your terminal in real time; stderr from the
   remote is mirrored live.
6. Exits with the remote process's exit code.

See [Jump Hosts](jump-hosts.md) for the SSH argv construction rules, all
four `[config.jump-hosts]` forms, the trust model, and the `run_as` /
`become_method` / `become_flags` privilege-escalation matrix.

---

## Bash and shell runnables

Any executable on `PATH` can be a runspec runnable — bash, Python, Node,
Ruby, Go binary, whatever. `runspec serve` invokes it with all argument
values pre-exported as `RUNSPEC_*` environment variables, so the runnable
doesn't need a runspec library:

```toml
[backup-logs]
description = "Back up application logs to S3"
autonomy    = "confirm"

[backup-logs.args]
env     = {type = "choice", options = ["prod", "staging"], description = "Target environment"}
days    = {type = "int", default = 7, description = "Days of logs to retain"}
dry-run = {type = "flag", description = "Print what would happen without doing it"}
```

```bash
#!/bin/bash
set -euo pipefail

if [ "$RUNSPEC_DRY_RUN" = "1" ]; then
    echo "Would sync $RUNSPEC_DAYS days of $RUNSPEC_ENV logs"
    exit 0
fi

aws s3 sync "/var/log/app/$RUNSPEC_ENV" "s3://logs-$RUNSPEC_ENV" \
    --delete --exclude "*.tmp"

echo "Backed up $RUNSPEC_DAYS days of $RUNSPEC_ENV logs"
```

Hyphens in arg names become underscores in the env var name (`dry-run` →
`RUNSPEC_DRY_RUN`). `RUNSPEC_AGENT=1` is always set so the script can branch
between human and agent output.

The runnable just has to be on `PATH` and have its section in
`runspec.toml`. `pip install` / `npm install` puts entry points on `PATH`
automatically; for bare shell scripts, drop them in your venv `bin/`
(Python) or expose them via a `bin` entry in `package.json` (Node).

---

## Usage in agent workflows

```bash
# 0. New project? Scaffold the whole thing
runspec init --name myapp --write-project

# 1. Check your config and see what's installed
runspec local

# 2. Preview what schemas an agent will see
runspec local --format mcp

# 3. Start the live MCP server
runspec serve

# 4. From an agent or terminal, run a tool on a jump host
runspec jump prod-app deploy -- --env prod
```

To wire it into Claude Desktop, point the MCP server config at `runspec
serve` (see the example above). The agent connects once at startup and
calls tools as needed.
