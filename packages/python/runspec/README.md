# runspec

A language-agnostic, TOML-based interface specification for anything runnable:
scripts, applications and MCP tools, readable by people and AI agents without
conversion.

This is the Python package (the reference implementation). The Node pack is
[`runspec-node`](https://www.npmjs.com/package/runspec-node).
Full documentation is at **[runspec.app](https://runspec.app)**.

## Why

Argument definitions are usually buried in code. When an AI agent needs to call a
script as a tool, the interface gets described a second time in a separate tool
or skills file.

An interface definition already *is* a tool description. runspec puts it in one
`runspec.toml` next to your code, and that one file drives argument parsing and
validation, `--help`, agent tool schemas, forms, and how much an agent may do
without asking (autonomy).

## Install

```bash
pip install runspec
```

Python 3.10+. No runtime dependencies on Python 3.11+ (`tomli` on 3.10).

## Quick start

Describe the runnable in `mypkg/runspec.toml`, inside your package directory:

```toml
[greet]
description = "Greet someone from the command line"
autonomy    = "autonomous"

[greet.args]
name  = {type = "str"}
loud  = {default = false}
times = {default = 1}
```

Read the arguments in your code:

```python
from runspec import parse

def main():
    args = parse()
    message = f"Hello, {args.name.value}!"
    if args.loud.value:
        message = message.upper()
    for _ in range(args.times.value):
        print(message)
```

Register `greet = "mypkg.greet:main"` as a console script (the entry point name
matches the runnable name), install the package, and it runs as a normal CLI:

```bash
greet --name Ada --loud --times 2
```

List every runspec runnable in the environment, or serve them to an agent over
MCP:

```bash
runspec local
runspec serve
```

## Features

- **Spec first**: write the interface before the implementation.
- **One call**: `parse()` finds the spec, validates the arguments and returns
  them typed.
- **Self-describing values**: each argument carries its value plus its type,
  default, options and where the value came from (CLI, environment or default).
- **Inference**: type, required and choice are inferred from defaults and
  options.
- **Autonomy control**: declare whether an agent may run a tool freely, must ask
  first, or must leave it to a person.
- **Agent discovery**: `runspec local` and `runspec serve` expose installed
  runnables as tools.
- **Run logging and audit** on by default.
- **Language agnostic**: the same format for Python, Node, shell or anything else.

## Links

- [Documentation](https://runspec.app)
- [Format specification](https://github.com/runspec-app/runspec/blob/main/spec/SPEC.md)
- [Changelog](https://github.com/runspec-app/runspec/blob/main/CHANGELOG.md)
- [Source](https://github.com/runspec-app/runspec)
- [runspec-console](https://pypi.org/project/runspec-console/), the desktop console for runspec runnables

## License

MIT
