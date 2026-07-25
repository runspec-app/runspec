# MCP server → module + runnable

Turn an MCP server into the runspec house pattern: a dependency-free **logic
core** (the reusable API) plus a thin **runnable wrapper** whose `runspec.toml`
carries the prompts, types, and autonomy. Because `runspec serve` re-expands a
runnable's subcommands into individual leaf MCP tools, the result is: *one MCP
server → one importable module + one runnable → back to individual MCP tools* —
now runnable over the runspec-console SSH fleet (local **and** remote) with
per-tool confirm gates and `run_as` identity, and no second server process on the
host.

This is the same guidance the `mcp-to-runnable` Claude Code skill follows, so a
human and an agent build these packages the same way. It is a specialisation of
the [Authoring guide](authoring.md) — read that for the per-runnable mechanics
(arg types, the boundary-unpack stub, testing); read this for the
server→module+runnable mapping.

!!! tip "Use it as an agent skill"
    The raw skill file is published here too — fetch it on any machine, no repo
    access needed, and drop it in as a Claude Code skill:

    ```bash
    curl -O https://runspec.app/skills/mcp-to-runnable.md
    ```

---

## What you produce

Two sibling packages, mirroring the `runspec-linux-core` / `runspec-linux` split:

- **`runspec-<name>-core`** — the "API part". A **dependency-free** package (no
  `runspec`, no `runspec.toml`, no entry points) with one plain function per MCP
  tool: plain params in, plain data out, *raise* on failure. Because it doesn't
  require `runspec`, it stays invisible to `runspec local` / `runspec serve`
  discovery, so a downstream package can import a **subset** of its helpers
  without surfacing any runnables. This is the reuse story.
- **`runspec-<name>`** — the thin wrapper. Depends on `runspec` + the core. Ships
  **one runnable** whose **verbs are the server's tools**, with all prompts /
  types / autonomy in `runspec.toml`. A `cli.py` dispatcher unpacks each `.value`
  at the boundary and calls the matching core helper.

`runspec serve` flattens the one runnable's verbs into leaf MCP tools named
`<name>_<tool>`, so an agent still calls the tools individually.

## Port, don't proxy

The core **reimplements** each tool in Python rather than spawning or proxying
the original (usually Node) MCP server. Porting is what lets the runnable run over
SSH with nothing extra installed on the host, keeps the logic importable and
reusable, and puts it under runspec's governance. Pure-logic tools (filesystem,
text, parsing) port to the standard library; service-backed tools have the core
wrap the client library or HTTP — still with no `runspec` dependency.

## Map the tools

For every MCP tool you need its name, description, and `inputSchema`. Each tool
becomes a verb (`[<name>.commands.<tool>]`); each schema property becomes an arg:

| MCP `inputSchema` | runspec arg |
|---|---|
| `string` | `str` (use `path` when the property names a filesystem path) |
| `number` / `integer` | `float` / `int` |
| `boolean` | `flag` (presence = true) |
| `string` with `enum` | `choice` + `options = [...]` |
| `array` of scalars | the scalar type + `multiple = true` |
| **`array` of objects** | **decompose into one parallel `multiple` arg per object field, zipped in the core** — e.g. an `edits: [{oldText, newText}]` tool becomes `--old-text` + `--new-text`, paired by position. No new arg type, and the agent sees two typed string arrays. |
| nested / irregular object | *fallback:* a `str` arg carrying JSON the core parses |
| secret-shaped (`password`, `token`, `api_key`) | `password` (masked, refused on the CLI, omitted from agent schemas) |

runspec has no native array-of-object type; the parallel-`multiple`-args
decomposition is the house pattern for it.

**Autonomy** — reads/queries are `autonomous`; anything that writes, deletes,
moves, sends, or mutates external state is `confirm` (the default).

## Optional: per-invocation run-as

If the ported tools act on OS-owned resources and the operator or agent should
pick *which* OS user runs each call, add two **inherited global args** under
`[<name>.args]` — `run-as` (`str`) and `become-method` (`choice` sudo/su) — so
they surface on every leaf MCP tool. When the tool is already a subprocess, wrap
its argv with a `sudo -n -u <user>` / `su -c` builder. When it runs in-process,
resolve the args into a plain `(verb, kwargs)` dict and ship it over stdin to a
tiny worker module re-invoked as the target user, with the verb→helper dispatch
in a runspec-free module the worker imports (so the escalated user needs only the
pure core). Access stays bounded by the selected user's OS permissions.

## Steps

1. **Get the tool list** and confirm the mapping — package names, which tools,
   autonomy per tool, and whether each tool's logic can be reimplemented in the
   standard library or needs a client library.
2. **Core package** — `pyproject.toml` (`dependencies = []`, no scripts),
   `errors.py`, one logic module per tool group, `__init__.py` re-exports,
   `CHANGELOG.md`, tests over real fixtures.
3. **Wrapper package** — `pyproject.toml` (deps + `[project.scripts]` `<name> =
   "runspec_<name>.cli:main"`), `runspec_<name>/runspec.toml` (`[<name>]` + a
   `[<name>.commands.<tool>]` per verb), the `cli.py` dispatcher, `CHANGELOG.md`,
   tests (a `smoke()` over the verb tree + a couple of end-to-end verb runs).
4. **Install & verify** — `pip install -e` the core, `runspec`, then the wrapper
   (run tests from **inside each package dir** so the installed `runspec` isn't
   shadowed by the sibling source). `runspec local` shows `<name>`; `runspec test
   --runnable <name>` passes; drive one `runspec serve` round-trip to confirm the
   `<name>_<tool>` leaf tools and their autonomy.

## Worked example: the Filesystem MCP server

`runspec-fs-core` + `runspec-fs` are the shipped proof of concept — the 13-tool
[Filesystem MCP server](https://github.com/modelcontextprotocol/servers/tree/HEAD/src/filesystem)
ported to the `fs` runnable (read/write/edit/list/tree/search/move/stat; reads
`autonomous`, mutations `confirm`). `edit-file` uses the paired
`--old-text`/`--new-text` decomposition, and the runnable takes a per-invocation
`--run-as`. See [Agent integration (MCP)](agents.md) for how the leaf tools reach
the console agent.

---

**Next:** [Agent integration (MCP)](agents.md) — how `runspec serve` exposes
runnables (and their verbs) as MCP tools.
