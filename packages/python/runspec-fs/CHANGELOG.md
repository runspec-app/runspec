# runspec-fs Changelog

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
