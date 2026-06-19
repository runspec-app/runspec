# Typed `parse()` with `runspec stubs`

`runspec stubs` makes your editor and type checker understand each runnable's
arguments. After running it, `args = parse("deploy")` is precisely typed at the
call site — autocomplete, hover types, and typo-catching — with **no annotation
and no cast**:

```python
from runspec import parse

args = parse("deploy")
args.workers.value     # int
args.env.value         # Literal["dev", "prod"]
args.wrokers           # error: "_DeployArgs" has no attribute "wrokers"; maybe "workers"?
```

The type is keyed off the literal name you pass to `parse()`. A typo — or an arg
you haven't regenerated for yet — is a **hard error**, not a silent `Any`.

---

## TL;DR

```bash
runspec stubs          # generate types from the installed runnables
runspec stubs --check  # CI / pre-commit: fail if missing or stale
runspec stubs --off    # remove the stub, back to plain Any
```

It writes one file: `typings/runspec/__init__.pyi` at your project root.
**pyright / Pylance (VS Code) read `typings/` automatically** — zero config. For
mypy, add one line (see [Editor setup](#editor-setup)).

---

## What you get

Without the stub, a parsed arg is typed `Any` — it works, but the checker can't
help you:

```python
args = parse("deploy")
args.workers.value     # Any — no autocomplete, no checking
args.wrokers.value     # Any — typo sails through
```

After `runspec stubs`, every runnable has a precise type:

| In `runspec.toml` | `args.<name>.value` becomes |
|---|---|
| `workers = { default = 4 }` | `int` |
| `name = { type = "str" }` | `str` |
| `out = { type = "path" }` | `Path` |
| `format = { options = ["json", "yaml"] }` | `Literal["json", "yaml"]` |
| `tags = { type = "str", multiple = true }` | `list[str]` |

The runnable's metadata surface still works too — `args.runspec_agent`,
`args.get("maybe").value`, `"force" in args`. Subcommand args are all available
(the view is the superset across the command tree); read presence-dependent ones
with `args.get(...)`.

---

## The everyday workflow

This is the loop you'll repeat. If you scaffolded with `runspec init`, steps 1
and the editor config are already done for you.

1. **Scaffold (once).** `runspec init` drops a *placeholder* stub, so your editor
   immediately says *run `runspec stubs`* instead of silently typing everything
   `Any`. With `--write-project` it also wires the mypy line and a pre-commit
   hook.

2. **Install your package.** Discovery only sees installed runnables:

   ```bash
   pip install -e .      # or: uv sync / poetry install
   ```

3. **Generate.** The first `runspec local` after install auto-upgrades the
   placeholder to real types, or run it explicitly:

   ```bash
   runspec stubs
   ```

4. **Edit a `runspec.toml`** — add/rename/retype an arg. The stub is now stale.
   Your editor flags any *new* arg you reference (`no attribute`), and
   `runspec local` prints a nudge:

   ```
   ℹ  typings/runspec/__init__.pyi is out of date — run 'runspec stubs' to refresh.
   ```

5. **Regenerate and commit.** Run `runspec stubs` again, then commit the updated
   `typings/`. If you wired the pre-commit hook, it regenerates for you and the
   commit fails until you re-stage — so a stale stub can't slip through.

The mental model: the stub is **generated from your `runspec.toml`**, like a lock
file or any other codegen. Schema changed → regenerate. The reflex is the same as
re-running `poetry install` after editing dependencies.

---

## Editor setup

=== "VS Code / pyright / Pylance"

    Nothing to configure — pyright reads `./typings` by default. Reload the
    window after the first generation if types don't appear immediately.

=== "mypy"

    Point mypy at the directory (once):

    ```toml
    [tool.mypy]
    mypy_path = "typings"
    ```

    `runspec init --write-project` adds this for you.

---

## Keeping it fresh

A stub is only as good as its last generation. Three mechanisms keep it current,
from most to least automatic:

- **pre-commit hook** (wired by `runspec init --write-project`): regenerates the
  stub whenever a `runspec.toml` changes and fails the commit if it had to
  rewrite — re-stage and commit again. This is the "I don't have to remember"
  path.
- **`runspec local`**: auto-upgrades the `init` placeholder to real types, and
  nudges (without writing) when a real stub is stale.
- **CI gate**: `runspec stubs --check` exits non-zero if the committed stub is
  missing or out of date:

  ```yaml
  # .github/workflows/ci.yml
  - name: Check runspec stubs are current
    run: runspec stubs --check
  ```

> **A stale stub never affects runtime.** `parse()` reads your `runspec.toml`,
> not the stub. The worst a stale or missing stub does is show an out-of-date
> hint in your editor — never a crash, never wrong behavior. So it's safe to
> treat regeneration as a convenience, not a correctness requirement.

---

## Commands

| Command | What it does |
|---|---|
| `runspec stubs` | Scan the installed venv and write `typings/runspec/__init__.pyi`. |
| `runspec stubs --check` | Verify the stub is up to date; write nothing. Exit 1 if missing/stale. For CI and pre-commit. |
| `runspec stubs --off` | Remove the stub, restoring plain `Any`-typed arg access. |

Discovery uses `importlib.metadata` — the same set of runnables `runspec local`
sees. A package must be installed (`pip install -e .`) to be included.

---

## Should I commit `typings/`?

**Yes.** Commit `typings/runspec/__init__.pyi` like any generated artifact you
want teammates and CI to have without each person regenerating. The `--check`
gate then guards it against drift. (If you'd rather not, gitignore it and
generate in each environment — both work, but committing is simplest.)

---

## Troubleshooting

**Types still show as `Any`.**
Most often mypy isn't reading `typings/` — add `mypy_path = "typings"`. For
pyright, reload the editor window. Otherwise the stub may be stale or missing —
run `runspec stubs`.

**"No runspec-aware runnables found."**
The package isn't installed in this environment. Run `pip install -e .` (or `uv
sync` / `poetry install`) from your project, then `runspec stubs` again.

**A brand-new arg shows `no attribute`.**
That's the feature working — you added the arg to `runspec.toml` but haven't
regenerated. Run `runspec stubs`.

**The editor shows `_StubsNotGenerated`.**
That's the `runspec init` placeholder: you haven't generated real types yet. Run
`runspec stubs` (or `runspec local` after installing the package).
