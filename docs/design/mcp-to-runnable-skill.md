# MCP server → runspec module + runnable

Status: shipped (Filesystem proof-of-concept). Skill:
`.claude/skills/mcp-to-runnable/SKILL.md`. Worked example: `runspec-fs-core` +
`runspec-fs`.

## Why

An MCP server is a set of tools with typed inputs — exactly the shape a runspec
runnable already has. runspec-console reaches a fleet of hosts over SSH and
exposes installed runnables to its agent (itself an LLM) via `runspec serve`,
which turns a runnable's subcommands into individual leaf MCP tools. So there is
a clean bridge: **take an MCP server, re-express its tools as one runspec
runnable, and the console agent gets those tools on every fleet host** — local
and remote — with runspec's per-tool confirm gates, `run_as` identity, and audit
logging, and **no second server process** to deploy on each host.

The `add-runnable` skill scaffolds a single runnable. This is the
higher-level move: one MCP server → a reusable **module** (the API) + a
**runnable** (the interface), following the same core-plus-wrapper split the repo
already uses for `runspec-linux-core` / `runspec-linux`.

## The pattern

Two sibling packages:

- **`runspec-<name>-core`** — the "API part". Dependency-free
  (`dependencies = []`), **no** `runspec`, **no** `runspec.toml`, **no** entry
  points. One plain function per MCP tool: plain params in, plain data out,
  *raise* on failure. Because it doesn't require `runspec`, it is invisible to
  `runspec local` / `runspec serve` discovery, so a downstream package can import
  a **subset** of its helpers without surfacing any runnables. This is the reuse
  story — "when we need a subset of methods", just like `runspec-linux-core`.
- **`runspec-<name>`** — the thin wrapper. Depends on `runspec` + the core. Ships
  **one runnable** whose **verbs are the server's tools**, with all prompts /
  types / autonomy in `runspec.toml`. A `cli.py` dispatcher unpacks each
  `.value` at the boundary and calls the core helper.

`runspec serve` re-expands the one runnable's verbs into individual leaf MCP
tools named `<name>_<tool>` (e.g. `fs_read-text-file`), each advertising that
verb's own args and inherited globals plus its `x-autonomy`. So the agent calls
the tools individually — the round trip is: MCP `tools/call` →
`<name> <tool> --arg …` subprocess → `parse()` resolves the verb → core helper →
JSON on stdout → captured as the tool result.

### Port, don't proxy

The core **reimplements** each tool in Python rather than spawning or proxying
the original (usually Node) MCP server. Porting is what makes the runnable run
over SSH with nothing extra installed on the host, keeps the logic importable and
reusable, and puts it under runspec's governance. Pure-logic tools (filesystem,
text, parsing) port to stdlib; service-backed tools have the core wrap the client
library / HTTP (the `runspec-jsm` / `runspec-snow` shape) — still with no
`runspec` dependency.

## Worked example: the Filesystem MCP server

Ported from
[modelcontextprotocol/servers `src/filesystem`](https://github.com/modelcontextprotocol/servers/tree/HEAD/src/filesystem)
(13 tools) into:

- **`runspec-fs-core`** — stdlib helpers (`pathlib`/`os`/`shutil`/`difflib`) in
  `files.py` / `dirs.py` / `search.py` / `info.py`, with `errors.py`
  (`FsCoreError`, `EditNotAppliedError`). Filesystem failures propagate as the
  stdlib `OSError` family.
- **`runspec-fs`** — the `fs` runnable, one verb per tool.

Verbs and autonomy (reads autonomous, mutations confirm):

| verb | autonomy |
|---|---|
| `read-text-file` (`--head`/`--tail`), `read-media-file`, `read-multiple-files`, `list-directory`, `list-directory-with-sizes` (`--sort-by`), `directory-tree` (`--exclude`), `search-files` (`--exclude`), `get-file-info`, `list-allowed-directories` | `autonomous` |
| `write-file`, `edit-file`, `create-directory`, `move-file` | `confirm` |

The canonical spec is `packages/python/runspec-fs/runspec_fs/runspec.toml`.

### No directory jail — deliberately

The MCP server restricts operations to a set of "allowed directories" set at
launch. `runspec-fs` enforces **no** software jail: filesystem access is bounded
by the OS permissions of the user the process runs as — the logged-in user, or a
`run_as` service account. A wrong path the agent picks and a human confirms still
can't exceed what that user could already do on the host, so the OS is the real
boundary and a second, weaker software boundary would only add friction.
`list-allowed-directories` is kept as an **informational** verb (reports the
effective user + home/cwd) so the 13-tool parity holds and the agent knows who it
is and where it starts.

### Per-invocation run-as (0.2.0)

Like `runspec-linux`'s venv runnables (`create-venv`, `install-into-venv`, …),
`fs` takes a **runtime `--run-as <user>`** (+ `--become-method sudo|su`) rather
than a declarative `run_as`, so the user *or* the agent picks which OS user each
verb runs as per invocation — the service account that owns the files is chosen
at call time. They're **inherited global args** (`[fs.args]`), so `runspec serve`
advertises them on every leaf MCP tool.

The linux venv ops are already subprocesses (pip/venv/chmod), so escalation is
just prefixing them with `sudo -n -u <user>`. The fs verbs run **in-process**, so
`cli.py` instead resolves the args into a plain `(verb, kwargs)` dict and ships it
over stdin to a tiny worker (`runspec_fs._worker`) re-invoked as the target user
via `runspec_fs_core.build_become`. The worker imports only the pure core (no
`runspec`), so the escalated user needs nothing but the core importable and no
`runspec.toml` lookup happens under sudo (where the env is stripped). The
verb→core dispatch lives in a runspec-free `runspec_fs._dispatch` shared by
`cli.py` and the worker. Access stays bounded by the selected user's OS
permissions — consistent with the no-jail stance. This "re-exec a pure worker as
the target user" shape is the general answer for giving an **in-process** ported
tool a per-invocation run-as.

### Array-of-objects without a new type

The MCP `edit_file` tool takes `edits: [{oldText, newText}]`. runspec has no
native array-of-object arg type, and inventing one would be a SPEC-first,
Node-parity change out of scope for this work. Instead the verb takes **two
parallel repeatable args** — `--old-text` and `--new-text` (both `str`,
`multiple`) — which `cli.py` zips by position into `(old, new)` pairs (erroring
if the counts differ). This reuses runspec's existing `multiple` support, so the
agent sees two typed string arrays rather than an opaque JSON blob, and no new
type was needed. This is the general strategy the skill documents for
array-of-objects inputs; a JSON-carrying `str` arg is the fallback for irregular
/ deeply nested objects.

Illustrative (`text` to keep it out of the schema-validated fence; the real,
validated spec is the file above):

```text
[fs.commands.edit-file.args]
path     = {type = "path", required = true}
old-text = {type = "str", multiple = true, required = true}
new-text = {type = "str", multiple = true, required = true}
dry-run  = {type = "flag", default = false}
```

## Type mapping (MCP JSON Schema → runspec arg)

`string`→`str` (`path` when it names a path); `number`/`integer`→`float`/`int`;
`boolean`→`flag`; `enum`→`choice`+`options`; `array<scalar>`→scalar +
`multiple`; **`array<object>`→one parallel `multiple` arg per field, zipped in
core**; nested/irregular object→a JSON-carrying `str` (fallback);
secret-shaped→`password`.

## Packaging / CI / release

The two packages follow the repo conventions exactly:

- `ci.yml` gains `fs-core-quality`/`fs-core-test` and `fs-quality`/`fs-test`
  (cloned from the `linux-core` / `linux` jobs), wired into the `changes` filter
  and the aggregate `ci:` job. The core test asserts the split's contract (no
  `runspec` requirement, no console scripts); the wrapper test asserts the `fs`
  script runs and `runspec.toml` ships in the wheel.
- `fs-core-release.yml` + `fs-release.yml` mirror the linux release workflows;
  `auto-tag.yml` and `docs/releasing.md` gain both manifests (tag prefixes
  `fs-core-v` / `fs-v`). **Publish `runspec-fs-core` before `runspec-fs`** so the
  `runspec-fs-core>=…` floor resolves on PyPI.

## Using it from runspec-console

`pip install runspec-fs` into the console's venv; the `fs` verbs then surface as
`fs_<tool>` leaf MCP tools in the agent's toolset on every host where it's
installed, reads autonomous and mutations confirm-gated. It is intentionally
**not** in the console's dependency set — it's opt-in, like `runspec-logops`.
