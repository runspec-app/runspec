# Agent Integration

runspec turns any runnable into something an agent can understand and safely
use — without any extra code beyond defining your interface in TOML.

There are two ways agents discover runspec runnables:

- **Locally** via `runspec serve` — MCP stdio server exposing every installed
  runspec-aware runnable as a tool.
- **Remotely** via `runspec jump` — SSH+MCP into a configured jump host and
  run a tool there. See [Jump Hosts](jump-hosts.md).

---

## The problem runspec solves

Agents need to know three things about every tool they might call:

1. **What arguments does it take?** Types, required vs optional, valid values.
2. **What does it do?** Enough context to decide whether to call it.
3. **Can it run this automatically?** Or does it need to stop and ask a human first?

Without a structured interface, agents are guessing. With runspec, the
answers are machine-readable, always accurate, and derived from the same
source the CLI uses — so they can never drift.

---

## Emitting schemas

`runspec local --format mcp` converts your installed runnables into JSON
Schema tool definitions ready for any agent framework.

```bash
runspec local --format mcp
```

```json
{
  "tools": [
    {
      "name": "greet",
      "description": "Greet someone from the command line",
      "x-autonomy": "autonomous",
      "x-output": "text",
      "inputSchema": {
        "type": "object",
        "properties": {
          "name":  { "type": "string" },
          "loud":  { "type": "boolean", "default": false },
          "times": { "type": "integer", "default": 1 }
        },
        "required": ["name"]
      }
    }
  ]
}
```

The agent gets the full interface: argument names, types, defaults, which
args are required, autonomy level, and output type — all from the one place
you already maintain.

### Format options

```bash
runspec local --format mcp                       # MCP (the standard schema format)
runspec local --format openai                    # OpenAI tool calling
runspec local --format anthropic                 # Anthropic tool use
runspec local --format mcp --runnable deploy     # one runnable only
```

---

## Autonomy control

The `x-autonomy` field in every emitted schema declares how an agent runtime
should gate invocation. **This is a contract for agent invocation, not a
directive for human users** — a human typing the command has already chosen
the action.

| Level | What it means for an agent |
|---|---|
| `autonomous` | Run freely — no confirmation needed |
| `confirm` | Stop and confirm with the user before running |
| `supervised` | Run, but hold the output for human review before acting on it |
| `manual` | Do not call this tool — hand off to a human entirely |

This is set in your spec:

```toml
[deploy]
description     = "Deploy to production"
autonomy        = "manual"
autonomy-reason = "Irreversible — requires human sign-off"

[compress]
description = "Compress output files"
autonomy    = "autonomous"
```

And surfaces in the emitted schema:

```json
{
  "name": "deploy",
  "description": "Deploy to production",
  "x-autonomy": "manual",
  "x-autonomy-reason": "Irreversible — requires human sign-off",
  "x-output": "text",
  "inputSchema": { ... }
}
```

A conforming MCP host reads `x-autonomy` and gates accordingly: blocking
`manual` tools, prompting before `confirm` tools, and running `autonomous`
tools freely.

### Per-argument autonomy

Autonomy can also be declared on individual arguments. The most restrictive
level wins — so a `confirm`-level runnable with a `manual`-level arg becomes
effectively `manual` when that arg is used:

```toml
[pipeline.args]
input   = {type = "path"}
api-key = {type = "str", env = "PIPELINE_API_KEY", autonomy = "manual"}
```

The runspec library calculates the effective autonomy for you and exposes it
on the parsed result:

=== "Python"

    ```python
    args = runspec.parse()
    print(args.runspec_autonomy)   # "manual" if api-key was provided
    ```

=== "Node"

    ```typescript
    const args = parse();
    console.log(args.__runspec_autonomy__);  // "manual" if api-key was provided
    ```

### Tool-side enforcement

Because host gating isn't universal, runnables that perform destructive
actions should also enforce autonomy themselves at runtime. The recommended
pattern for a destructive flag:

=== "Python"

    ```python
    if args.delete:
        if args.runspec_agent and args.runspec_autonomy != "autonomous":
            raise SystemExit(
                "✗ --delete requires autonomy='autonomous' for agent invocation"
            )
        # ... proceed
    ```

=== "Node"

    ```typescript
    if (args.delete && args.__runspec_agent__ && args.__runspec_autonomy__ !== 'autonomous') {
      console.error("✗ --delete requires autonomy='autonomous' for agent invocation");
      process.exit(1);
    }
    ```

This refuses agent invocation unless the spec explicitly permits unattended
execution. Human invocation is unaffected.

---

## Discovery

`runspec local` finds every runspec-aware runnable installed in the
environment and reports them — with no per-tool configuration.

```bash
runspec local
```

```
Found 3 installed runnable(s):

  /home/user/project/mypkg/runspec.toml
    deploy       Deploy the application    [confirm]
    process      Process input files       [confirm]
    validate     Validate input data       [autonomous]

Run 'runspec local --format mcp' to emit MCP tool schemas.
```

With `--format mcp`, this becomes a complete tool list ready to hand to an
MCP server or agent framework — no per-tool setup, no `skills.md` to
maintain.

Runnables must be installed (`pip install -e .` for Python; `npm install`
for Node) to appear.

---

## Environment variables for agents

The `env` field lets sensitive arguments be set via environment variables
rather than passed directly on the command line. Combined with
`autonomy = "manual"`, this keeps secrets out of agent reach entirely:

```toml
[deploy.args]
server  = {type = "str",  env = "DEPLOY_SERVER"}
api-key = {type = "str",  env = "DEPLOY_API_KEY", autonomy = "manual"}
region  = {type = "str",  env = "AWS_REGION", default = "us-east-1"}
```

The operator sets the environment variables. The agent calls the runnable
with zero args. The runnable gets the values it needs. The agent never sees
or touches the secrets.

This pattern works in any environment where you can set variables before the
agent runs — CI/CD pipelines, container orchestration, Ansible, Docker
Compose, system services.

---

## Live MCP server

`runspec serve` starts an MCP stdio server that exposes every runnable in
your environment as a callable tool. This is the recommended way to connect
any MCP-compatible agent — Claude Desktop, Cursor, or your own agent loop —
to your runnables.

```bash
runspec serve
```

The server reads your runspec config, advertises each runnable as an MCP
tool, and runs the corresponding script when the agent calls it. No separate
MCP server to write or maintain.

### Connecting Claude Desktop

Add an entry to `claude_desktop_config.json` (typically at
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS
or `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

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

The key (`"analytics-pipeline"`) is the display name shown in Claude
Desktop. `cwd` is the directory `serve` searches for your runspec config —
set it to your project root.

Each virtual environment is its own MCP server. For multiple projects, add
one entry per environment — each exposes only its own runnables.

### What the agent sees

The agent receives tool definitions with full argument schemas,
descriptions, and autonomy levels — everything from your TOML. Calling a
tool runs the script and returns its stdout. On non-zero exit, the tool
returns an error with the exit code, stdout, and stderr intact.

### Agent-aware output

Scripts called via `serve` receive `RUNSPEC_AGENT=1` in their environment.
Read it through the parsed args to switch between human and machine output:

=== "Python"

    ```python
    args = runspec.parse()

    if args.runspec_agent:
        print(json.dumps({"status": "ok", "deployed_to": str(args.env)}))
    else:
        print(f"✓ Deployed to {args.env}")
    ```

=== "Node"

    ```typescript
    const args = parse();

    if (args.__runspec_agent__) {
      console.log(JSON.stringify({ status: 'ok', deployed_to: args.env }));
    } else {
      console.log(`✓ Deployed to ${args.env}`);
    }
    ```

When the runnable's `output` field is set to `"json"`, set it in your spec
and agents will get `x-output: "json"` in the schema — they know to parse
the response rather than display it as text.

---

## Logging in agent mode

When `[config.logging]` is configured, runspec adjusts its behaviour for
agent invocations:

- **No console handler.** stderr is the MCP streaming side-channel; writing
  log lines there would corrupt the JSON-RPC framing.
- **File logging still at DEBUG.** `{package_dir}/logs/{runnable}.log` is
  your debugging surface for everything an agent invoked.
- **`--log-level` is available as a runtime knob.** An agent can pass
  `--log-level debug` (or set `RUNSPEC_LOG_LEVEL=debug`) on a one-off
  invocation without editing the spec.

You write the same `logger.info(...)` calls; runspec routes them
appropriately. See [Logging](logging.md) for the full picture, including
the sensitive-data redaction filter.

---

## Remote execution

For tools that need to run on a different machine, configure
`[config.jump-hosts]` and use `runspec jump`. The agent talks to a local
`runspec jump` process, which SSHes to the remote and speaks MCP with
`runspec serve` over there.

```bash
runspec jump --list-jump-hosts                       # list configured hosts
runspec jump prod-app                                # list tools on prod-app
runspec jump prod-app deploy -- --env production     # run a tool on prod-app
```

The full model — SSH argv construction, `hosts` filtering, `run_as` /
`become_method` / `become_flags` privilege escalation, the trust model — is
on the dedicated [Jump Hosts](jump-hosts.md) page.

!!! note "Replaces the old `runspec-registry`"
    Earlier versions of runspec shipped an HTTP registry service for tool
    discovery. It was removed in 0.7.0 in favour of `[config.jump-hosts]` +
    SSH + MCP. The `runspec-registry` PyPI package is archived.

---

## Practical patterns

### Validate before exposing

Run `runspec local` in CI before deploying. It reports missing descriptions,
undeclared autonomy levels, required args without descriptions, and other
agent-facing gaps. Exits with code 1 on errors:

```yaml
# .github/workflows/ci.yml
- name: Validate runspec
  run: runspec local
```

### Emit schemas for inspection

```bash
runspec local --format mcp        # exactly what the agent will see
runspec local --format openai     # for an OpenAI-shaped framework
```

### Check before running (from an agent loop)

```python
import json, subprocess

schemas = json.loads(subprocess.check_output(["runspec", "local", "--format", "mcp"]))

for tool in schemas["tools"]:
    autonomy = tool.get("x-autonomy", "confirm")
    if autonomy == "manual":
        print(f"  {tool['name']}: skip — requires human operator")
    elif autonomy == "confirm":
        print(f"  {tool['name']}: ask user first")
    else:
        print(f"  {tool['name']}: ok to run")
```

---

## What agents see

A well-specified runnable gives an agent everything it needs without any
extra documentation:

| Spec field | Agent use |
|---|---|
| `description` | Decides whether this is the right tool |
| `args[].description` | Knows what value to pass |
| `args[].type` | Passes the right type |
| `args[].options` | Picks from valid choices |
| `args[].required` | Knows what it must provide |
| `args[].default` | Knows what happens if it doesn't provide |
| `args[].env` | Knows there's an env var fallback |
| `x-autonomy` | Decides whether to run or ask first |
| `x-autonomy-reason` | Explains to the user why confirmation is needed |
| `x-output` | Knows whether to display output as text or parse it as JSON |

The more of these you fill in, the more reliably an agent can use your
runnable without human help.

!!! tip
    Run `runspec local` to see exactly which fields are missing. The output
    maps directly to gaps in what agents can infer.
