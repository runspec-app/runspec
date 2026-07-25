# runspec-fs-core Changelog

## [0.2.0] — 2026-07-25

Add `become.py` — the pure privilege-escalation command builder behind the
`fs` runnable's per-invocation `--run-as`, mirroring `runspec-linux`'s venv
runnables:

- **`build_become(argv, run_as, method="sudo")`** — wrap `argv` so it runs as
  `run_as` (`sudo -n -u <user> …` or `su <user> -c '…'`); returns `argv`
  unchanged when `run_as` is empty. `-n` never prompts, so a missing
  passwordless-sudo rule fails fast instead of hanging on a TTY-less channel.
- **`validate_user`** (rejects anything but a POSIX-ish user name, so a crafted
  value can't inject arguments), **`current_user`** (effective user name, to
  decide whether escalation is even needed), **`escalation_supported`**
  (`hasattr(os, "geteuid")` — false on Windows), `BECOME_METHODS`, `BecomeError`.

Stdlib-only; the builder is pure (execution stays with the caller).

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
