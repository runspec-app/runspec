# runspec-fs Changelog

## [0.4.0] — 2026-09-17

### Added — `directory-size` verb (folder-size disk-usage triage)

- New `filesystem directory-size --path <dir> [--limit N] [--depth N]` verb: report the
  recursive total size of each immediate child of a directory, largest first —
  the "which folder is eating the drive" view (like `du -d1 | sort`). Unlike
  `list-directory-with-sizes` (which shows `0` for a folder entry), each
  subdirectory's size is the total of everything beneath it, so an agent
  investigating a full drive can find the culprit folder and drill down.
  `--depth N` (default 1) nests the breakdown that many levels deep in one
  call (each folder carries its own sorted `children`), so several levels can
  be seen without a call per level.
  Autonomous (read-only). `runspec serve` flattens it to a
  `filesystem_directory-size` leaf MCP tool. `--path` is a directory arg
  (`ui = "directory"`), so the console Forms tab renders a folder picker.
- Backed by the new `runspec_fs_core.directory_size` helper (pin bumped to
  `runspec-fs-core>=0.3.0`).

## [0.3.0] — 2026-09-11

Rename the runnable (and its console-script entry point) from `fs` to
**`filesystem`** to make its purpose clearer at a glance.

- **Breaking:** the runnable section in `runspec.toml` is now `[filesystem]`
  (with `[filesystem.args]` / `[filesystem.commands.*]`), and the installed
  binary / entry point is `filesystem` (was `fs`). Invoke it as
  `filesystem list-directory …`.
- `runspec serve` now flattens the verbs into `filesystem_<verb>` leaf MCP tools
  (e.g. `filesystem_read-text-file`), where they were `fs_<verb>`. Agents /
  configs referencing the old `fs_*` tool names must update.
- No behaviour change — every verb, argument, and the `--run-as` /
  `--become-method` globals are unchanged; only the runnable name moved. The
  import package stays `runspec_fs` (and the logic core `runspec-fs-core`).

## [0.2.0] — 2026-07-25

Add a per-invocation **`--run-as`** — two inherited global args on `fs`, so the
user *or* the agent picks which OS user each verb runs as (mirroring
`runspec-linux`'s venv runnables, which take a runtime `--run-as` rather than a
declarative `run_as`):

- `--run-as <user>` — run the verb as another OS user.
- `--become-method sudo|su` (default `sudo`) — how to escalate.

Because the fs verbs run in-process (unlike the linux venv ops, which are already
subprocesses), the resolved `(verb, kwargs)` is shipped over stdin to a small
worker (`runspec_fs._worker`) re-invoked as the target user via
`runspec_fs_core.build_become` (`sudo -n -u <user>` / `su <user> -c`). The worker
imports only the pure core, so the escalated user needs no `runspec.toml` lookup.
Access stays bounded by that user's OS permissions — still no directory jail. The
args surface on every leaf MCP tool via `runspec serve`, so the console agent can
set them per call (needs `runspec-fs-core >= 0.2.0`).

Internals: verb→core dispatch moved to a runspec-free `runspec_fs._dispatch`
(shared by `cli.py` and the worker); `cli.py` now unpacks the spec into a plain
kwargs dict via `_verb_kwargs`.

## [0.1.0] — 2026-07-25

Initial release.

A single runnable, `fs`, with a verb per tool of the
[Filesystem MCP server](https://github.com/modelcontextprotocol/servers/tree/HEAD/src/filesystem)
— read / write / edit / list / tree / search / move / stat. `runspec serve`
re-expands each verb into its own leaf MCP tool (`fs_read-text-file`,
`fs_write-file`, …), so a runspec-console agent can call them individually over
SSH on any fleet host — giving it local **and** remote filesystem access with
per-verb confirm gates and no extra process on the host.

- **Reads (autonomous)** — `read-text-file` (`--head` / `--tail`),
  `read-media-file` (base64 + MIME), `read-multiple-files`, `list-directory`,
  `list-directory-with-sizes` (`--sort-by name|size`), `directory-tree`
  (`--exclude`), `search-files` (glob or substring, `--exclude`), `get-file-info`,
  `list-allowed-directories`.
- **Mutations (confirm)** — `write-file`, `edit-file`, `create-directory`,
  `move-file`.

`edit-file` takes its edits as two parallel repeatable args — `--old-text` and
`--new-text`, paired by position — rather than a JSON blob, so agents pass two
typed string arrays (no new runspec arg type was needed).

**No directory jail** — filesystem access is bounded by the OS permissions of the
user the process runs as (the logged-in user, or a `run_as` service account).
The pure logic lives in `runspec-fs-core` (no runspec dependency), re-exported
here for corporate-wrapping.
