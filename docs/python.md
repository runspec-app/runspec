# The Python tongue

*Act II · Learning the Craft — runspec spoken in Python.*

The reference implementation: parse, read `.value`, and learn where each value came from.

The Python library is the reference implementation. Install it, call
`parse()`, and you get a fully validated, type-coerced `RunSpec` object back.

!!! info "Version"
    This page tracks the latest published **runspec** release; API additions
    are annotated with the version that introduced them. Python 3.10+ is
    supported. Python 3.11+ has **zero
    runtime dependencies**; on 3.10, `tomli` is the only dependency (used as
    the `tomllib` backport).

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
   injects `--debug` (see [Logging](logging.md)).
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
| `InvalidPattern` | String value didn't fully match declared `pattern` |
| `InvalidLength` | String value outside declared `min-length`/`max-length` |
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
    assert args.name.value == "Alice"
    assert args.loud.value is True
```

---

## RunSpec

`parse()` returns a `RunSpec` — an argument namespace with full spec
metadata. Hyphens in arg names become underscores.

```python
args = runspec.parse()

print(args.name.value)        # str
print(args.workers.value)     # int
print(args.input_dir.value)   # pathlib.Path
print(args.format.value)      # str, one of declared options
```

Each `args.<name>` is an [`Arg`](#arg) — read `.value` for the coerced native
value (see [Arg](#arg)). Hyphens in arg names become underscores
(`args.dry_run` for `dry-run`).

### Safe access across subcommands — `in` and `get()`

The arg set is scoped to the active subcommand path (the runnable's globals
plus the chosen command's own args). An arg declared only on a *different*
subcommand is not present, and reading it as an attribute raises
`AttributeError`. Use `in` to test membership, or `get()` for a value that may
not apply to this invocation — neither requires an up-front guard:

```python
args = runspec.parse()

if "symbol" in args:
    do_something(args.symbol.value)

# get() always returns an Arg, so .value is always safe — it yields the
# default (None unless given) when the arg isn't in the active command path:
since = args.get("since").value            # None if not applicable here
symbol = args.get("symbol", "ABC123").value # default when absent
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

if args.delete.value:
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
    print(json.dumps({"status": "deployed", "env": args.env.value}))
else:
    print(f"✓ Deployed to {args.env.value}")
```

---

## Arg

Every argument is an `Arg` — a value plus its full spec metadata. Read
`.value` for the coerced native value:

```python
# .value is the native Python value, already coerced
total  = args.batch_size.value * args.workers.value   # int * int
scaled = args.quality.value / 100                      # int / int → float

if args.dry_run.value:                    # flag arg → bool
    print("Dry run — no writes")

print(f"Format: {args.format.value!r}")
print(f"Quality: {args.quality.value:03d}")

# multiple=true args return a list
for tag in args.tag.value:
    print(tag)

for i in range(args.workers.value):
    ...

# path args are pathlib.Path
for file in args.input_dir.value.glob("*.csv"):
    ...
if args.output.value.is_dir():
    ...

# .value slots straight into common APIs
with open(args.input_path.value) as f:
    data = f.read()
shutil.copy(args.input_path.value, args.output_path.value)
first_tag = args.tag.value[0]
```

!!! warning "`Arg` is not a transparent proxy (since 0.38.0)"
    `Arg` deliberately does **not** behave as its value. `args.x + 1`,
    `int(args.x)`, `for t in args.x`, `open(args.x)`, and `args.x.is_dir()`
    all raise — and `if args.x:` and `args.x == "y"` raise a `TypeError`
    telling you to add `.value` (those two are the cases where a forgotten
    `.value` would otherwise be *silently* wrong: every object is truthy, and
    identity-equality against a native value is always `False`). Always read
    `.value`.

    Before 0.38.0 `Arg` proxied ~30 operators to masquerade as its value, but
    the proxy leaked — `args.x is None` never matched an unset optional,
    `isinstance(args.x, str)` was `False`, and the value would not
    JSON-serialise — so `.value` was needed "sometimes but not always". It is
    now always required, and an unset optional is honestly `None` via
    `.value`.

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
| `pattern` | `str \| None` | Regex each value must fully match (`str` only) |
| `min_length` | `int \| None` | Minimum string length (`str` only) |
| `max_length` | `int \| None` | Maximum string length (`str` only) |
| `multiple` | `bool` | Whether the arg accepts multiple values |
| `delimiter` | `str \| None` | Split character for delimiter-separated values |
| `short` | `str \| None` | Short flag alias |
| `position` | `int \| None` | 1-based positional index if positional |
| `env` | `str \| None` | Environment variable name |
| `deprecated` | `str \| None` | Deprecation message |
| `autonomy` | `str \| None` | Per-arg autonomy override |
| `discoverable` | `bool \| list \| None` | Per-arg visitor-facing audiences (self-service forms) |
| `ui` | `str \| None` | Form control hint |
| `hint` | `str \| None` | UI placeholder/hint text (`str`/`path`/`password`) |
| `meta` | `dict \| None` | Developer-defined pass-through metadata |
| `source` | `str` | Provenance of `value` — see [Value provenance](#value-provenance-source) |

```python
print(args.format.options)      # ['json', 'csv', 'parquet']
print(args.quality.range)       # (1, 100)
print(args.api_key.env)         # 'PIPELINE_API_KEY'
print(args.name.source)         # 'cli' | 'env' | 'runspec_env' | 'spec_default' | 'not_set'
```

### Value provenance — `source`

Every `Arg` records **where its value came from** in `arg.source`. The parser
resolves each arg through a fixed tier order and stamps `source` with the tier
that supplied the value — so a runnable can react to provenance (warn when a
secret was typed on the command line, distinguish "explicitly set" from
"defaulted", or audit exactly how an invocation was configured) without
re-deriving any of it. `source` is one of five strings:

| `source` | The value came from | Tier |
|---|---|---|
| `"cli"` | The command line (argv), or an interactive password prompt | 1 |
| `"env"` | A process environment variable — the automatic `RUNSPEC_<RUNNABLE>_ARG_<NAME>` key, or a declared `env =` alias | 2 |
| `"runspec_env"` | A `.runspec_env` file loaded at parse time (same keys as `"env"`, but read from the file, not the live environment) | 2 |
| `"spec_default"` | The arg's `default =` in `runspec.toml` | 3 |
| `"not_set"` | No value from any tier — the arg is unset (its `.value` is `None`) | — |

Resolution is **cli → env / runspec_env → spec_default → not_set**: the first
tier to supply a non-`None` value wins, and `source` names that tier.

```python
args = runspec.parse()

# Refuse a secret typed on the command line (it leaks into shell history / ps):
if args.api_key.source == "cli":
    raise SystemExit("✗ Pass the key via PIPELINE_API_KEY, not the command line")

# Tell 'caller chose this value' apart from 'fell back to the default':
if args.workers.source == "spec_default":
    logger.info("workers not specified — using default %d", args.workers.value)

# Audit how every arg was resolved:
for name, arg in args._args.items():
    logger.info("%s = %r (from %s)", name, arg.value, arg.source)
```

!!! note "Python only"
    Per-arg metadata — `source`, `type`, `meta`, and the other `Arg` fields —
    is a feature of the Python `Arg` object. The Node pack returns **bare
    coerced values** (`args.name` is the string itself), so it has no per-arg
    provenance; invocation-level metadata (`runspec_command`, …) is still
    available there.

### `python_type` — effective value type

`type` is the **item** type — it drives coercion and, for a `multiple` arg, is
applied to *each* element. So a `multiple` arg's `value` is a **list**, but
`type` alone still reads as the scalar item type. `python_type` composes the two
into the effective Python type of `value`:

```python
# tags = {type = "str", multiple = true}
args.tags.type          # 'str'          — the item type (unchanged)
args.tags.value         # ['a', 'b']     — the parsed value is a list
args.tags.python_type   # 'list[str]'    — the effective type

args.quality.python_type   # 'int'
args.input.python_type     # 'Path'
```

!!! tip "Precise editor types"
    `python_type` is a runtime/debug aid — to a static type checker `args.tags`
    is still `Any` (see [`parse()`](#parse)). Per-argument **editor** type hints
    (`args.tags: list[str]`) are slated for a generated `.pyi` via
    `runspec emit --stubs`; see the
    [stubs design](https://github.com/JasonFinestone/runspec/blob/main/docs/design/emit-stubs.md).
    Not yet implemented.

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
configures stdlib logging automatically and auto-injects a `--debug` flag.
Just use `logger = logging.getLogger(__name__)` — no extra setup, no
`runspec` imports beyond `parse()`.

```python
import logging
from runspec import parse

logger = logging.getLogger(__name__)

def main():
    args = parse()
    logger.info("Deploy starting for %s", args.target.value)
    logger.info("Result", extra={"target": args.target.value, "duration_ms": 1240})
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
        "format": args.format.value,
        "workers": args.workers.value,
        "tags": args.tag.value,
    })

    if args.dry_run.value:
        logger.info("Dry run — no writes")
        if args.runspec_agent:
            print(json.dumps({"status": "dry-run", "input": str(args.input.value)}))
        else:
            print(f"[dry run] would process {args.input.value}")
        return

    for i in range(args.workers.value):
        chunk = load_chunk(args.input.value, i, args.workers.value)
        process(chunk, format=args.format.value)

    if args.runspec_agent:
        print(json.dumps({"status": "ok", "tags": args.tag.value}))
    else:
        if args.verbose.value:
            print(f"Ran as: {args.runspec_runnable} "
                  f"(autonomy={args.runspec_autonomy})")
```

---

→ **Next:** [The Node tongue](node.md) — the same craft in Node.
