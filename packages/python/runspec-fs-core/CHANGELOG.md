# runspec-fs-core Changelog

## [0.1.0] — 2026-07-25

Initial release.

The pure-Python logic core behind `runspec-fs` — a stdlib port of the
[Filesystem MCP server](https://github.com/modelcontextprotocol/servers/tree/HEAD/src/filesystem)
tool set. Provides the filesystem helpers as plain importable functions — **no
dependency on `runspec`, no `runspec.toml`, no console-script entry points** — so
a package can `from runspec_fs_core import read_text_file` (and the rest) without
surfacing any runnables in the venv or in `runspec local` / `runspec serve`
discovery.

Each function returns plain data and *raises* on failure — the stdlib `OSError`
family (`FileNotFoundError`, `NotADirectoryError`, `PermissionError`,
`FileExistsError`) for filesystem problems, plus `EditNotAppliedError` when an
`edit_file` old-text block is not found; the runnable wrappers in `runspec-fs`
catch these and render the JSON/exit behaviour.

No directory jail is enforced: filesystem access is bounded by the OS permissions
of the user the process runs as. `allowed_directories()` reports the effective
user + home/cwd and is informational only.

Exports:

- **Read** — `read_text_file` (head/tail), `read_media_file` (base64 + MIME),
  `read_multiple_files` (per-file errors)
- **Write / edit** — `write_file`, `edit_file` (ordered substring replacements,
  unified diff, `dry_run`)
- **Directories** — `create_directory`, `list_directory`,
  `list_directory_with_sizes` (name/size sort), `directory_tree`, `move_file`
- **Search / info** — `search_files` (glob or substring, exclude globs),
  `get_file_info`, `allowed_directories`
- **Errors** — `FsCoreError`, `EditNotAppliedError`
