# Quickstart

Zero to a working CLI in five minutes. The TOML format is identical across
Python and Node — only the install step and the import differ.

---

## 1. Install

=== "Python"

    ```bash
    pip install runspec
    ```

=== "Node"

    ```bash
    npm install runspec-node
    ```

---

## 2. Scaffold a project

`runspec init` creates `runspec.toml` and a working code stub. The Python
CLI also has `--write-project` to lay down the surrounding project files
in one go.

=== "Python"

    ```bash
    mkdir greet && cd greet
    runspec init --name greet --write-project
    ```

    You get:

    ```
    .                      ← parent dir (cwd before --write-project)
    ├── pyproject.toml     ← entry point already wired up
    ├── .gitignore
    ├── CLAUDE.md
    └── greet/
        ├── __init__.py
        ├── runspec.toml   ← lives inside the package, not at the project root
        └── greet.py       ← parse() call ready to go
    ```

    `--write-project` writes `pyproject.toml` to the parent directory (use
    `--project-dir` to override). Add `--example` to scaffold worked
    `clean` + `scan` runnables alongside, with confirmation prompts and
    conditional deletion you can read for ideas.

=== "Node"

    ```bash
    mkdir greet && cd greet
    npm init -y
    npm install runspec-node
    npx runspec init --name greet
    ```

    You get:

    ```
    greet/
    ├── package.json
    ├── runspec.toml       ← the interface
    └── greet.ts           ← parse() call ready to go
    ```

    Add the `bin` entry to `package.json` so the runnable is on `PATH` after
    `npm install`:

    ```json
    {
      "bin": { "greet": "./dist/greet.js" }
    }
    ```

    Compile (`tsc` or whatever your build is) so `dist/greet.js` exists.

---

## 3. Fill in the logic

The init scaffold already wires up `parse()`. Just write your runnable.

=== "Python"

    ```python
    # greet/greet.py
    from runspec import parse


    def main():
        args = parse()
        message = f"Hello, {args.name.value}!"
        if args.loud.value:
            message = message.upper()
        for _ in range(args.times.value):
            print(message)


    if __name__ == "__main__":
        main()
    ```

=== "Node"

    ```typescript
    // greet.ts
    import { parse } from 'runspec-node';

    function main(): void {
      const args = parse();
      let message = `Hello, ${args.name as string}!`;
      if (args.loud) message = message.toUpperCase();
      for (let i = 0; i < (args.times as number); i++) console.log(message);
    }

    main();
    ```

The interface itself is in `runspec.toml`:

```toml
[greet]
description = "Greet someone from the command line"
autonomy    = "autonomous"

[greet.args]
name  = {type = "str"}
loud  = {default = false}
times = {default = 1}
```

!!! note "Entry-point name must match"
    The runnable section name (`[greet]`) must match the binary name on
    `PATH`. Python uses `[project.scripts]` in `pyproject.toml`; Node uses
    `bin` in `package.json`. `runspec init` wires this up for you.

---

## 4. Install and run

=== "Python"

    ```bash
    pip install -e .
    greet --name Ada --loud --times 3
    ```

=== "Node"

    ```bash
    npm install
    npm run build      # if you have a build step
    greet --name Ada --loud --times 3
    ```

Either way:

```
HELLO, ADA!
HELLO, ADA!
HELLO, ADA!
```

`greet --help` works out of the box — no help text written:

```
Usage: greet --name <str> [--loud] [--times <int>]

Greet someone from the command line

Arguments:
  --name                 (str, required)
  --loud                 (flag, default: False)
  --times                (int, default: 1)

Autonomy: autonomous

  -h, --help    Show this message and exit
```

---

## 5. See what validation gives you

Missing required argument:

```
$ greet
✗  Missing required argument: --name
   Type: str
```

Wrong type:

```
$ greet --name Ada --times abc
✗  Cannot coerce value 'abc' to type 'int' for argument '--times':
   invalid literal for int() with base 10: 'abc'
```

Bad choice (with a fuzzy suggestion):

```
$ greet --name Ada --format yml
✗  Invalid value for --format: 'yml'
   Expected one of: json, csv, parquet
   Did you mean: json?
```

No argument parsing code. No error handling. Just your runnable and a TOML.

---

## 6. Try the worked examples

=== "Python"

    ```bash
    cd /tmp && mkdir runspec-sandbox && cd runspec-sandbox
    runspec init --example --write-project
    pip install -e .
    clean --help
    scan --help
    ```

`--example` scaffolds two runnables — `clean` and `scan` — that demonstrate
confirmation prompts, conditional deletion, autonomy escalation, and
agent-aware output. Read them as a learning sandbox.

---

## 7. See it as an agent tool

```bash
runspec local                  # list installed runnables, validate setup
runspec local --format mcp     # emit MCP tool schemas
runspec serve                  # start the live MCP stdio server
```

Wire `runspec serve` into Claude Desktop, Cursor, or any MCP host once —
every runspec-aware package installed in the environment is immediately
available as a tool. See [Agent Integration](agents.md) for the wiring.

---

## Next steps

- [Format Reference](format.md) — every field, every option
- [Logging](logging.md) — add `[config.logging]` and get rotation, JSON
  file logs, sensitive-data redaction, and structured `extra` fields
- [CLI Reference](cli.md) — `init`, `local`, `serve`, `jump` flag by flag
- [Python Library](python.md) / [Node Library](node.md) — full API reference
