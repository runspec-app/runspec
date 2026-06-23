---
description: Add a new runspec runnable to the current project — TOML section, code stub, entry point, validation, and a test
---

Add a runnable to this project: **$ARGUMENTS**

> Published companion guide: <https://runspec.app/authoring/> (mirrors this skill;
> link the human there). Typed args: <https://runspec.app/typed-args/>. Testing:
> <https://runspec.app/testing/>. This file is also downloadable raw at
> <https://runspec.app/skills/add-runnable.md> (no repo access needed).

## First: survey the current project state

Before anything else, locate existing `runspec.toml` files with the **Glob** tool
(pattern `**/runspec.toml`). Ignore matches under `.venv/`, `node_modules/`, or
`site-packages/` — those belong to dependencies, not this project. **Read** the
nearest remaining one to match its existing format and conventions; if there are
none, you'll be creating the first (add the schema comment — see Steps). These
are built-in, cross-platform tools, so this works on any OS / shell (the earlier
`!`find …`` injections assumed a POSIX shell and broke on Windows without Git
Bash).

---

## Then: grill out the requirements (don't guess them)

A runnable *is* an interface, so a vague request yields a vague interface. If the
ask is thin, **interview the user before scaffolding** — and ground every
question in what you just surveyed, so you ask informed ones rather than generic
ones (a "grill, with docs" approach):

- the authoring + format reference — <https://runspec.app/authoring/>,
  <https://runspec.app/format/> — for the real arg types, fields, autonomy
  levels, and subcommand shape;
- the nearest existing `runspec.toml` — to match house naming, autonomy
  defaults, and layout.

Grill until each of these is pinned down — ask 2–4 at a time with
`AskUserQuestion`, and turn vague answers concrete ("fast" → what budget?
"handle errors" → which, and do what?):

- **Purpose & success** — what it does and what a correct run produces (one line
  fit for `description`).
- **Name** — the runnable / entry-point name (they must match).
- **Args** — for each input: name, type, required-vs-default, options/range,
  short flag, env var. Push for the *minimal* set, not every possible knob.
- **Subcommands** — one action or several? If several, is a bare invocation
  allowed, or is `require-command` set?
- **Side effects & autonomy** — read-only, or does it change something? That
  decides `autonomy` and whether a destructive flag needs an in-code agent gate.
- **Failure modes** — what can go wrong, and what should happen (let it
  propagate, or catch one specific case for a remediation hint).
- **Verification** — how we'll know it works (the test's key assertions).

Stop when the unknowns left are low-risk, then **play it back as a one-paragraph
spec** — name, description, the arg table, autonomy, subcommands — and get a yes
before scaffolding. If the request already spells all this out, skip the
interview and just confirm the few genuine gaps; don't manufacture questions.

---

## Hard rules — do not deviate

1. **runspec.toml lives inside the package directory** (mypkg/runspec.toml), never at the project root. The package dir contains __init__.py (Python) or index.ts/index.js (Node).

2. **Each runnable is a top-level TOML section**: [name]. Never [runnables.name] or [scripts.name].

3. **[config] is reserved** — never use it as a runnable name.

4. **Entry point name must exactly match the runnable name** — runspec local discovers entry points by matching installed script name to TOML section name.

5. **Reinstall after editing pyproject.toml** — pip install -e . (Python) or npm install (Node).

6. Default autonomy = "confirm" unless explicitly asked otherwise.

---

## Fast path: `runspec init`

If runspec is already installed, prefer scaffolding the skeleton rather than hand-writing every file. From **inside the package directory**:

```bash
runspec init --name <name> --lang python      # also: typescript, javascript
```

This writes the `[name]` TOML section, a code stub, and (with `--write-project`) a `pyproject.toml` whose `[project.scripts]` and `dev = ["pytest", "ruff"]` extra are already wired. It also drops a placeholder typing stub plus the `runspec stubs` wiring (mypy path + pre-commit hook), so a parsed `RunSpec` types precisely from the first run. Then edit the args and `main()` to taste. The steps below are the manual equivalent — use them to add a runnable to an existing spec, or when you need full control.

---

## Arg types

| Type       | Notes                                                          |
|------------|----------------------------------------------------------------|
| "str"      | Text                                                           |
| "int"      | Integer                                                        |
| "float"    | Float                                                          |
| "bool"     | Boolean (true/false) — distinct from "flag"                    |
| "flag"     | Boolean switch; presence = true; default = false              |
| "path"     | File/dir path coerced to pathlib.Path (Python); always required unless given a default |
| "choice"   | Requires options = [...]                                       |
| "password" | Secret; refused on the command line, read from env / prompt, omitted from agent schemas, redacted from logs |
| "rest"     | List of strings captured after a literal `--`; at most one per runnable; never required |

**Inference (so you can often omit `type`):** default integer → "int", float → "float", string → "str", `true`/`false` → "flag", `options = [...]` → "choice". No `default` (and not flag/rest) → `required = true`; `type = "path"` with no default → required.

**Optional arg fields** (all on the inline table): `description`, `default`, `required`, `options`, `range = [min, max]`, `pattern` (full-match regex, str only), `min-length` / `max-length` (str only), `multiple = true`, `delimiter`, `short = "-x"`, `env`, `position` (1-based → positional), `deprecated`, `hint`, `meta` (pass-through dict runspec never reads).

Inline table style for args:
```toml
[myrule.args]
directory = {type = "path",   description = "Directory to process", default = "."}
dry_run   = {type = "flag",   description = "Preview only",         default = false}
format    = {type = "choice", description = "Output format",        options = ["text", "json"], default = "text"}
```

---

## Subcommands (optional)

A runnable can branch into subcommands under `[name.commands.<sub>]`. Args declared at the runnable level are shared by every subcommand; args under a subcommand are local to it. Set `require-command = true` to force the caller to pick a subcommand (no bare invocation).

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

## Groups (mutually-exclusive / co-required args)

Declare relationships between args under `[<name>.groups.<group>]` with one of
`exclusive` (at most one), `exactly-one`, `at-least-one`, `inclusive` (all or
none), or `if`/`requires` (conditional). **Group members must be optional** —
`required = false`, with no `default`/`env` — because runspec infers `required`
for a no-default arg, and a defaulted/env member always counts as "provided",
which breaks the constraint. `flag` args make ideal members.

```toml
[fetch.args]
url  = {type = "str",  required = false}
file = {type = "path", required = false}
[fetch.groups.source]
exactly-one = true
args        = ["url", "file"]
```

`--help` lists these under a **Constraints:** section. When testing, `smoke()` is
group-aware (includes at most one member of an exclusive/exactly-one group), but
**supply only one member of the group in `values`** if you want the valid-path
check to run for an `exactly-one` group, e.g. `h.smoke(values={"url": "x"})`.

---

## Autonomy

| Level | Use for |
|---|---|
| "confirm" (default) | Destructive ops — agent presents intent, awaits human approval |
| "autonomous" | Read-only / safe ops — agent runs freely |
| "supervised" | Agent runs, human reviews output before acting on it |
| "manual" | Human only — agent cannot invoke |

For a **destructive flag**, gate it in code so an agent can't trigger it without declared autonomy (the human typing the flag on the CLI is their own confirmation):

```python
if args.runspec_agent and args.runspec_autonomy != "autonomous":
    raise SystemExit("✗ --delete requires autonomy='autonomous' for agent invocation")
```

---

## Python stub

```python
from runspec import parse

def main():
    args = parse()
    print("done")
```

Read an argument's value with `args.<name>.value` (e.g. `args.directory.value`)
— `args.<name>` is an `Arg`, and since 0.38.0 it is not a transparent proxy, so
`if args.x:` / `args.x == y` raise a `TypeError` telling you to add `.value`. Use
`name in args` / `args.get(name, default)` for args that only exist under some
subcommands.

**The `Arg` carries metadata you can use in code** (not just `.value`). Each arg
is self-describing — `args.x.type`, `.default`, `.options`, `.range`, `.env`,
`.meta` (your pass-through dict), and the rest of the spec. Most useful is
**`.source`, the value's provenance**: where the value actually came from, one of
`"cli"`, `"env"`, `"runspec_env"`, `"spec_default"`, `"not_set"` (resolution
order cli → env → spec_default → not_set). Use it to react to *how* a value
arrived:

```python
# Refuse a secret typed on the command line (leaks into shell history / ps):
if args.api_key.source == "cli":
    raise SystemExit("✗ Pass the key via the env var, not the command line")

# Tell 'caller chose this value' apart from 'fell back to the default':
if args.workers.source == "spec_default":
    logger.info("workers not set — using default %d", args.workers.value)
```

Per-arg metadata is **Python-only** — the Node pack returns bare coerced values
(`args.x` is the value itself, no `.value`/`.source`). Full field list +
provenance table: <https://runspec.app/python/#arg>.

pyproject.toml entry:
```toml
[project.scripts]
myrule = "mypkg.myrule:main"
```

## Node/TypeScript stub

```typescript
import { parse } from "runspec-node";
const args = parse();
console.log("done");
```

package.json entry:
```json
"bin": { "myrule": "dist/myrule.js" }
```

---

## Typed args — `runspec stubs`

`Arg.value` is typed `Any` by default, so a typo (`args.wrokers.value`) sails
through the type checker. Once the package is installed, run `runspec stubs` and
`parse("myrule")` becomes precisely typed at the call site — no annotation, no
cast (`args.workers.value` → `int`, choices → `Literal[...]`, a typo'd attribute
is a hard error, not silent `Any`).

```bash
runspec stubs          # generate types from the installed runnables
runspec stubs --check  # CI / pre-commit: fail if missing or stale
runspec stubs --off    # remove the stub, back to plain Any
```

It writes one committed artifact — `typings/runspec/__init__.pyi` — regenerated
whenever a `runspec.toml` changes (commit it like a lock file). pyright/Pylance
read `typings/` automatically; for mypy add `mypy_path = "typings"`
(`runspec init --write-project` wires both). A stale stub never affects runtime —
`parse()` reads the `runspec.toml`, not the stub — so treat regeneration as a
convenience, not a correctness gate. After adding or retyping an arg, re-run
`runspec stubs` and commit `typings/`. Full guide: <https://runspec.app/typed-args/>.

---

## Error handling — let it propagate, don't wrap

**Do not** wrap `parse()` or the body of `main()` in a blanket `try/except` that prints the error and calls `sys.exit(1)`. runspec already does this for you, better:

- **Argument / config errors are handled by `parse()` itself** — always, with or without `[config.logging]`. Bad args, missing required args, bad choices, unknown runnable, missing config: runspec prints a clean human message and exits 1. Wrapping `parse()` adds nothing.
- **Uncaught runtime exceptions in your own logic are handled too — when `[config.logging]` is declared.** runspec installs a process-level handler (Python `sys.excepthook`, Node `uncaughtException`/`unhandledRejection`) that writes a structured audit record and a concise stderr line (`ERROR: ValueError: ...`), or a full traceback under `--debug`. **Just let the exception propagate.**

A blanket `try/except Exception: print(e); sys.exit(1)` is an anti-pattern here: it throws away the structured audit record and the traceback, hides the exception type, and duplicates what runspec does. So write the happy path:

```python
from runspec import parse

def main():
    args = parse()                 # arg/config errors already exit cleanly
    result = do_the_work(args)     # let any exception propagate — runspec logs + renders it
    print(result)
```

The few **legitimate** reasons to raise or exit explicitly:

- **Intentional control flow with a clear message** — e.g. the destructive-flag autonomy gate above (`raise SystemExit("✗ --delete requires autonomy='autonomous' …")`).
- **Catching one *specific*, expected exception** to add a remediation hint — then re-raise or `raise SystemExit(msg)`. Catch the narrow type (`except ConnectionError`), never bare `except Exception`.

(Without `[config.logging]`, an uncaught exception falls back to the language's default traceback — another reason to add a `[config.logging]` table rather than hand-rolling error handling.)

---

## Testing the runnable

runspec ships a test harness that is **stdlib-only and pulls in no test framework**, so importing it is free. The key insight: `parse()` already takes `argv=` and `config_path=`, so you never monkeypatch `sys.argv` or mock the parser. There are two layers worth testing:

1. **Wiring / parsing — no mocks.** Does each (sub)command accept its args, coerce types, and enforce required args / `require-command`?
2. **The actual work — mock only the I/O boundary.** Keep `main()` thin (`parse() → plain function → print`), then test the plain function directly and patch its single external call with `unittest.mock`.

### `ParseHarness` (Python — `from runspec.testing import ParseHarness`)

- `expect_ok(argv)` → returns the parsed `RunSpec`; fails the test if it exits.
- `expect_exit(argv, code=1, contains=...)` → asserts a clean error exit; `contains` checks the message names the offending flag. `code=0` for `--help`.
- `smoke(values={...})` → auto-covers the whole command tree in one call: `--help` exits 0, `require-command` is enforced, omitting required args exits 1, and (when `values` supplies arg values) a fully-populated invocation parses.

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

You can also pass an inline spec with `toml="..."` instead of `config_path=`.

**Node:** `import { ParseHarness } from "runspec-node/testing"` — same API (`expectOk`, `expectExit({ code, contains })`, `smoke({ values })`).

### `runspec test` — the CI gate

`runspec test` smoke-tests **every installed runnable**: an in-process spec smoke (the `ParseHarness.smoke` checks above) plus a subprocess that runs the installed entry point with `--help` — proving the runnable's own code imports and is wired to the right spec. Exits 1 if any runnable fails. Run it after install; add it to CI.

```bash
runspec test                  # all runnables
runspec test --runnable myrule
runspec test --format json
```

---

## Steps

0. If the request is thin, **grill out the requirements first** (see "Then: grill out the requirements") and play back a one-paragraph spec for sign-off before scaffolding.
1. Find the package directory — locate runspec.toml or the source dir with __init__.py / index.ts. (Or run `runspec init` from inside it for the skeleton.)
2. Add the runnable section to runspec.toml. If creating fresh, add the schema comment first:
   #:schema https://runspec.app/runspec.schema.json
3. Create the code stub in the package directory. Keep `main()` thin — delegate the real work to a plain function so it's directly testable.
4. Wire the entry point in pyproject.toml or package.json.
5. Add a test under the package's tests/ dir using `ParseHarness` (at least a `smoke()` call; assert on key arg coercion / validation).
6. Reinstall — pip install -e . or npm install.
7. Generate stubs (Python) — `runspec stubs`, then commit `typings/`.
8. Validate — run `runspec local` (confirm the runnable appears and is callable), then `runspec test` (confirm it passes the wiring gate). Run `pytest` for your own tests.
