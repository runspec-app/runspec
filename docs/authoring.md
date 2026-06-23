# Carving your first path

*Act I · Into the Forest — cutting a trail that lasts.*

The quickstart showed the way; here you carve a runnable to last — args, types, autonomy, and a test that proves it.

This is the end-to-end guide to adding a runnable to a project: the `runspec.toml`
section, the code stub, the entry point, validation, and a test. It is the same
guidance the `add-runnable` Claude Code skill follows, so a human and an agent
build runnables the same way.

If you are migrating an existing `argparse` script, read
[Migrating from argparse](migration.md) first — it maps each `add_argument` onto
its TOML equivalent. This page assumes you are starting fresh.

!!! tip "Use it as an agent skill"
    The raw skill file is published here too — fetch it on any machine, no repo
    access needed, and drop it in as a Claude Code skill:

    ```bash
    curl -O https://runspec.app/skills/add-runnable.md
    ```

---

## Hard rules — do not deviate

1. **`runspec.toml` lives inside the package directory** (`mypkg/runspec.toml`),
   never at the project root. The package directory is the one that holds
   `__init__.py` (Python) or `index.ts` / `index.js` (Node).
2. **Each runnable is a top-level TOML section** — `[name]`. Never
   `[runnables.name]` or `[scripts.name]`.
3. **`[config]` is reserved** — never use it as a runnable name.
4. **The entry point name must exactly match the runnable name.** `runspec local`
   discovers runnables by matching an installed script name to a TOML section
   name; a mismatch makes the runnable invisible.
5. **Reinstall after editing `pyproject.toml` / `package.json`** —
   `pip install -e .` (Python) or `npm install` (Node).
6. **Autonomy defaults to `"confirm"`** — safe by default. Opt a runnable up to
   `"autonomous"` only when it is read-only or otherwise safe for an agent to run
   unattended.

---

## Fast path: `runspec init`

If `runspec` is installed, scaffold the skeleton rather than hand-writing every
file. From **inside the package directory**:

```bash
runspec init --name <name> --lang python      # also: typescript, javascript
```

This writes the `[name]` TOML section, a code stub, and — with `--write-project`
— a `pyproject.toml` whose `[project.scripts]` and `dev = ["pytest", "ruff"]`
extra are already wired. It also drops a placeholder typing stub and the
[`runspec stubs`](typed-args.md) wiring (mypy path + pre-commit hook), so your
editor types arguments precisely from the first run. Then edit the args and
`main()` to taste.

The sections below are the manual equivalent — use them to add a runnable to an
existing spec, or when you want full control.

---

## File location

```
mypkg/
  __init__.py
  runspec.toml      ← here, not at the project root
  myrule.py         ← the runnable's code
```

When you create a `runspec.toml` from scratch, put the schema comment on the
first line so editors validate and autocomplete it:

```toml
#:schema https://runspec.app/runspec.schema.json

[myrule]
description = "Do the thing"
```

---

## Arg types

| Type       | Notes                                                          |
|------------|----------------------------------------------------------------|
| `"str"`      | Text                                                         |
| `"int"`      | Integer                                                      |
| `"float"`    | Float                                                        |
| `"bool"`     | Boolean (`true`/`false`) — distinct from `"flag"`           |
| `"flag"`     | Boolean switch; presence = `true`; `default = false`        |
| `"path"`     | File/dir path coerced to `pathlib.Path` (Python); always required unless given a default |
| `"choice"`   | Requires `options = [...]`                                   |
| `"password"` | Secret; refused on the command line, read from env / prompt, omitted from agent schemas, redacted from logs |
| `"rest"`     | List of strings captured after a literal `--`; at most one per runnable; never required |

**Inference (so you can often omit `type`):** a default integer infers `"int"`,
a float `"float"`, a string `"str"`, `true`/`false` infers `"flag"`, and
`options = [...]` infers `"choice"`. No `default` (and not flag/rest) →
`required = true`; `type = "path"` with no default → required.

**Optional arg fields** (all on the inline table): `description`, `default`,
`required`, `options`, `range = [min, max]`, `pattern` (full-match regex, str
only), `min-length` / `max-length` (str only), `multiple = true`, `delimiter`,
`short = "-x"`, `env`, `position` (1-based → positional), `deprecated`, `hint`,
`meta` (a pass-through dict runspec never reads). The complete list lives in the
[Format Reference](format.md).

Inline-table style for args:

```toml
[myrule.args]
directory = {type = "path",   description = "Directory to process", default = "."}
dry_run   = {type = "flag",   description = "Preview only",         default = false}
format    = {type = "choice", description = "Output format",        options = ["text", "json"], default = "text"}
```

---

## Reading argument values: always `.value`

`parse()` returns a `RunSpec`. Each `args.<name>` is an **`Arg`**, not the raw
value — read `args.<name>.value` for the coerced native value:

```python
from runspec import parse

def main():
    args = parse()
    root = args.directory.value          # pathlib.Path
    if args.dry_run.value:               # bool
        ...
    print(args.format.value)             # str
```

Since 0.38.0 the `Arg` is **not** a transparent proxy. The two cases that used to
be silently wrong without `.value` now raise a guiding `TypeError`:

```python
if args.dry_run:          # TypeError — did you mean args.dry_run.value?
if args.format == "json": # TypeError — compare args.format.value instead
```

Arithmetic, `int()`, iteration, indexing, `os.fspath`, and attribute delegation
are absent too — there is no path to a silently-wrong read.

For args whose presence depends on the active subcommand, use `name in args` or
`args.get(name, default)` (which always returns an `Arg`) — no up-front guard
needed:

```python
if "force" in args and args.force.value:
    ...
limit = args.get("limit", default=None).value
```

**Node:** `parse()` returns **bare coerced values** (`args.directory` *is* the
string/number/array) — there is no `.value` and no per-arg metadata wrapper.

### The `Arg` also carries metadata (Python)

Beyond `.value`, each `Arg` is self-describing — it carries its full spec plus
**where the value came from**, so a runnable can introspect its own inputs
without re-deriving anything:

```python
args.workers.type      # 'int'        — declared type
args.workers.default   # 4            — spec default
args.format.options    # ['text', 'json']
args.api_key.env       # 'PIPELINE_API_KEY'
args.workers.source    # provenance — see below
```

**`source` is the value's provenance** — one of `"cli"`, `"env"`,
`"runspec_env"`, `"spec_default"`, or `"not_set"` (resolution order
cli → env → spec_default → not_set). Use it to react to *how* a value arrived:

```python
# Refuse a secret typed on the command line (leaks into shell history / ps):
if args.api_key.source == "cli":
    raise SystemExit("✗ Pass the key via PIPELINE_API_KEY, not the command line")

# Tell 'caller chose this' apart from 'fell back to the default':
if args.workers.source == "spec_default":
    logger.info("workers not set — using default %d", args.workers.value)
```

Full field list and the provenance table: [Python Library → Arg](python.md#arg).
(Per-arg metadata is Python-only; Node returns bare values.)

---

## Typed `parse()` with `runspec stubs`

`Arg.value` is typed `Any` by default, so a typo (`args.wrokers.value`) sails
through the type checker. Run [`runspec stubs`](typed-args.md) once your package
is installed and `parse("myrule")` becomes precisely typed at the call site —
no annotation, no cast:

```python
args = parse("deploy")
args.workers.value     # int
args.env.value         # Literal["dev", "prod"]
args.wrokers           # error: "_DeployArgs" has no attribute "wrokers"
```

```bash
runspec stubs          # generate types from the installed runnables
runspec stubs --check  # CI / pre-commit: fail if missing or stale
```

It writes one committed artifact — `typings/runspec/__init__.pyi` — regenerated
whenever a `runspec.toml` changes. pyright / Pylance read `typings/`
automatically; for mypy add `mypy_path = "typings"`. A stale stub never affects
runtime (`parse()` reads the `runspec.toml`, not the stub) — the worst it does is
show an out-of-date editor hint. Full guide: [Typed args](typed-args.md).

---

## Subcommands (optional)

A runnable can branch into subcommands under `[name.commands.<sub>]`. Args
declared at the runnable level are shared by every subcommand; args under a
subcommand are local to it. Set `require-command = true` to force the caller to
pick a subcommand (no bare invocation).

```toml
[db]
description = "Database operations"
require-command = true

[db.commands.migrate]
description = "Run pending migrations"

[db.commands.migrate.args]
steps = {type = "int", description = "How many migrations to apply", default = 1}
```

Inside `main()`, `args.runspec_command` is the chosen subcommand name.

---

## Autonomy

| Level | Use for |
|---|---|
| `"confirm"` (default) | Destructive ops — the agent presents intent, awaits human approval |
| `"autonomous"` | Read-only / safe ops — the agent runs freely |
| `"supervised"` | The agent runs, a human reviews output before acting on it |
| `"manual"` | Human only — the agent cannot invoke it |

For a **destructive flag** on an otherwise-safe runnable, gate it in code so an
agent can't trigger it without declared autonomy (a human typing the flag on the
CLI is their own confirmation):

```python
if args.runspec_agent and args.runspec_autonomy != "autonomous":
    raise SystemExit("✗ --delete requires autonomy='autonomous' for agent invocation")
```

---

## Code stub

Keep `main()` thin: `parse()` → a plain function that does the work → render the
result. The plain function is directly unit-testable with ordinary values (see
[Testing runnables](testing.md)).

=== "Python"

    ```python
    from runspec import parse

    def do_work(directory, *, dry_run=False):   # plain values — testable
        ...
        return {"ok": True}

    def main():
        args = parse()
        result = do_work(args.directory.value, dry_run=args.dry_run.value)
        print(result)
    ```

    `pyproject.toml` entry — the script name **must** match the runnable name:

    ```toml
    [project.scripts]
    myrule = "mypkg.myrule:main"
    ```

=== "Node / TypeScript"

    ```typescript
    import { parse } from "runspec-node";

    const args = parse();
    console.log(args.directory);   // bare coerced value — Node has no .value
    ```

    `package.json` entry:

    ```json
    "bin": { "myrule": "dist/myrule.js" }
    ```

---

## Error handling — let it propagate, don't wrap

**Do not** wrap `parse()` or the body of `main()` in a blanket `try/except` that
prints the error and calls `sys.exit(1)`. runspec already does this, better:

- **Argument / config errors are handled by `parse()` itself** — always, with or
  without `[config.logging]`. Bad args, missing required args, bad choices,
  unknown runnable, missing config: runspec prints a clean human message and
  exits 1. Wrapping `parse()` adds nothing.
- **Uncaught runtime exceptions in your own logic are handled too — when
  `[config.logging]` is declared.** runspec installs a process-level handler
  (Python `sys.excepthook`, Node `uncaughtException` / `unhandledRejection`) that
  writes a structured audit record plus a concise stderr line
  (`ERROR: ValueError: ...`), or a full traceback under `--debug`. **Just let the
  exception propagate.**

```python
from runspec import parse

def main():
    args = parse()                 # arg/config errors already exit cleanly
    result = do_the_work(args)     # let any exception propagate — runspec logs + renders it
    print(result)
```

The few **legitimate** reasons to raise or exit explicitly:

- **Intentional control flow with a clear message** — e.g. the destructive-flag
  autonomy gate above (`raise SystemExit("✗ --delete requires …")`).
- **Catching one *specific*, expected exception** to add a remediation hint, then
  re-raising or `raise SystemExit(msg)`. Catch the narrow type
  (`except ConnectionError`), never bare `except Exception`.

A blanket `try/except Exception: print(e); sys.exit(1)` throws away the structured
audit record and the traceback, hides the exception type, and duplicates what
runspec does. Adding a `[config.logging]` table is the right move — see
[Logging](logging.md).

---

## Testing the runnable

runspec ships a stdlib-only test harness that pulls in no test framework, so
importing it is free. `parse()` already accepts `argv=` and `config_path=`, so
you never monkeypatch `sys.argv` or mock the parser. There are two layers worth
testing:

1. **Wiring / parsing — no mocks.** Does each (sub)command accept its args,
   coerce types, and enforce required args / `require-command`?
2. **The actual work — mock only the I/O boundary.** Keep `main()` thin, then
   test the plain function directly and patch its single external call.

`ParseHarness` covers layer 1:

```python
# tests/test_myrule.py  — lives in the package's tests/ dir
from pathlib import Path
from runspec.testing import ParseHarness

SPEC = Path(__file__).parent.parent / "runspec.toml"   # adjust to your layout

def test_wiring():
    h = ParseHarness(script_name="myrule", config_path=SPEC)
    spec = h.expect_ok(["--directory", "."])
    assert spec.directory.value == "."
    h.expect_exit(["--format", "nope"], contains="--format")  # bad choice rejected
    h.smoke(values={"directory": "."})                        # walk the whole tree
    h.close()
```

- `expect_ok(argv)` returns the parsed `RunSpec`; fails the test if it exits.
- `expect_exit(argv, code=1, contains=...)` asserts a clean error exit;
  `contains` checks the message names the offending flag. Use `code=0` for
  `--help`.
- `smoke(values={...})` auto-covers the whole command tree: `--help` exits 0,
  `require-command` is enforced, omitting required args exits 1, and a
  fully-populated invocation parses.

Pass an inline spec with `toml="..."` instead of `config_path=` for small cases.
**Node:** `import { ParseHarness } from "runspec-node/testing"` — same API
(`expectOk`, `expectExit({ code, contains })`, `smoke({ values })`).

The full testing guide — Layer 2 mocking, the `runspec test` CI gate, and the
`no_run_as_check()` escape hatch for driving `main()` directly — is on
[Testing runnables](testing.md).

### `runspec test` — the CI gate

`runspec test` smoke-tests **every installed runnable**: an in-process spec smoke
(the `ParseHarness.smoke` checks above) plus a subprocess that runs each
installed entry point with `--help` — proving the runnable's own code imports and
is wired to the right spec. It exits 1 if any runnable fails, so it drops
straight into CI.

```bash
runspec test                  # all runnables
runspec test --runnable myrule
runspec test --format json
```

---

## Steps

1. **Find the package directory** — locate `runspec.toml` or the source dir with
   `__init__.py` / `index.ts`. (Or run `runspec init` from inside it for the
   skeleton.)
2. **Add the runnable section** to `runspec.toml`. If creating the file fresh,
   add the schema comment first: `#:schema https://runspec.app/runspec.schema.json`.
3. **Create the code stub** in the package directory. Keep `main()` thin —
   delegate the real work to a plain function so it's directly testable.
4. **Wire the entry point** in `pyproject.toml` or `package.json` — name must
   match the runnable.
5. **Add a test** under the package's `tests/` dir using `ParseHarness` (at least
   a `smoke()` call; assert on key arg coercion / validation).
6. **Reinstall** — `pip install -e .` or `npm install`.
7. **Generate stubs** (Python) — `runspec stubs`, then commit `typings/`.
8. **Validate** — run `runspec local` (confirm the runnable appears and is
   callable), then `runspec test` (confirm it passes the wiring gate), then
   `pytest` for your own tests.

---

→ **Next:** [The field guide](format.md) — every field and type, for when you need to look one up.
