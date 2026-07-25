---
description: Turn an MCP server into a runspec module + runnable — a dependency-free logic core (the reusable API) plus a thin runnable wrapper whose runspec.toml carries the prompts, types, and autonomy. One server → one importable module + one runnable → back to individual leaf MCP tools via `runspec serve`.
---

Port an MCP server into the runspec core-plus-wrapper pattern: **$ARGUMENTS**

> This skill is a *specialisation* of `add-runnable`. It reuses everything there
> (arg types, autonomy, the boundary-unpack stub, `ParseHarness` testing) and
> adds only what's specific to porting an MCP server. Read `add-runnable` for the
> per-runnable mechanics; read this for the server→module+runnable mapping.
>
> Reference implementation shipped in this repo: `runspec-fs-core` +
> `runspec-fs` (the Filesystem MCP server). Design doc:
> `docs/design/mcp-to-runnable-skill.md`.

## What this produces

Two sibling packages under `packages/python/`, mirroring
`runspec-linux-core` / `runspec-linux`:

1. **`runspec-<name>-core`** — the "API part". A **dependency-free** Python
   package (`dependencies = []`, **no** `runspec`, **no** `runspec.toml`, **no**
   entry points) with one plain function per MCP tool. It does the work, returns
   plain data, and *raises* on failure. Because it doesn't require `runspec`, it
   is invisible to `runspec local` / `runspec serve` discovery — so a downstream
   package can import a **subset** of its helpers (`from runspec_<name>_core
   import foo`) without surfacing any runnables. This is the reuse story.
2. **`runspec-<name>`** — the thin wrapper. Depends on `runspec` + the core.
   Ships **one runnable** whose **verbs are the server's tools**, declared in
   `runspec.toml` with all the prompts / types / autonomy. `runspec serve`
   re-expands each verb into its own leaf MCP tool (`<name>_<tool>`), so an agent
   still calls the tools individually — but now over the runspec-console SSH
   fleet (local **and** remote hosts), with per-verb confirm gates and `run_as`
   identity.

## First: get the MCP server's tool list

You need, for every tool: its **name**, **description**, and **inputSchema**
(each property's name, JSON-Schema type, required-or-not, and any `enum`).

- From the server's repo/README (the fs POC was built from the
  modelcontextprotocol/servers `src/filesystem` README), **or**
- From a live `tools/list` if you can run the server.

Write the list down before mapping — it is the verb table.

## Then: grill the mapping (don't guess it)

Ask 2–4 at a time with `AskUserQuestion`, grounded in the tool list:

- **Package names** — `runspec-<name>` + `runspec-<name>-core`, and the runnable
  name (`<name>`, matching the entry point).
- **Which tools** — all of them, or a subset? (A server may expose tools that
  don't make sense over the fleet.)
- **Autonomy per tool** — reads/queries → `autonomous`; anything that writes,
  deletes, moves, sends, or mutates external state → `confirm` (the default).
- **Portability** — *the key question.* Can the tool's logic be **reimplemented
  in Python** (stdlib or a small SDK), or does it need a real external service?
  - Pure-logic tools (filesystem, text, math, parsing) → port to stdlib in the
    core. This is the fs case.
  - Service-backed tools (a database, an API) → the core wraps the client
    library / HTTP; still no `runspec` dependency. (See `runspec-jsm` /
    `runspec-snow` for the "core wraps an HTTP API" shape.)
  - **Never** shell out to / spawn / proxy the original MCP server. Porting is
    the point — it's what lets the runnable run over SSH with no extra process.

Play it back as a one-paragraph spec (package names, the verb table with each
verb's args + autonomy) and get a yes before scaffolding.

## Hard rules — do not deviate

1. **Core is dependency-free.** `dependencies = []`, no `runspec`, no
   `runspec.toml`, no `[project.scripts]`. It must stay invisible to discovery
   and importable on its own. (The release CI asserts this — see the fs-core
   workflow's "Wheel contents check".)
2. **Wrapper depends on core + `runspec`.** `runspec-<name>` lists
   `runspec>=0.44.0` (leaf-globals in `serve`) and `runspec-<name>-core>=<v>`.
3. **One runnable, verbs = the server's tools.** `[<name>]` with
   `require-command = true`, then `[<name>.commands.<tool>]` per tool. `runspec
   serve` flattens these to `<name>_<tool>` leaf MCP tools automatically.
4. **Entry-point name == runnable name.** `[project.scripts]` `<name> =
   "runspec_<name>.cli:main"`, matching `[<name>]` in `runspec.toml`.
5. **Port, don't proxy.** Reimplement each tool in the core; never spawn the
   source server.
6. **No directory/resource jail unless the server's semantics require one.** In
   the console model, OS permissions of the invoking / `run_as` user already
   bound what a tool can reach — a wrong target an agent picks and a human
   confirms can't exceed what that user could already do. The fs POC deliberately
   enforces no jail (see its `allowed_directories` note). Add a jail only if the
   source tool guards something the OS doesn't.
7. **Reinstall after editing `pyproject.toml`** — `pip install -e .`.

## Type mapping — MCP JSON Schema → runspec arg

| MCP inputSchema | runspec arg |
|---|---|
| `string` | `str` (use `path` when the property names a filesystem path — it coerces to `Path` and infers `required`) |
| `number` / `integer` | `float` / `int` |
| `boolean` | `flag` (presence = true) |
| `string` with `enum` | `choice` + `options = [...]` |
| `array` of scalars | the scalar type + `multiple = true` (repeatable flag; `serve` advertises it as `array<items>`) |
| **`array` of objects** | **decompose into one parallel `multiple` arg per object field, zipped in the core** — e.g. the fs `edit-file` tool's `edits: [{oldText, newText}]` becomes `--old-text` + `--new-text` (both `str, multiple`), paired by position in `cli.py`. No new runspec type, and the agent sees two typed string arrays. |
| nested / irregular object | *fallback only:* a `str` arg carrying JSON that the core `json.loads`. Prefer the parallel-args decomposition; reach for this only when fields are irregular or deeply nested. |
| secret-shaped (`password`, `token`, `api_key`) | `password` (masked, refused on the CLI, omitted from agent schemas) |

> runspec has **no native array-of-object / structured type** today. The
> parallel-`multiple`-args decomposition is the house pattern for it; a real
> structured type would be a deliberate, SPEC-first, Node-parity change — not
> something this skill introduces.

## The two packages, concretely

Core module — one function per tool, plain params in, plain data out, raise on
failure (stdlib `OSError` family for I/O; a small `errors.py` base +
tool-specific exceptions for anything else). `__init__.py` re-exports every
helper. Model it on `runspec-fs-core/runspec_fs_core/` (or
`runspec-linux-core/`).

Wrapper `cli.py` — the boundary-unpack dispatcher (model on
`runspec-fs/runspec_fs/cli.py` or `runspec-claude/runspec_claude/cli.py`):

```python
def main() -> None:
    spec = rs.parse("<name>")
    try:
        result = _dispatch(spec)          # dispatch on spec.runspec_command
    except (CoreError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "kind": type(exc).__name__}))
        sys.exit(1)
    print(json.dumps(result, default=str))
```

`_dispatch` reads `spec.runspec_command`, unpacks each `spec.<arg>.value` (hyphen
→ underscore: `--old-text` → `spec.old_text`), and calls the matching core
helper. Only touch an arg under the command that declares it (no cross-verb
access). `output = "json"` on the runnable.

## Steps

0. Get the tool list; grill the mapping; play back a one-paragraph spec.
1. **Core package** — `packages/python/runspec-<name>-core/`: `pyproject.toml`
   (`dependencies = []`, `dev` extra, **no** scripts), `errors.py`, one logic
   module per tool group, `__init__.py` re-exports, `CHANGELOG.md`, `tests/`
   (real fixtures under `tmp_path`, not mocks).
2. **Wrapper package** — `packages/python/runspec-<name>/`: `pyproject.toml`
   (deps + `[project.scripts]` `<name> = "runspec_<name>.cli:main"`),
   `runspec_<name>/runspec.toml` (`[<name>]` + a `[<name>.commands.<tool>]` per
   verb), `cli.py` dispatcher, `__init__.py` (re-export core for back-compat),
   `CHANGELOG.md`, `tests/` (`ParseHarness.smoke()` over the verb tree + a
   couple of end-to-end verb runs).
3. **CI / release wiring** — clone the `fs-core` / `fs` jobs in
   `.github/workflows/ci.yml` (add both to the `changes` filter + outputs and the
   aggregate `ci:` `needs`/`check`), add `<name>-core-release.yml` +
   `<name>-release.yml`, add both manifests to `auto-tag.yml` (paths + matrix),
   and add two rows to `docs/releasing.md` (publish `-core` before the wrapper).
   Add the two packages to the `CLAUDE.md` package table.
4. **Install & verify** — `pip install -e` the core, `runspec`, then the wrapper
   (run tests from **inside each package dir** so the installed `runspec` isn't
   shadowed by the sibling source). `runspec local` shows `<name>`; `runspec test
   --runnable <name>` passes; drive one `runspec serve`
   initialize→tools/list→tools/call round-trip to confirm the `<name>_<tool>`
   leaf tools and their `x-autonomy`.
5. `runspec stubs` (Python) and commit `typings/` if the wrapper package uses it.

## Anti-patterns — do NOT do these

- **Spawning / proxying the source MCP server** (a subprocess, an HTTP proxy).
  Port the logic instead.
- **A runtime dependency in the core** — it must stay `dependencies = []` (a
  small SDK the tools genuinely need is the only exception; never `runspec`).
- **`runspec.toml` at the project root** — it lives inside the wrapper's import
  package (`runspec_<name>/runspec.toml`), so it ships in the wheel.
- **One top-level runnable per tool** instead of one runnable with verbs — you
  lose the "one server = one runnable" grouping (and `serve` already re-expands
  verbs into individual tools, so you gain nothing).
- The `add-runnable` arg anti-patterns (reading `args.x` instead of
  `args.x.value`, `getattr`, faking a `RunSpec` in tests, blanket `try/except`
  around `parse()`).
