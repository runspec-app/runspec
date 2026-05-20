# Python Library

The Python library is the reference implementation. Install it, call
`parse()`, and you get a fully validated, type-coerced `RunSpec` object back.

!!! info "Version"
    This page documents **runspec 0.11.0**. Python 3.10+ is supported.
    Python 3.11+ has **zero runtime dependencies**; on 3.10, `tomli` is the
    only dependency (used as the `tomllib` backport).

---

## Installation

```bash
pip install runspec
```

The `runspec` binary is installed alongside the library — see
[CLI Reference](cli.md).

---

## parse()

```python
import runspec

args = runspec.parse()
```

That's the whole call. runspec finds your config, resolves the runnable name,
parses `sys.argv`, validates, coerces, and returns a `RunSpec`.

### Signature

```python
def parse(
    script_name: str | None = None,
    argv: list[str] | None = None,
    config_path: str | os.PathLike | None = None,
) -> RunSpec
```

| Parameter | Description |
|---|---|
| `script_name` | Override the runnable name. Inferred from `sys.argv[0]` if omitted. |
| `argv` | Override `sys.argv`. Uses `sys.argv[1:]` if omitted. Useful for testing. |
| `config_path` | Explicit path to `runspec.toml`. Overrides the cwd-walk and `RUNSPEC_CONFIG`. |

### What it does

1. Resolves the config file (`config_path` → `RUNSPEC_CONFIG` env → walk up
   from cwd).
2. Infers the runnable name from `sys.argv[0]`.
3. Applies inference rules to fill in `type` and `required`.
4. Resolves any subcommand from `argv`.
5. Intercepts `--help` / `-h` and prints usage, then exits.
6. **If `[config.logging]` is present, configures stdlib logging** and
   injects `--log-level` (see [Logging](logging.md)).
7. Parses `argv` into raw values.
8. Applies environment variable fallbacks.
9. Applies spec defaults.
10. Validates individual args, then group constraints.
11. Coerces values to native Python types.
12. Returns a `RunSpec`.

### Errors

| Exception | When |
|---|---|
| `FileNotFoundError` | No config file found |
| `RunSpecError` | Runnable not in config, reserved name used |
| `MissingRequiredArg` | A required arg was not provided |
| `InvalidChoice` | Value not in declared `options` |
| `OutOfRange` | Numeric value outside declared `range` |
| `UnknownArg` | An arg was passed that isn't in the spec |
| `GroupViolation` | A group constraint was violated |
| `AutonomyViolation` | Per-arg autonomy escalation was attempted unsafely |

All errors inherit from `RunSpecError`. Error messages include what was
expected, what was received, and a fuzzy suggestion where possible.

For project-wide validation use `runspec local`, which surfaces the same
class of errors at spec-load time with the same message style (handy in CI).

### Testing

Pass `argv` directly to test without touching `sys.argv`:

```python
def test_greet_loud():
    args = runspec.parse(argv=["--name", "Alice", "--loud"])
    assert args.name == "Alice"
    assert args.loud is True
```

---

## RunSpec

`parse()` returns a `RunSpec` — an argument namespace with full spec
metadata. Hyphens in arg names become underscores.

```python
args = runspec.parse()

print(args.name)          # str
print(args.workers)       # int
print(args.input_dir)     # pathlib.Path
print(args.format)        # str, one of declared options
```

### Metadata properties

`RunSpec` exposes invocation context via `runspec_*` properties (all
prefixed to avoid collisions with your arg names):

| Property | Type | Description |
|---|---|---|
| `runspec_runnable` | `str` | Name of the runnable (e.g. `"deploy"`) |
| `runspec_source` | `Path` | Path to the config file that was loaded |
| `runspec_prefix` | `Path` | Package root: parent directory of `runspec.toml` |
| `runspec_command` | `str \| None` | Active subcommand (leaf), if any |
| `runspec_command_path` | `list[str]` | Full subcommand path, deepest last |
| `runspec_autonomy` | `str` | Effective autonomy after escalation |
| `runspec_agent` | `bool` | `True` when called via `runspec serve` |
| `runspec_spec` | `dict` | Raw, fully-inferred spec for the runnable |
| `runspec_groups` | `list[Group]` | Group constraints declared on this runnable |

```python
args = runspec.parse()

print(args.runspec_runnable)     # "deploy"
print(args.runspec_command)      # "run"  (if a subcommand was matched)
print(args.runspec_autonomy)     # "confirm"
print(args.runspec_agent)        # True under runspec serve
print(args.runspec_source)       # PosixPath('/home/user/project/mypkg/runspec.toml')
print(args.runspec_prefix)       # PosixPath('/home/user/project/mypkg')
```

### `runspec_prefix` — package-relative paths

When a runnable needs to resolve a path relative to its package, use
`runspec_prefix`:

```python
args = runspec.parse()
templates = args.runspec_prefix / "templates"
for path in templates.glob("*.j2"):
    ...
```

This is much sturdier than `Path(__file__).parent`, which breaks when the
runnable is invoked as a wrapper script or via `runspec serve`.

### Autonomy gating

`runspec_autonomy` reflects the most restrictive level across the runnable,
its args, and any per-arg overrides. Use it to refuse agent invocation of
destructive actions:

```python
args = runspec.parse()

if args.delete:
    if args.runspec_agent and args.runspec_autonomy != "autonomous":
        raise SystemExit(
            "✗ --delete requires autonomy='autonomous' for agent invocation"
        )
    # ... proceed
```

This refuses agent invocation unless the spec explicitly permits unattended
execution. Human invocation is unaffected — a human at the terminal has
already chosen the action by passing the flag.

### Agent-aware output

`runspec_agent` is `True` when the runnable is called via `runspec serve`
(detected from `RUNSPEC_AGENT=1`). Use it to switch output format:

```python
args = runspec.parse()

if args.runspec_agent:
    print(json.dumps({"status": "deployed", "env": str(args.env)}))
else:
    print(f"✓ Deployed to {args.env}")
```

---

## Arg

Every argument is an `Arg` — a value plus its full spec metadata. `Arg` is
transparent: it behaves as its native type in expressions, so you rarely
need to think about it.

### Transparent value access

`Arg` implements the Python data model:

```python
# Arithmetic
total = args.batch_size * args.workers    # int * int
scaled = args.quality / 100               # int / int → float
items = args.workers + 2

# Boolean
if args.dry_run:                          # flag arg
    print("Dry run — no writes")

# Formatting
print(f"Format: {args.format!r}")         # __repr__
print(f"Quality: {args.quality:03d}")     # __format__

# Iteration (multiple=true args return a list)
for tag in args.tag:
    print(tag)

# Range / indexing
for i in range(args.workers):             # __index__
    ...

# Path args are pathlib.Path — all Path methods work
for file in args.input_dir.glob("*.csv"):
    ...
if args.output.is_dir():
    ...
```

### `__fspath__`, `__hash__`, `__getitem__`

`Arg` implements three additional dunders so it slots into common Python
APIs without unwrapping:

```python
# __fspath__: works directly with open(), os.path, pathlib
with open(args.input_path) as f:
    data = f.read()

shutil.copy(args.input_path, args.output_path)

# __hash__: works as a dict key or in a set
seen = {args.format, args.lang}

# __getitem__: indexing into multiple=true list values
first_tag = args.tag[0]
slice_   = args.tag[:3]
```

### Arg fields

Every `Arg` carries its full spec:

| Field | Type | Description |
|---|---|---|
| `value` | `Any` | Resolved, coerced value |
| `name` | `str` | Arg name as declared |
| `type` | `str` | Type name (`"str"`, `"int"`, `"path"`, …) |
| `required` | `bool` | Whether the arg is required |
| `default` | `Any` | Default from spec |
| `description` | `str \| None` | Description from spec |
| `options` | `list \| None` | Valid choices for `choice` type |
| `range` | `tuple \| None` | `(min, max)` for numeric types |
| `multiple` | `bool` | Whether the arg accepts multiple values |
| `delimiter` | `str \| None` | Split character for delimiter-separated values |
| `short` | `str \| None` | Short flag alias |
| `position` | `int \| None` | 1-based positional index if positional |
| `env` | `str \| None` | Environment variable name |
| `deprecated` | `str \| None` | Deprecation message |
| `autonomy` | `str \| None` | Per-arg autonomy override |
| `ui` | `str \| None` | Form control hint |
| `meta` | `dict \| None` | Developer-defined pass-through metadata |
| `source` | `str` | Where the value came from: `"cli"`, `"env"`, `"default"` |

```python
print(args.format.options)      # ['json', 'csv', 'parquet']
print(args.quality.range)       # (1, 100)
print(args.api_key.env)         # 'PIPELINE_API_KEY'
print(args.name.source)         # 'cli' | 'env' | 'default'
```

### `meta` — pass-through data

A common pattern is associating choice values with lookup data:

```toml
[deploy.args.server]
options = ["web-01", "web-02", "db-01"]

[deploy.args.server.meta]
web-01 = {datacenter = "us-east", tier = "web"}
web-02 = {datacenter = "us-west", tier = "web"}
db-01  = {datacenter = "eu-central", tier = "db"}
```

```python
args = runspec.parse()
info = args.server.meta[args.server.value]
print(info["datacenter"])   # "us-east"
```

---

## load_spec()

```python
def load_spec(script_name: str | None = None) -> RunSpec
```

Loads the spec without parsing `sys.argv`. Returns a `RunSpec` with default
values only — no CLI args applied. Useful for tooling, code generation, and
introspection:

```python
spec = runspec.load_spec("deploy")

print(spec.runspec_runnable)            # "deploy"
for name, arg in spec._args.items():
    print(f"{name}: {arg.type} (required={arg.required})")
```

This is what `runspec local --format mcp` uses internally — load the spec,
then serialise.

---

## register_type()

```python
def register_type(name: str, coercer: Callable[[Any, dict], Any]) -> None
```

Register a custom type. The coercer receives the raw value and the full arg
spec dict, and returns the coerced Python value. Raise `ValueError` to
produce a clean error message.

```python
import json
from pathlib import Path
import runspec

runspec.register_type(
    "json-file",
    lambda v, arg: json.loads(Path(v).read_text())
)


def coerce_port(raw: str, arg: dict) -> int:
    port = int(raw)
    if not (1 <= port <= 65535):
        raise ValueError(f"{port} is not a valid port number")
    return port


runspec.register_type("port", coerce_port)
```

Then in your spec:

```toml
[pipeline.args]
config = {type = "json-file"}
port   = {type = "port", default = 8080}
```

The coercer is called during `parse()` after validation passes.

---

## Logging integration

When `[config.logging]` is present in your `runspec.toml`, `parse()`
configures stdlib logging automatically and auto-injects a `--log-level`
argument. Just use `logger = logging.getLogger(__name__)` — no extra setup,
no `runspec` imports beyond `parse()`.

```python
import logging
from runspec import parse

logger = logging.getLogger(__name__)

def main():
    args = parse()
    logger.info("Deploy starting for %s", args.target)
    logger.info("Result", extra={"target": args.target, "duration_ms": 1240})
```

Sensitive-data redaction (passwords, tokens, `Authorization` headers, URL
credentials) is applied to every log line — console and file. See
[Logging](logging.md) for the full picture.

---

## Errors

All runspec exceptions inherit from `RunSpecError`:

```python
from runspec.errors import (
    RunSpecError,       # base class
    MissingRequiredArg,
    InvalidChoice,
    OutOfRange,
    UnknownArg,
    GroupViolation,
    AutonomyViolation,
)
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

Catch the base class to handle all runspec errors uniformly:

```python
try:
    args = runspec.parse()
except runspec.errors.RunSpecError as e:
    print(e)
    raise SystemExit(1)
```

---

## Complete example

```toml
# mypkg/runspec.toml
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

```python
# mypkg/process.py
import json
import logging
from runspec import parse

logger = logging.getLogger(__name__)


def main():
    args = parse()

    logger.info("Run starting", extra={
        "format": str(args.format),
        "workers": int(args.workers),
        "tags": list(args.tag),
    })

    if args.dry_run:
        logger.info("Dry run — no writes")
        if args.runspec_agent:
            print(json.dumps({"status": "dry-run", "input": str(args.input)}))
        else:
            print(f"[dry run] would process {args.input}")
        return

    for i in range(args.workers):
        chunk = load_chunk(args.input, i, args.workers)
        process(chunk, format=str(args.format))

    if args.runspec_agent:
        print(json.dumps({"status": "ok", "tags": list(args.tag)}))
    else:
        if args.verbose:
            print(f"Ran as: {args.runspec_runnable} "
                  f"(autonomy={args.runspec_autonomy})")
```
