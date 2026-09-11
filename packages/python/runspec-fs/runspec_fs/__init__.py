"""runspec-fs — filesystem operations as a runspec runnable (the ``filesystem`` runnable).

Ported from the
`Filesystem MCP server <https://github.com/modelcontextprotocol/servers/tree/HEAD/src/filesystem>`_.
The pure logic lives in ``runspec-fs-core`` (importable with **no** runspec
dependency); this package wraps each helper as a verb of the ``filesystem`` runnable and
is what ``runspec local`` / ``runspec serve`` discover.

The core helpers are re-exported here for convenience / back-compat, so a caller
that already depends on ``runspec-fs`` can ``from runspec_fs import
read_text_file`` without also naming ``runspec-fs-core``:
"""

from runspec_fs_core import (
    EditNotAppliedError,
    FsCoreError,
    allowed_directories,
    create_directory,
    directory_tree,
    edit_file,
    get_file_info,
    list_directory,
    list_directory_with_sizes,
    move_file,
    read_media_file,
    read_multiple_files,
    read_text_file,
    search_files,
    write_file,
)

__all__ = [
    "FsCoreError",
    "EditNotAppliedError",
    "read_text_file",
    "read_media_file",
    "read_multiple_files",
    "write_file",
    "edit_file",
    "create_directory",
    "list_directory",
    "list_directory_with_sizes",
    "directory_tree",
    "move_file",
    "search_files",
    "get_file_info",
    "allowed_directories",
]
