# runspec

> A language-agnostic, TOML-based interface specification for anything runnable —
> scripts, applications, and MCP tools — readable by humans and AI agents without conversion.

[![Python](https://img.shields.io/pypi/v/runspec?label=pip%20install%20runspec)](https://pypi.org/project/runspec)
[![Node](https://img.shields.io/npm/v/runspec-node?label=npm%20install%20runspec-node)](https://www.npmjs.com/package/runspec-node)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/JasonFinestone/runspec/actions/workflows/python.yml/badge.svg)](https://github.com/JasonFinestone/runspec/actions)
[![Docs](https://img.shields.io/badge/docs-mkdocs-blue)](https://JasonFinestone.github.io/runspec)

---

## The Problem

Argument definitions are buried in code. `argparse` configs require reading Python.
Click and Typer decorators are better but still code-first. When AI agents need to
invoke a script as a tool, the interface has to be re-described in a separate skills
file — duplicating what already exists.

**The insight:** an interface definition *is* a skill description. If you know something
accepts `--input-file`, `--model`, and `--output-format [json|csv]`, you already know
how to invoke it as an agent tool, render it as a form, or generate an implementation.

## The Solution

A single `runspec.toml` that lives inside your package directory alongside your code —
the single source of truth for documentation, validation, agent tool schemas,
form rendering, and autonomy policy.

## Quick Start

Python:

```bash
pip install runspec
```

Node:

```bash
npm install runspec-node
```

Add your runnable's interface to `mypkg/runspec.toml`:

```toml
[greet]
description = "Greet someone from the command line"
autonomy    = "autonomous"

[greet.args]
name  = {type = "str"}
loud  = {default = false}
times = {default = 1}
```

Use it in your runnable (Python):

```python
from runspec import parse

def main():
    args = parse()
    message = f"Hello, {args.name}!"
    if args.loud:
        message = message.upper()
    for _ in range(args.times):
        print(message)
```

Or in Node/TypeScript:

```typescript
import { parse } from 'runspec-node';

function main() {
  const args = parse();
  let message = `Hello, ${args.name}!`;
  if (args.loud) message = message.toUpperCase();
  for (let i = 0; i < (args.times as number); i++) console.log(message);
}
main();
```

Make it agent-discoverable:

```bash
runspec local --format mcp
```

That's it. Your runnable is now a typed, validated, agent-callable tool.

---

## Features

- **Spec first** — write the interface before the implementation
- **Zero boilerplate** — one import, one `parse()` call
- **Rich return object** — every arg carries its full metadata alongside its value
- **Inference over declaration** — type, required, and choice inferred from context
- **Enforced priority stack** — CLI → env → config → default, automatically
- **Autonomy control** — declare whether agents can run freely, need confirmation, or must ask
- **Form rendering** — spec maps directly to MCP chat-native forms
- **Agent discovery** — `runspec local` finds all runspec-aware tools in the environment
- **Language agnostic** — same format for Python, Node, shell, or any language

## Repository Structure

This is a mono-repo containing all official runspec language packs:

| Package | Install | Status |
|---|---|---|
| `runspec` (Python) | `pip install runspec` | Active — 0.11.0 on PyPI |
| `runspec-node` | `npm install runspec-node` | Active — 0.10.0 on npm |
| `runspec-go` | `go get github.com/JasonFinestone/runspec/go` | Planned |

## Documentation

- [Documentation site](https://JasonFinestone.github.io/runspec) — full guides for CLI developers and agent integrators
- [Format Specification](spec/SPEC.md) — canonical reference
- [Changelog](CHANGELOG.md)
- [Design Document](DESIGN.md)
- [Contributing](CONTRIBUTING.md)

## License

MIT — see [LICENSE](LICENSE)
