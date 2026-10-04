---
description: Add a new runspec runnable to the current project — TOML section, code stub, entry point, validation, and a test
---

Add a runnable to this project: **$ARGUMENTS**

> Published companion guide: <https://runspec.app/authoring/> (mirrors this skill;
> link the human there). Typed args: <https://runspec.app/typed-args/>. Testing:
> <https://runspec.app/testing/>. Logging: <https://runspec.app/logging/>. Audit
> events: <https://runspec.app/audit/>. This file is also downloadable raw at
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
- **What must be on the record** — which actions inside a run (a refund issued,
  a host rebooted, an account unlocked, a file deleted) someone will later need
  to prove happened. Each one gets an audit event (see "Audit events"); pin down
  the event names and what goes in `target` / `detail`.
- **Who it runs as** — personal tool, or installed in a shared venv owned by a
  service account? That decides whether the `run_as` default fits (see
  "Who it runs as").
- **Where else it runs** — will it be a Rundeck job (schedule, node filter,
  failure alerts) or an Ansible task? Then it needs a `meta.rundeck` /
  `meta.ansible` table (see "Rundeck and Ansible").
- **Failure modes** — what can go wrong, and what should happen (let it
  propagate, or catch one specific case for a remediation hint).
- **Verification** — how we'll know it works (the test's key assertions).

Stop when the unknowns left are low-risk, then **play it back as a one-paragraph
spec** — name, description, the arg table, autonomy, subcommands, audit events —
and get a yes
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

## Anti-patterns — do NOT do these

Common wrong turns models make around runspec. If your first instinct is any of
these, stop — the right pattern is in "Python stub" and "Testing the runnable"
below. Each of these looks like defensive/clean code but actively fights the
library:

- **`getattr(args, name)` or walking `args.__dict__`.** `parse()` returns a
  `RunSpec`; `args.name` is an `Arg`. For an arg that only exists under some
  subcommands use `name in args` and then `args.name.value`. Never `getattr` —
  attribute access is already the right API, and `getattr` disables the
  `TypeError` guard that catches the `if args.x:` / `args.x == y` mistakes.
- **`types.SimpleNamespace`, `Mock()`, `argparse.Namespace`, or a hand-rolled
  dataclass to fake args in tests.** You never need to fake a `RunSpec`. Either
  use `ParseHarness` (real spec, no mocks) *or* factor `main()` to unpack
  `.value` at the boundary and pass plain primitives to a worker function you
  test directly. See "Testing the runnable — Layer 2".
- **Wrapping `parse()` in an adapter class, a dict, or a dataclass "for cleaner
  code".** The boundary-unpack pattern in `main()` (unpack each `.value` into a
  local, hand it to a worker taking plain params) is the clean version — that's
  what the shipped runnables do (see `runspec_linux.nc_command:main`). An
  adapter layer just hides `.source`, `.type`, and the `TypeError` guards.
- **Rebuilding argparse on top of runspec.** No `add_argument`, no `Namespace`,
  no `sys.argv` munging. `parse()` already reads `sys.argv`; tests pass `argv=`
  via `ParseHarness`, not by mutating `sys.argv`.
- **Reading `args.name` as if it were the value.** `args.name` is an `Arg`; read
  `.value`. `if args.dry_run:` raises `TypeError` on purpose — write
  `if args.dry_run.value:`.
- **Blanket `try/except` around `parse()` or `main()`.** See "Error handling" —
  runspec already handles arg errors and uncaught runtime exceptions. Wrapping
  throws the structured run record away.
- **Hand-rolling logging or an audit trail.** No `logging.basicConfig()`, no
  handlers, no writing your own `actions.log` / CSV / JSON trail. Logging is on
  by default (`logging.getLogger(__name__)` just works), and an action that must
  be on the record is one `runspec.audit(...)` call, which lands in a
  hash-chained ledger *and* the OS log. A homemade file has neither.
- **Putting secrets in an audit event.** `target` / `detail` are for what was
  acted on and what happened. runspec redacts secret-looking keys and values,
  but never pass a password, token or key there in the first place.
- **Adding a `--user` / `--sudo` arg to escalate.** Who a runnable runs as is
  the spec's `run_as` (default: the venv's owner), resolved and enforced by
  runspec and the executors. Don't rebuild it in code.

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

**Optional arg fields** (all on the inline table): `description`, `default`, `required`, `options`, `range = [min, max]`, `pattern` (full-match regex, str only), `min-length` / `max-length` (str only), `multiple = true`, `delimiter`, `short = "-x"`, `env`, `position` (1-based → positional; still accepted as `--name`), `deprecated`, `autonomy` (per-arg override — most restrictive wins, e.g. a `--force` flag at `"confirm"` on an otherwise autonomous runnable), `ui` (form-control hint for the console / room forms: `"date"`/`"datetime"`/`"time"`/`"month"`/`"color"` on a `str`, `"slider"` on a ranged number, `"multiselect"` on a `multiple` choice — never changes parsing), `hint` (form placeholder), `meta` (pass-through dict runspec never reads).

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
group-aware for every constraint: it includes at most one member of an
exclusive/exactly-one group, and adds a fabricatable member (a flag or a choice)
to satisfy an `at-least-one`/`inclusive`/`if`+`requires` group. If a group's only
members are plain `str`/`int` args with no value to fabricate (and, for
`exactly-one`, when none is supplied), smoke **skips** that leaf's valid-path
check — **supply one member in `values`** to cover it, e.g.
`h.smoke(values={"url": "x"})`.

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

**Boundary-unpack pattern** — unpack each `.value` at the top of `main()` and
hand plain primitives to a worker function. Nothing runspec-shaped crosses into
your business logic. This is what the shipped runnables do (e.g.
`runspec_linux.nc_command:main` → `nc_send(host, port, command, wait=…)`), and
it's what makes tests trivial (see Layer 2 below):

```python
# mypkg/myrule.py — thin wiring, one runnable
from runspec import parse
from mypkg._core import do_the_work

def main() -> None:
    args = parse("myrule")
    result = do_the_work(
        directory=args.directory.value,
        dry_run=args.dry_run.value,
        format=args.format.value,
    )
    print(result)
```

```python
# mypkg/_core.py — plain function, no runspec import, no Arg objects
def do_the_work(*, directory: str, dry_run: bool, format: str) -> dict:
    ...
```

Read an argument's value with `args.<name>.value` (e.g. `args.directory.value`)
— `args.<name>` is an `Arg`, and since 0.38.0 it is not a transparent proxy, so
`if args.x:` / `args.x == y` raise a `TypeError` telling you to add `.value`. Use
`name in args` / `args.get(name, default)` for args that only exist under some
subcommands (both return the `Arg`; still read `.value` off it).

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

## Logging — on by default, just use a logger

From runspec 0.71.0 (Python) every runnable gets logging **without declaring
anything**: a JSON log file under `{venv}/logs/`, a run record for every run
(who ran it, which args with secrets left out, exit code, duration), INFO →
stdout (reads like `print()`, and is what an agent sees as the tool's answer),
WARNING+ → stderr, an auto-added `--debug` flag, and the uncaught-exception
handler. So in your code:

```python
import logging
logger = logging.getLogger(__name__)

logger.info("Rotated %d files", count)                    # stdout + log file
logger.warning("Skipped %s: locked", path)                # stderr + log file
logger.info("Synced", extra={"host": host, "rows": n})    # extra fields → JSON "extra"
```

Don't call `logging.basicConfig()` or attach handlers. Add a `[config.logging]`
table only to tune it (`rotate`, `keep`, `summary_console = true` to echo a
one-line summary to stderr). If the package genuinely brings its own logging
(structlog, loguru, a corporate handler), opt out of the takeover:

```toml
[config.logging]
enabled = false   # audit-only: runspec leaves logging alone, still writes the run record + audit events
```

There is no switch that turns off the run record itself. The **Node pack** is
still opt-in: there, add `[config.logging]` and use `getLogger(name)` from
`runspec-node`. Full guide: <https://runspec.app/logging/>.

---

## Audit events — record the actions that must be on the record

The run record already says *that* the runnable ran. An **audit event** records
a specific action *inside* the run that someone may later have to prove: a
refund issued, a host rebooted, an account unlocked, a user added to a group, a
record deleted. Ask during the grill which actions those are. Read-only
runnables usually need none.

```python
import runspec

def main() -> None:
    args = runspec.parse("unlock-account")
    user = args.user.value
    unlock(user)
    runspec.audit(
        "account.unlock",                 # dotted lowercase noun.verb — people search by it
        "succeeded",                      # outcome: started / succeeded / failed / denied …
        target={"user": user},            # what was acted on
        detail={"ticket": args.ticket.value},  # anything else worth keeping (no secrets)
    )
```

- The event is tied to the current run automatically (runnable, subcommand,
  `run_id`, whether an agent ran it, what triggered it) and goes to the venv's
  append-only, **hash-chained** ledger `{venv}/logs/audit.jsonl`, the OS log
  (syslog/journald or the Windows event log), and the run's own log, so it
  shows next to the run in History and `runspec logs`.
- **Must-not-happen-unrecorded:** pass `strict=True` before the action — it
  raises `runspec.AuditError` if nothing could record it, so the action never
  runs off the record. Without `strict`, `audit()` never raises for a recording
  failure (only `ValueError` for a bad event name — a bug in your code).
- **Long or risky action:** record `"started"` (with `strict=True`) before and
  `"succeeded"` / `"failed"` after, so a crash in between still leaves a
  `started` with no matching end:

```python
runspec.audit("host.reboot", "started", target={"host": host}, strict=True)
try:
    reboot(host)
except Exception as exc:
    runspec.audit("host.reboot", "failed", target={"host": host}, detail={"error": str(exc)})
    raise                                  # still let it propagate
runspec.audit("host.reboot", "succeeded", target={"host": host})
```

- **From a shell script** (a Bash runnable, or a step that isn't Python):
  `runspec audit record host.reboot --outcome succeeded --target host=web-01 --detail by=cron`
  (`--strict` exits 1 instead of raising). Read with `runspec audit list`,
  check the chain with `runspec audit verify`.
- Where the audit call lives: keep it in the thin `main()` (or a small helper it
  calls) around the worker call, so the worker stays a plain function you test
  without runspec. `[config.audit]` (`syslog`, `facility`) only tunes the OS-log
  copy; there is no setting that turns the ledger off.

Python only for now (runspec 0.71.0+); the Node pack follows. Full guide:
<https://runspec.app/audit/>.

---

## Who it runs as — `run_as`

From runspec 0.68.0 a runnable that doesn't set `run_as` runs as **the account
that owns its venv** (`"@owner"`, never root). In a personal venv that's you, so
nothing changes. In a shared venv owned by a service account, every tool runs as
that account: the console / `runspec serve` / `runspec exec` escalate to it
(`sudo -u`), and a direct run by anyone else is refused with the exact
`sudo -u <owner> …` command to use (`enforce_run_as = "error"`, the default).

Usually leave it alone. Set it only when the ask calls for it:

```toml
[restart-app]
run_as = "appsvc"        # a specific account (root only for tools that truly need it)

[config]
run_as = ""              # package-wide: run as whoever invokes it, no check
enforce_run_as = "warn"  # or just warn on a mismatch instead of refusing
```

Secrets (`password` args, credentials) are passed to escalated runs on stdin,
never on a command line, so nothing in your code changes for `run_as`. Full
reference: <https://runspec.app/format/#run_as-become_method-become_flags>.

---

## Rundeck and Ansible — `meta.rundeck` / `meta.ansible`

`runspec emit --rundeck` writes one Rundeck job (JSON) per leaf command to
`jobs/`, and `runspec emit --ansible` writes one Ansible module per leaf command
to `library/`. Both read the same `runspec.toml`: args become job options /
module params, `password` args become Rundeck secure options / Ansible `no_log`
params on the secret channel (never argv). Anything the scheduler needs that the
interface doesn't say lives in a `meta` pass-through table — runspec itself never
reads `meta`, the emitters do. If the runnable will be a Rundeck job or an
Ansible task, write that table now, next to the runnable:

```toml
[config.meta.rundeck]            # project-wide defaults
venv  = "/opt/app/.venv"         # venv whose bin/ holds the runnable
group = "myteam"                 # Rundeck job group (default "runspec")

[rotate-logs.meta.rundeck]       # this runnable only (overrides [config])
node_filter = "tags: web"        # presence turns on orchestrator subset=1 (one node per run)
schedule    = "0 0 2 * * ?"      # Quartz crontab (6/7 fields), NOT Unix 5-field cron

[rotate-logs.meta.rundeck.notifications.onfailure]
email = "ops@example.com"        # also onsuccess / onstart; email and/or webhook

[rotate-logs.meta.ansible]
venv = "/opt/app/.venv"          # the only Ansible setting
```

Other `meta.rundeck` keys: `orchestrator` (`"subset"`/`"none"`), `threadcount`,
`loglevel`, `strategy`, `keepgoing`, `timeout`, `retry`. Precedence for both: CLI flag →
`[<runnable>.meta.*]` → `[config.meta.*]` → built-in default.

- **Ansible idempotency:** with `output = "json"`, print a `changed` key
  (`{"changed": false, ...}`) so the task reports changed/ok correctly; without
  it a zero exit reports `changed: true`. The module runs the runnable with
  `RUNSPEC_AUTONOMY=autonomous`, so a `confirm` runnable runs unattended in a
  playbook.
- **Not carried over:** `run_as` (the Rundeck script calls the venv binary
  directly; escalate on the node executor) and group constraints.
- **Keep them fresh:** commit `jobs/` / `library/` and add
  `runspec emit --rundeck --check` / `runspec emit --ansible --check` to CI (exit 1
  when stale). To push jobs to a Rundeck server from CI, use the
  `runspec-rundeck` package (`rundeck sync`).

Guides: <https://runspec.app/rundeck/>, <https://runspec.app/ansible/>.

---

## Error handling — let it propagate, don't wrap

**Do not** wrap `parse()` or the body of `main()` in a blanket `try/except` that prints the error and calls `sys.exit(1)`. runspec already does this for you, better:

- **Argument / config errors are handled by `parse()` itself** — always, with or without `[config.logging]`. Bad args, missing required args, bad choices, unknown runnable, missing config: runspec prints a clean human message and exits 1. Wrapping `parse()` adds nothing.
- **Uncaught runtime exceptions in your own logic are handled too.** Logging is on by default (runspec 0.71.0+), so runspec installs a process-level handler (Python `sys.excepthook`, Node `uncaughtException`/`unhandledRejection`) that writes a structured audit record and a concise stderr line (`ERROR: ValueError: ...`), or a full traceback under `--debug`. **Just let the exception propagate.**

A blanket `try/except Exception: print(e); sys.exit(1)` is an anti-pattern here: it throws away the structured run record and the traceback, hides the exception type, and duplicates what runspec does. So write the happy path:

```python
from runspec import parse

def main():
    args = parse()                 # arg/config errors already exit cleanly
    result = do_the_work(directory=args.directory.value)  # let any exception propagate — runspec logs + renders it
    print(result)
```

The few **legitimate** reasons to raise or exit explicitly:

- **Intentional control flow with a clear message** — e.g. the destructive-flag autonomy gate above (`raise SystemExit("✗ --delete requires autonomy='autonomous' …")`).
- **Catching one *specific*, expected exception** to add a remediation hint — then re-raise or `raise SystemExit(msg)`. Catch the narrow type (`except ConnectionError`), never bare `except Exception`.

(Exceptions: a package with `[config.logging] enabled = false` keeps its own `sys.excepthook`, and the Node pack is still opt-in — without `[config.logging]` there, an uncaught exception falls back to the default stack trace.)

---

## Testing the runnable

runspec ships a test harness that is **stdlib-only and pulls in no test framework**, so importing it is free. The key insight: `parse()` already takes `argv=` and `config_path=`, so you never monkeypatch `sys.argv` or mock the parser. There are two layers worth testing:

1. **Wiring / parsing — no mocks.** Does each (sub)command accept its args, coerce types, and enforce required args / `require-command`?
2. **The actual work — mock only the I/O boundary.** Keep `main()` thin (`parse() → plain function → print`), then test the plain function directly and patch its single external call with `unittest.mock`.

### `ParseHarness` (Python — `from runspec.testing import ParseHarness`)

- `expect_ok(argv)` → returns the parsed `RunSpec`; fails the test if it exits.
- `expect_exit(argv, code=1, contains=...)` → asserts a clean error exit; `contains` checks the message names the offending flag. `code=0` for `--help`.
- `smoke(values={...})` → auto-covers the whole command tree in one call: `--help` exits 0, `require-command` is enforced, omitting required args exits 1, and (when `values` supplies arg values) a fully-populated invocation parses. It's **group-aware** (caps an exclusive/exactly-one group at one member, and adds a fabricatable member to satisfy an at-least-one/inclusive/conditional group) and **choice-aware** — when the same arg name declares different `options` per subcommand, smoke picks a value valid for each leaf, so one shared `values` entry (or none) works tree-wide; no per-subcommand workaround needed.

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

### Layer 2 — test the worker with plain primitives (no fake args)

Because `main()` unpacks `.value` at the boundary and hands primitives to a
worker function (see "Python stub"), Layer 2 tests never need a `RunSpec`, a
`SimpleNamespace`, or a mock — just call the worker directly:

```python
# tests/test_core.py
from unittest.mock import patch
from mypkg._core import do_the_work

def test_dry_run_does_not_write():
    with patch("mypkg._core._write_file") as w:
        result = do_the_work(directory=".", dry_run=True, format="json")
    assert result["written"] == 0
    w.assert_not_called()
```

### Driving `main()` itself in a test

`ParseHarness` skips the logging setup and the `run_as` check for the parses it
makes. A test that calls your runnable's `main()` directly runs the *real*
`parse()`, so:

- set `RUNSPEC_AUDIT=off` for the test run (e.g. `monkeypatch.setenv` in an
  autouse fixture, or in the CI job) so it doesn't take over logging or write
  run records / audit events into your venv — **tests and CI only, never
  production**;
- wrap the call in `with no_run_as_check():` (from `runspec.testing`) so the
  `run_as` gate doesn't refuse it on your machine;
- to assert an audit event was recorded, patch `runspec.audit` and check the
  call (`event`, `outcome`, `target`).

If you find yourself building a `SimpleNamespace(directory=".", dry_run=True)`
or `Mock(spec=…)` to pass into `main()` or a helper that takes `args`, the
helper's signature is wrong — pull the `.value` unpack up into `main()` and give
the helper plain params. Same rule for `argparse.Namespace(**{…})` — you're not
using argparse; don't shape tests as if you were. For everything that *is*
about parsing (types, choices, groups, `require-command`, `--help`, missing
required args), use `ParseHarness` — it produces the real `RunSpec`.

### `runspec test` — the CI gate

`runspec test` smoke-tests **every installed runnable**: an in-process spec smoke (the `ParseHarness.smoke` checks above) plus a subprocess that runs the installed entry point with `--help` — proving the runnable's own code imports and is wired to the right spec. Exits 1 if any runnable fails. Run it after install; add it to CI.

```bash
runspec test                  # all runnables
runspec test --runnable myrule
runspec test --format json
```

---

### Scheduled / unattended runs — `runspec exec`

For cron, CI or another scheduler, call the runnable through
`runspec exec <name> [args…]` (optionally `--label`, `--lock PATH`,
`--skip-if-running`, `--timeout`). It records a run row even when the runnable
fails before parsing, the *observed* exit code, and captured stdout/stderr — and
escalates to the `run_as` account the same way `runspec serve` does.

---

## Steps

0. If the request is thin, **grill out the requirements first** (see "Then: grill out the requirements") and play back a one-paragraph spec for sign-off before scaffolding.
1. Find the package directory — locate runspec.toml or the source dir with __init__.py / index.ts. (Or run `runspec init` from inside it for the skeleton.)
2. Add the runnable section to runspec.toml. If creating fresh, add the schema comment first:
   #:schema https://runspec.app/runspec.schema.json
3. Create the code stub in the package directory. Keep `main()` thin — delegate the real work to a plain function so it's directly testable. Log with `logging.getLogger(__name__)`; add a `runspec.audit(...)` call for each action agreed as must-be-on-the-record.
4. Wire the entry point in pyproject.toml or package.json.
5. Add a test under the package's tests/ dir using `ParseHarness` (at least a `smoke()` call; assert on key arg coercion / validation). If a test drives `main()`, set `RUNSPEC_AUDIT=off` and use `no_run_as_check()`.
6. Reinstall — pip install -e . or npm install.
7. Generate stubs (Python) — `runspec stubs`, then commit `typings/`.
8. If it's a Rundeck job or Ansible task, add the `meta.rundeck` / `meta.ansible` table and regenerate (`runspec emit --rundeck` / `--ansible`); commit `jobs/` / `library/`.
9. Validate — run `runspec local` (confirm the runnable appears and is callable), then `runspec test` (confirm it passes the wiring gate). Run `pytest` for your own tests.
