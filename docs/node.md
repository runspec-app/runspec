# Node Library

The Node package (`runspec-node`) brings the same interface specification to
Node.js and TypeScript projects. Same TOML format, same CLI, same MCP server
— just `parse()` returning a `ParsedArgs` object instead of a Python `RunSpec`.

!!! info "Version"
    This page documents **runspec-node 0.10.0**. Node 18+ is required; CI
    covers 18, 20, and 22.

---

## Installation

```bash
npm install runspec-node
```

One runtime dependency: `smol-toml` (TOML parsing — Node has no stdlib TOML
parser). TypeScript types are bundled — no separate `@types/` package. The
`runspec` binary is installed alongside the library — see
[CLI Reference](cli.md).

---

## parse()

```typescript
import { parse } from 'runspec-node';

const args = parse();
```

That's the whole call. runspec finds your config, resolves the runnable name,
parses `process.argv`, validates, coerces, and returns a `ParsedArgs`.

### Signature

```typescript
function parse(opts?: ParseOptions): ParsedArgs

interface ParseOptions {
  scriptName?: string;  // override runnable name (inferred from script filename otherwise)
  argv?: string[];      // override process.argv.slice(2)
  cwd?: string;         // start directory for config search (default: process.cwd())
  configPath?: string;  // explicit path to runspec.toml (overrides cwd walk)
}
```

### What it does

1. Resolves the config file (`configPath` → `RUNSPEC_CONFIG` env → walk up
   from `cwd`).
2. Infers the runnable name from the script filename.
3. Applies inference rules to fill in `type` and `required`.
4. Resolves any subcommand from `argv`.
5. Intercepts `--help` / `-h` and prints usage, then exits.
6. **If `[config.logging]` is present, configures the logger** and injects
   `--log-level` (see [Logging](logging.md)).
7. Parses `argv` into raw values.
8. Applies environment variable fallbacks.
9. Applies spec defaults.
10. Validates individual args, then group constraints.
11. Coerces values to native JavaScript types.
12. Returns a `ParsedArgs`.

### Errors

| Exception | When |
|---|---|
| `RunSpecError` | No config found, runnable not in spec, reserved name used |
| `MissingRequiredArg` | A required arg was not provided |
| `InvalidChoice` | Value not in declared `options` |
| `OutOfRange` | Numeric value outside declared `range` |
| `UnknownArg` | An arg was passed that isn't in the spec |
| `GroupViolation` | A group constraint was violated |
| `AutonomyViolation` | Per-arg autonomy escalation was attempted unsafely |

All errors inherit from `RunSpecError`. Error messages include what was
expected, what was received, and a fuzzy suggestion where possible. For
project-wide validation use `runspec local`, which surfaces the same errors
at spec-load time (handy in CI).

### Testing

Pass `argv` directly to test without touching `process.argv`:

```typescript
import { parse } from 'runspec-node';

test('greet with --loud', () => {
  const args = parse({ argv: ['--name', 'Alice', '--loud'] });
  expect(args.name).toBe('Alice');
  expect(args.loud).toBe(true);
});
```

---

## ParsedArgs

`parse()` returns a `ParsedArgs` — a plain object with coerced argument
values. Hyphens in arg names become underscores; access values by key:

```typescript
const args = parse();

const name     = args.name as string;
const workers  = args.workers as number;
const inputDir = args.input_dir as string;   // --input-dir → input_dir
const format   = args.format as string;
const tags     = (args.tag as string[]) ?? [];
const dryRun   = args.dry_run as boolean;
```

Unlike the Python `Arg` proxy, there is no transparent wrapper — `args.name`
IS the coerced value. Cast to the TypeScript type you expect, or use a small
helper:

```typescript
function get<T>(args: ParsedArgs, key: string): T {
  return args[key] as T;
}

const workers = get<number>(args, 'workers');
```

### Metadata properties

`ParsedArgs` exposes invocation context. The `__runspec_*__` keys are the
storage; the `runspec_*` properties below them are the recommended API:

| Property | Type | Description |
|---|---|---|
| `__runspec_script__` | `string` | Name of the runnable |
| `__runspec_source__` | `string` | Absolute path to `runspec.toml` |
| `__runspec_command_path__` | `string[]` | Subcommand path, deepest last |
| `__runspec_autonomy__` | `string` | Effective autonomy after escalation |
| `__runspec_agent__` | `boolean` | `true` under `runspec serve` (RUNSPEC_AGENT=1) |
| `__runspec_spec__` | `ScriptSpec` | Raw, fully-inferred spec for the runnable |
| `runspec_command` | `string \| undefined` | Active subcommand (leaf) |
| `runspec_command_path` | `string[]` | Same as `__runspec_command_path__` |
| `runspec_prefix` | `string` | Package root: directory containing `runspec.toml` |

```typescript
const args = parse();

console.log(args.__runspec_script__);    // "deploy"
console.log(args.runspec_command);       // "run"  (if a subcommand was matched)
console.log(args.__runspec_autonomy__);  // "confirm"
console.log(args.__runspec_agent__);     // true under runspec serve
console.log(args.runspec_prefix);        // "/home/user/project/mypkg"
```

### `runspec_prefix` — package-relative paths

```typescript
import * as path from 'path';
import * as fs from 'fs';

const args = parse();
const templatesDir = path.join(args.runspec_prefix, 'templates');
const files = fs.readdirSync(templatesDir);
```

Much sturdier than `__dirname`, which moves around when the runnable is
invoked via a wrapper or `runspec serve`.

### Autonomy gating

`__runspec_autonomy__` reflects the most restrictive level across the
runnable, its args, and any per-arg overrides. Use it to refuse agent
invocation of destructive actions:

```typescript
if (args.delete && args.__runspec_agent__ && args.__runspec_autonomy__ !== 'autonomous') {
  console.error("✗ --delete requires autonomy='autonomous' for agent invocation");
  process.exit(1);
}
```

### Agent-aware output

```typescript
if (args.__runspec_agent__) {
  console.log(JSON.stringify({ status: 'deployed', env: args.env }));
} else {
  console.log(`✓ Deployed to ${args.env}`);
}
```

---

## loadSpec()

```typescript
import { loadSpec } from 'runspec-node';

const spec = loadSpec();
const specForDeploy = loadSpec({ scriptName: 'deploy', cwd: '/path/to/project' });
```

Loads the spec without parsing `process.argv`. Returns a `ParsedArgs` with
default values only — no CLI args applied. Accepts the same `ParseOptions`
as `parse()`. Used for tooling, introspection, and code generation:

```typescript
import { loadSpec } from 'runspec-node';
import type { ScriptSpec } from 'runspec-node';

const spec = loadSpec({ scriptName: 'deploy' });
const scriptSpec = spec.__runspec_spec__ as ScriptSpec;

for (const [name, arg] of Object.entries(scriptSpec.args)) {
  console.log(`${name}: ${arg.type} (required=${arg.required})`);
}
```

---

## registerType()

```typescript
import { registerType, listTypes } from 'runspec-node';

function registerType(name: string, coercer: (value: unknown) => unknown): void
function listTypes(): string[]
```

Register a custom type. The coercer receives the raw value and returns the
coerced value. Throw to produce a clean error message.

```typescript
import * as fs from 'fs';
import { registerType } from 'runspec-node';

registerType('json-file', (v) =>
  JSON.parse(fs.readFileSync(v as string, 'utf-8'))
);

registerType('port', (v) => {
  const port = Number(v);
  if (!Number.isInteger(port) || port < 1 || port > 65535)
    throw new Error(`${v} is not a valid port number`);
  return port;
});
```

Then in your spec:

```toml
[server.args]
config = {type = "json-file"}
port   = {type = "port", default = 8080}
```

---

## Logging integration (`getLogger`)

When `[config.logging]` is present in your `runspec.toml`, `parse()`
configures a lightweight logger automatically. Call `getLogger(name)` to
obtain a named logger — that's the entire integration.

```typescript
import { parse, getLogger } from 'runspec-node';

const logger = getLogger('deploy');

function main(): void {
  const args = parse();

  logger.info('Deploy starting for %s', args.target);
  logger.info('Result', {
    target: args.target,
    duration_ms: 1240,
  });
}

main();
```

The trailing object becomes structured `extra` fields; the special `error`
key extracts an `Error`:

```typescript
try {
  await runDeploy(args);
} catch (err) {
  logger.error('Deploy failed', { target: args.target, error: err });
  process.exit(1);
}
```

Sensitive-data redaction (passwords, tokens, `Authorization` headers, URL
credentials) is applied to every log line. See [Logging](logging.md) for
the full picture: rotation policies, agent-mode behaviour, and the
auto-injected `--log-level` flag.

---

## Exports

Everything `runspec-node` exposes from the package root:

| Export | Kind | Description |
|---|---|---|
| `parse` | function | Parse argv, return `ParsedArgs` |
| `loadSpec` | function | Load spec without parsing argv |
| `registerType` | function | Register a custom type coercer |
| `listTypes` | function | List all registered type names |
| `getLogger` | function | Get a named logger (no-op without `[config.logging]`) |
| `findConfig` | function | Locate the nearest `runspec.toml` |
| `loadRaw` | function | Read and parse a `runspec.toml` to its raw dict form |
| `RunSpecError` | class | Base error class |
| `MissingRequiredArg`, `InvalidChoice`, `OutOfRange`, `UnknownArg`, `GroupViolation`, `AutonomyViolation` | classes | Specific error subclasses |
| `ParsedArgs`, `ScriptSpec`, `ArgSpec`, `GroupSpec`, `RawSpec`, `RawConfig`, `LoggingConfig` | types | TypeScript interfaces |

---

## Errors

```typescript
import {
  RunSpecError,       // base class
  MissingRequiredArg,
  InvalidChoice,
  OutOfRange,
  UnknownArg,
  GroupViolation,
  AutonomyViolation,
} from 'runspec-node';
```

Error messages include context, expected values, and fuzzy suggestions:

```
✗  Missing required argument: --input
   Type: path
   Tip: set environment variable PIPELINE_INPUT as an alternative

✗  Invalid value for --format: 'yml'
   Expected one of: json, csv, parquet
   Got: 'yml'

   Did you mean: json?
```

Catch the base class for uniform handling:

```typescript
try {
  const args = parse();
  // ... your runnable ...
} catch (e) {
  if (e instanceof RunSpecError) {
    console.error(e.message);
    process.exit(1);
  }
  throw e;
}
```

---

## CLI

The `runspec` binary is included in `runspec-node`. Use it via `npx` in any
project that has `runspec-node` installed:

```bash
npx runspec init                   # scaffold runspec.toml + code stub
npx runspec local                  # list installed runnables and validate
npx runspec local --format mcp     # emit MCP tool schemas
npx runspec serve                  # start the MCP stdio server
```

Or install globally for direct access:

```bash
npm install -g runspec-node
runspec local
```

The CLI is identical to the Python version and reads the same `runspec.toml`
format. If both `runspec` (Python) and `runspec-node` are installed, either
binary works.

!!! note "Jump-host execution"
    `runspec jump` is fully implemented in the Python CLI. The Node CLI
    accepts the `jump` subcommand but currently prints a pointer to the
    Python package; full Node parity is on the roadmap. See
    [Jump Hosts](jump-hosts.md).

See the [CLI Reference](cli.md) for full documentation of all commands.

---

## MCP server

`runspec serve` starts a JSON-RPC 2.0 MCP stdio server for your project. It
reads your `runspec.toml`, exposes all runnables as tool schemas, and
executes them when an agent calls a tool.

**How it finds scripts:** the Node serve command looks in `node_modules/.bin/`
relative to your config file. Any package you install that declares a `bin`
entry appears there automatically. Name your runnable the same as the binary
it wraps.

```
project/
  runspec.toml             # [process] runnable defined here
  node_modules/
    .bin/
      process              # ← runspec serve finds this and runs it
```

**Connect Claude Desktop or any MCP client:**

=== "Via npx (no global install required)"

    ```json
    {
      "mcpServers": {
        "my-tools": {
          "command": "npx",
          "args": ["--yes", "runspec-node", "serve"],
          "cwd": "/path/to/project"
        }
      }
    }
    ```

=== "Global install"

    ```json
    {
      "mcpServers": {
        "my-tools": {
          "command": "runspec",
          "args": ["serve"],
          "cwd": "/path/to/project"
        }
      }
    }
    ```

=== "Project local (package.json scripts)"

    ```json
    {
      "mcpServers": {
        "my-tools": {
          "command": "npm",
          "args": ["run", "serve"],
          "cwd": "/path/to/project"
        }
      }
    }
    ```

    With `"serve": "runspec serve"` in your `package.json` scripts.

See [Agent Integration](agents.md) for autonomy gating, agent-aware output,
and the `RUNSPEC_AGENT` convention.

---

## Complete example

```toml
# runspec.toml
[config.logging]
level  = "info"
rotate = "midnight"
keep   = 7

[process]
description = "Process input files"
autonomy    = "confirm"

[process.args]
input    = {type = "path"}
format   = {options = ["json", "csv"], default = "json"}
workers  = {default = 4, range = [1, 16]}
dry-run  = {default = false}
verbose  = {default = false, short = "-v"}
api-key  = {type = "str", env = "PROCESS_API_KEY", autonomy = "manual"}
tag      = {type = "str", multiple = true}
```

```typescript
import { parse, getLogger } from 'runspec-node';

const logger = getLogger('process');

function main(): void {
  const args = parse();

  const input   = args.input as string;
  const format  = args.format as string;
  const workers = args.workers as number;
  const dryRun  = args.dry_run as boolean;
  const verbose = args.verbose as boolean;
  const tags    = (args.tag as string[]) ?? [];
  const isAgent = args.__runspec_agent__;

  logger.info('Run starting', { format, workers, tags });

  if (dryRun) {
    if (isAgent) {
      console.log(JSON.stringify({ status: 'dry-run', input }));
    } else {
      console.log(`[dry run] would process ${input} as ${format}`);
    }
    return;
  }

  // ... do the work ...

  if (isAgent) {
    console.log(JSON.stringify({ status: 'ok', tags }));
  } else if (verbose) {
    console.log(`Processed ${input} with ${workers} workers`);
    if (tags.length) console.log(`Tags: ${tags.join(', ')}`);
  }
}

main();
```

Running it:

```bash
node dist/process.js --input data.csv --workers 8 --tag etl --tag prod
```

Or from an agent via `runspec serve` — no code change needed. `__runspec_agent__`
switches the output format automatically.
