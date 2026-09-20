# runspec-fs

A filesystem runnable for [runspec](https://pypi.org/project/runspec/), ported
from the [Filesystem MCP server](https://github.com/modelcontextprotocol/servers/tree/HEAD/src/filesystem).
One runnable — **`filesystem`** — with a verb per MCP tool. `pip install` it
into a venv and it's discoverable by `runspec local`, `runspec serve` (MCP), and
runspec-console; over the console's SSH fleet it gives the agent local **and**
remote filesystem access with no extra server process.

`runspec serve` flattens each verb into its own leaf MCP tool
(`filesystem_read-text-file` … `filesystem_move-file`). Read verbs are
`autonomy = "autonomous"`; the mutating ones (`write-file`, `edit-file`,
`create-directory`, `move-file`) are `autonomy = "confirm"`.

> **No directory jail.** Access is bounded by the OS permissions of the invoking
> (or `--run-as`) user, not by an allowlist.

## Install

```
pip install runspec-fs
```

## Verbs

| Kind | Verbs |
|---|---|
| Read | `read-text-file`, `read-media-file`, `read-multiple-files`, `list-directory`, `list-directory-with-sizes`, `directory-size`, `directory-tree`, `search-files`, `get-file-info`, `list-allowed-directories` |
| Write | `write-file`, `edit-file`, `create-directory`, `move-file` |

`edit-file` takes paired repeatable `--old-text` / `--new-text` args (zipped by
position) instead of a JSON blob, so it needs no new runspec arg type.

```
filesystem list-directory --path /var/log
filesystem read-text-file --path /etc/hostname
filesystem edit-file --path app.conf --old-text "debug = true" --new-text "debug = false"
```

### Run as another user

Two inherited global args — `--run-as <user>` (+ `--become-method sudo|su`) —
pick which OS user each verb runs as. Because the verbs run in-process, the
resolved `(verb, kwargs)` is shipped over stdin to a worker re-invoked as that
user (via `sudo -n -u` / `su`); the option surfaces on every leaf MCP tool.

## Public Python API

The pure logic is in **[`runspec-fs-core`](../runspec-fs-core)** — stdlib only,
no `runspec` dependency, no entry points:

```python
from runspec_fs_core import read_text_file, write_file, edit_file, directory_tree
```

Failures raise (the stdlib `OSError` family, plus `EditNotAppliedError`).

## Development

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest
```
