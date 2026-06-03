# Migrating from argparse

If you have working `argparse` scripts, runspec replaces the boilerplate —
`ArgumentParser`, every `add_argument`, the manual `if`-checks that call
`parser.error(...)` — with a declarative `runspec.toml`. The interface moves
out of your code and into data: typed, validated, and emittable as an agent
tool schema. Your script body shrinks to `args = parse()` plus the logic that
was always the point.

This guide is a practical conversion path, argparse-first. Every example is a
valid runspec setup you can copy.

---

## A complete before / after

A realistic image-compression tool: an int with bounds, a choice, a flag, a
path positional, an env-var fallback, a short flag, and two subcommands.

=== "Before (argparse)"

    ```python
    # imgtool/imgtool.py
    import argparse
    import os


    def main():
        parser = argparse.ArgumentParser(
            prog="imgtool",
            description="Compress and convert images",
        )
        sub = parser.add_subparsers(dest="command", required=True)

        c = sub.add_parser("compress", help="Compress an image")
        c.add_argument("input")                          # positional path
        c.add_argument("--quality", type=int, default=85)
        c.add_argument("--format", choices=["jpeg", "png", "webp"],
                       default="jpeg")
        c.add_argument("-v", "--verbose", action="store_true")
        c.add_argument("--api-key", default=os.environ.get("IMGTOOL_API_KEY"))

        x = sub.add_parser("convert", help="Convert between formats")
        x.add_argument("input")
        x.add_argument("--to", choices=["jpeg", "png", "webp"], required=True)

        args = parser.parse_args()

        # Manual validation argparse can't express
        if args.command == "compress" and not (1 <= args.quality <= 100):
            parser.error("--quality must be between 1 and 100")

        if args.command == "compress":
            print(f"compress {args.input} q={args.quality} -> {args.format}")
        else:
            print(f"convert {args.input} -> {args.to}")


    if __name__ == "__main__":
        main()
    ```

=== "After (runspec.toml)"

    ```toml
    # imgtool/runspec.toml
    [imgtool]
    description = "Compress and convert images"
    autonomy    = "confirm"

    [imgtool.commands.compress]
    description = "Compress an image"

    [imgtool.commands.compress.args]
    input   = {type = "path", position = 1}
    quality = {default = 85, range = [1, 100]}
    format  = {options = ["jpeg", "png", "webp"], default = "jpeg"}
    verbose = {default = false, short = "-v"}
    api-key = {type = "str", env = "IMGTOOL_API_KEY"}

    [imgtool.commands.convert]
    description = "Convert between formats"

    [imgtool.commands.convert.args]
    input = {type = "path", position = 1}
    to    = {options = ["jpeg", "png", "webp"]}
    ```

=== "After (Python)"

    ```python
    # imgtool/imgtool.py
    from runspec import parse


    def main():
        args = parse()
        # args.quality is already an int in [1, 100]; args.input is an
        # absolute resolved path; the choice is already validated. The active
        # subcommand is args.runspec_command (argparse's dest="command").
        if args.runspec_command == "compress":
            print(f"compress {args.input} q={args.quality} -> {args.format}")
        else:
            print(f"convert {args.input} -> {args.to}")


    if __name__ == "__main__":
        main()
    ```

Everything `argparse` did by hand is now declarative:

- `type=int, default=85` collapses to `default = 85` — int inferred.
- The manual `1 <= quality <= 100` check becomes `range = [1, 100]`.
- `choices=[...]` becomes `options = [...]` — `type = "choice"` inferred.
- `required=True` on `--to` is inferred from *having no default*.
- `add_subparsers(required=True)` becomes two `[...commands.*]` sections.
- The `os.environ.get` fallback becomes `env = "IMGTOOL_API_KEY"`.

And `--help` is generated — you never wrote `help=` text:

```
$ imgtool compress --help
Usage: imgtool compress <input> [--quality <int>] [--format <choice>] [--verbose] [--api-key <str>]

Compress an image

Arguments:
  input                  (path, required)
  --quality              (int, default: 85)
  --format               (choice, default: jpeg)
  --verbose, -v          (flag, default: False)
  --api-key              (str)

  -h, --help    Show this message and exit
```

!!! note "Hyphens become underscores"
    A TOML arg named `api-key` or a flag like `--dry-run` is reached as
    `args.api_key` / `args.dry_run` in Python — exactly like argparse's
    `dest` normalisation.

---

## The mapping table

| argparse | runspec.toml |
|---|---|
| `add_argument('--name')` (string) | `name = {type = "str"}` |
| `add_argument('--quality', type=int, default=85)` | `quality = {default = 85}` (int inferred) |
| `add_argument('--rate', type=float)` | `rate = {type = "float"}` |
| `add_argument('--verbose', action='store_true')` | `verbose = {default = false}` (flag inferred) or `{type = "flag"}` |
| `add_argument('--format', choices=['json','csv'])` | `format = {options = ["json","csv"]}` (choice inferred) |
| `add_argument('--tag', action='append')` / `nargs='+'` | `tag = {type = "str", multiple = true}` |
| comma-separated single value | `{type = "str", multiple = true, delimiter = ","}` |
| `add_argument('input')` (positional) | `input = {type = "path", position = 1}` (1-based; no `position` = a `--flag`) |
| `add_argument('-v', '--verbose')` short alias | `{short = "-v"}` (`-h` is reserved) |
| `type=argparse.FileType` / path strings | `{type = "path"}` (coerced to an absolute resolved path) |
| manual `os.environ.get(...)` fallback | `{env = "MY_VAR"}` or `{env = ["A", "B"]}` (first set wins) |
| `required=True` | inferred when there's no default (and not a flag); or `{required = true}` |
| `add_subparsers()` | `[<runnable>.commands.<sub>]` sections |
| `add_subparsers(required=True)` | `require-command = true` on the runnable (0.23.0+) |
| manual range check + `parser.error` | `{range = [min, max]}` on a numeric arg |
| manual regex / length check | `{type = "str", pattern = "...", min-length = 3, max-length = 40}` |
| manual mutually-exclusive logic | a `groups` block (see below) |
| `args = parser.parse_args()`; `args.quality` (int) | `args = parse()`; `args.quality` (already coerced) |

!!! tip "Arg objects are transparent"
    `parse()` returns coerced, native values: `args.quality + 1` works,
    `args.format == "jpeg"` works, `for t in args.tag` works. There's no
    `.value` unwrapping needed for normal use — the `Arg` proxies through to
    the coerced value. (`.meta` and friends are still there when you reach
    for them.)

### What you can omit — inference rules

You rarely need to write `type`. runspec infers it, in this order:

| You wrote | runspec infers |
|---|---|
| `default = 42` | `type = "int"` |
| `default = 3.14` | `type = "float"` |
| `default = "text"` | `type = "str"` |
| `default = true` / `false` | `type = "flag"` |
| `options = [...]` | `type = "choice"` (wins over `default`) |
| *no default, no `required = false`* | `required = true` |
| `type = "path"` with no default | `required = true` |

So `quality = {default = 85, range = [1, 100]}` needs no `type`, and a string
arg with no default — `to = {options = [...]}` — is required automatically.

### Types available

`str` (with `pattern` / `min-length` / `max-length`), `int`, `float` (both
take `range`), `bool`, `flag` (presence = `true`), `path` (resolved to an
absolute path), `choice` (declares `options`), and `rest` (collects every
token after a literal `--`, for wrapping another command). Need something
domain-specific? Register a coercer:

```python
import runspec

runspec.register_type("port", lambda s: _valid_port(int(s)))
```

---

## Subcommands

`add_subparsers()` maps directly to `[<runnable>.commands.<name>]` sections,
each with its own `args`, `groups`, `description`, and `autonomy`. They nest,
so a subcommand can have its own `commands`.

=== "Before (argparse)"

    ```python
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("input")
    val = sub.add_parser("validate")
    val.add_argument("input")
    val.add_argument("--strict", action="store_true")
    ```

=== "After (runspec.toml)"

    ```toml
    [pipeline]
    description     = "Process data pipeline files"
    require-command = true        # argparse's add_subparsers(required=True)

    [pipeline.commands.run.args]
    input = {type = "path", position = 1}

    [pipeline.commands.validate.args]
    input  = {type = "path", position = 1}
    strict = {default = false}
    ```

Read the chosen subcommand from `args.runspec_command` (the leaf name, like
argparse's `dest`) or `args.runspec_command_path` (the full path for nested
commands). `require-command = true` (Python 0.23.0+, node-0.18.0+) makes the
runnable error out if no subcommand is given — the equivalent of
`add_subparsers(required=True)`. `runspec serve` flattens these into MCP tools
with underscore-joined names (`pipeline_run`, `pipeline_validate`).

---

## Validation you get to delete

The fiddly post-`parse_args` blocks that argparse can't express move into the
spec and run automatically. Validation is two-pass: every arg is coerced and
checked first, then groups.

**Per-arg checks** replace inline `if`/`parser.error`:

```toml
[deploy.args]
workers  = {default = 4, range = [1, 32]}                 # numeric bounds
jira-key = {type = "str", pattern = "[A-Z]+-[0-9]+"}      # full-match regex
slug     = {type = "str", min-length = 3, max-length = 40}
```

`pattern` is anchored on both ends (`re.fullmatch` semantics — no `^`/`$`
needed). `pattern` / `min-length` / `max-length` apply to `str` only.

**Group checks** replace mutually-exclusive and "you need one of these" logic.
This argparse pattern:

```python
if args.format and args.raw:
    parser.error("--format and --raw are mutually exclusive")
if not (args.input_file or args.input_dir):
    parser.error("provide --input-file or --input-dir")
```

becomes two declarative blocks:

```toml
[export.groups.output-format]
exclusive = true
args      = ["format", "raw"]

[export.groups.input]
at-least-one = true
args         = ["input-file", "input-dir"]
```

The full set is `exclusive`, `inclusive`, `at-least-one`, `exactly-one`, and a
conditional `if` / `requires` (if one arg is present, others become required).
See [Groups in the Format Reference](format.md#groups).

---

## Packaging & discovery

This is the step argparse never had, and the one most easily missed. Three
things must line up.

**1. Add the dependency and an entry point.** The entry-point name *must
match* the runnable section name in `runspec.toml`.

```toml
# pyproject.toml
[project]
dependencies = ["runspec"]

[project.scripts]
imgtool = "imgtool.imgtool:main"   # name "imgtool" == [imgtool] section
```

**2. Ship `runspec.toml` as package data.** It lives *inside* the package
directory (`imgtool/runspec.toml`), never at the project root — so build
backends include it automatically and `parse()` can find it after install.

```
imgtool/                 ← project root
├── pyproject.toml
└── imgtool/             ← the package
    ├── __init__.py
    ├── runspec.toml     ← here
    └── imgtool.py
```

**3. Install editable, then it's discoverable.**

```bash
pip install -e .
imgtool compress photo.jpg --quality 90
runspec local            # lists + validates every installed runspec package
runspec serve            # exposes them all as MCP tools
```

`runspec local` uses `importlib.metadata` only — it sees installed packages,
not loose files, which is why `pip install -e .` is the step that makes a
runnable visible.

!!! note "Starting clean?"
    `runspec init --name imgtool --write-project` scaffolds the whole layout
    above — `pyproject.toml` with the entry point wired, the package dir, a
    `runspec.toml`, and a `parse()` stub. See the
    [Quickstart](quickstart.md).

---

## What you get for free after migrating

Once the script is a runspec runnable, you also get, with no extra code:

- **Generated `--help`** from the spec — including subcommands and examples.
- **Human-first validation errors** with fuzzy suggestions
  (`Did you mean: json?`) instead of argparse's terse `usage:` dump.
- **Native-typed, validated args** — `parse()` hands you `int`, `float`,
  `path`, lists, already range/pattern-checked.
- **Structured logging** the moment you add `[config.logging]`: JSON audit
  file, rotation, redaction, `extra=` fields, and an auto-injected `--debug`
  flag. See [Logging](logging.md).
- **Agent tool schemas.** `runspec local --format mcp` emits MCP-shaped
  schemas (range/pattern map to native JSON Schema keywords); `runspec serve`
  runs a live MCP stdio server exposing every installed runnable. See
  [Agent Integration](agents.md).

The same TOML drives the CLI, the validation, the help text, the logs, and the
agent interface — define the interface once.

---

## Next steps

- [Format Reference](format.md) — every field and group type in full
- [Logging](logging.md) — `[config.logging]`, rotation, redaction, `--debug`
- [Agent Integration](agents.md) — `runspec serve` and MCP schemas
- [CLI Reference](cli.md) — `init`, `local`, `serve`, `jump`, `env`
