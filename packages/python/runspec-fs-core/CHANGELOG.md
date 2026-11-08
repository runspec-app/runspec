# runspec-fs-core Changelog

## [0.6.0] — 2026-11-08

### Added

- **Secret placeholders.** New `secrets` module: text bound for a file may
  hold `{{secret:<credential>}}` / `{{secret:<credential>.username}}`,
  filled from `RUNSPEC_SECRET_<NAME>` / `RUNSPEC_SECRET_<NAME>__USERNAME`
  (`fill_secrets`, `fill_verb_secrets`). Only `RUNSPEC_SECRET_*` variables are
  read, so a placeholder can't copy another environment variable into a file;
  a placeholder with no value raises `SecretNotAvailableError` and nothing is
  written. `mask_secrets` / `mask_text` turn filled values back into their
  placeholder in anything printed (also JSON-escaped). `edit-file` fills only
  new text, so an edit can't test whether a file holds a secret.
- **File modes.** `write_file` / `edit_file` take `mode` (applied before the
  new text goes in) and `private`: a new private file is created `0600` from the
  first byte (`os.open` with the mode, no window where it's world-readable); an
  existing file keeps its owner and mode. Results carry `mode`, plus a
  `warning` when a file holding a secret is readable by every user.

## [0.5.0] — 2026-09-26

### Added

- **`directory_size` says when its totals are incomplete.** A scan as a normal
  user used to skip unreadable directories silently, reporting only a
  `skipped` count, so a scan of `/` could miss most of the disk without it
  showing (e.g. Docker volumes under `/var/lib/docker` counted as 0). Now a
  directory that couldn't be opened is listed with `unreadable: true` (its size
  a floor of 0), the first 10 such paths are named in a top-level
  `unreadable` list, and a `hint` says the totals are a lower bound and to
  re-run as root (`--run-as root`), or elevated on Windows. No hint when the
  scan already ran as root.

## [0.4.0] — 2026-09-26

### Changed

- **`directory_size` stays on one filesystem**, like `du -x`, instead of recursing
  across every mount below `path`. A scan of `/` walked `/proc` and counted
  `/proc/kcore` (~128 TiB) into the total. A subdirectory on another filesystem
  now counts as 0, is listed with `skipped_mount: true`, and is tallied in the new
  `skipped_mounts`.
- **New `cross_mounts=True`** measures other mounted filesystems too (a separate
  disk, a network share). Kernel virtual filesystems (`proc`, `sysfs`,
  `devtmpfs`, `cgroup2` …) are never measured, and a `path` on one raises
  `ValueError`. On Windows, a volume mounted at a folder, or a junction onto
  another volume, counts as another filesystem.
- The guard is `runspec_fs_core._mounts`, a byte-for-byte twin of
  `runspec_linux_core._mounts` (each core stays dependency-free) kept identical by
  a parity test.

## [0.3.0] — 2026-09-17

Add **`directory_size`** — the recursive folder-size helper for disk-usage
triage (the "which folder is eating the drive" view, like `du -d1 | sort`):

- **`directory_size(path, *, limit=20)`** — report the recursive total size of
  each immediate child of a directory, largest first. Unlike
  `list_directory_with_sizes` (which reports `0` for a directory entry), every
  subdirectory's `size` is the total of all files beneath it, so an agent
  investigating a full drive can spot the culprit folder and drill down.
- Symlinks are not followed (link targets are never double-counted or looped),
  `total_size` stays the true total even when `entries` is truncated to the top
  `limit` (`limit <= 0` returns all), and unreadable entries (permissions,
  races) are skipped and tallied under `skipped` rather than failing the scan.
- Raises `NotADirectoryError` when pointed at a file. Stdlib-only (`os.scandir`).

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
