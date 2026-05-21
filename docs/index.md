# runspec

**Write `runspec.toml`. Get a real CLI. Your CLI is also an agent tool.**

A single TOML interface specification for anything runnable. Define your
arguments once, get a fully validated CLI with `--help`, structured logging,
and MCP-ready tool schemas — without writing argument-parsing code.

<small>
runspec 0.11.0 (Python) &middot; runspec-node 0.10.0
</small>

---

## Install

=== "Python"

    ```bash
    pip install runspec
    ```

    Requires Python 3.10+. Zero runtime dependencies on Python 3.11+; on 3.10
    the only dependency is the `tomli` backport.

=== "Node"

    ```bash
    npm install runspec-node
    ```

    Requires Node 18+. One runtime dependency: `smol-toml` (TOML parsing).

---

## Define your interface

Put `runspec.toml` inside your package directory, alongside the code it
describes. The same file works for both languages:

```toml
# greet/runspec.toml
[greet]
description = "Greet someone from the command line"
autonomy    = "autonomous"

[greet.args]
name  = {type = "str"}
loud  = {default = false}
times = {default = 1}
```

The section name `[greet]` is what users type on the command line. Your entry
point name must match — that's how runspec links the spec to your code.

## Use it in your runnable

=== "Python"

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

=== "Node"

    ```typescript
    import { parse } from 'runspec-node';

    function main(): void {
      const args = parse();
      let message = `Hello, ${args.name}!`;
      if (args.loud) message = message.toUpperCase();
      for (let i = 0; i < (args.times as number); i++) console.log(message);
    }

    main();
    ```

That's it. runspec handles the rest.

## What you get

For developers building a CLI:

- **`--help` generated** from your spec — no help text written
- **Type coercion** — args arrive as native types, no casting
- **Validation** — missing required args, bad choices, out-of-range values
  surface as clean, human-first errors
- **Inference** — `type` and `required` are inferred from defaults and
  `options` where possible
- **Subcommands and groups** — exclusive, inclusive, at-least-one,
  exactly-one, conditional
- **Built-in `--debug` flag, file rotation, sensitive-data redaction** — just
  add `[config.logging]`; see [Logging](logging.md)
- **`runspec init`** scaffolds a complete project — `pyproject.toml`,
  package layout, a working code stub

For agent tooling (free):

- **MCP / OpenAI / Anthropic tool schemas** — `runspec local --format mcp`
- **Live MCP stdio server** — `runspec serve` exposes every installed
  runnable as an agent tool
- **Autonomy control** — declare `autonomous` / `confirm` / `supervised` /
  `manual` per runnable or per argument
- **Remote execution** — `runspec jump` runs tools on `[config.jump-hosts]`
  over SSH+MCP; see [Jump Hosts](jump-hosts.md)

---

## Built with runspec

Both `runspec` and `runspec-node` define their own CLI in `runspec.toml`. The
help output you get from `runspec --help`, the argument validation, the
schemas at `runspec local --format mcp` — all of it goes through the same
parser your CLI uses. The bundled specs live at:

- `packages/python/runspec/runspec/runspec.toml`
- `packages/node/src/runspec.toml`

See [CLI Reference](cli.md) for the full command surface and a walkthrough of
the dogfooding.

---

## Next

- [Quickstart](quickstart.md) — zero to a working CLI in five minutes
- [Format Reference](format.md) — every field, every option
- [Python Library](python.md) — `parse()`, `RunSpec`, `Arg`, custom types
- [Node Library](node.md) — `parse()`, `ParsedArgs`, `getLogger`, custom types
- [CLI Reference](cli.md) — `runspec init`, `local`, `serve`, `jump`
- [Logging](logging.md) — `[config.logging]`, rotation, redaction, extras
- [Agent Integration](agents.md) — MCP schemas, autonomy, agent-aware output
- [Jump Hosts](jump-hosts.md) — remote execution over SSH+MCP
