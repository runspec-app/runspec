"""runspec-fs-core — pure-Python filesystem helpers.

This package has **no dependency on runspec** and ships **no runspec.toml and no
entry points**, so installing it exposes the helper functions for import without
surfacing any runnables (it is invisible to ``runspec local`` / ``runspec serve``
discovery). ``runspec-fs`` depends on it and wraps each helper as a verb of the
``fs`` runnable.

The helpers are a stdlib port of the
`Filesystem MCP server <https://github.com/modelcontextprotocol/servers/tree/HEAD/src/filesystem>`_
tool set. Each function does the work and returns plain data; failures raise —
the stdlib ``OSError`` family (``FileNotFoundError``, ``NotADirectoryError``,
``PermissionError`` …) for filesystem problems, plus
``runspec_fs_core.errors.EditNotAppliedError`` for a failed ``edit_file``.

No directory jail is enforced: filesystem access is bounded by the OS permissions
of the user the process runs as. See ``allowed_directories``.
"""

from runspec_fs_core.become import (
    BECOME_METHODS,
    BecomeError,
    build_become,
    current_user,
    escalation_supported,
    validate_user,
)
from runspec_fs_core.dirs import (
    create_directory,
    directory_tree,
    list_directory,
    list_directory_with_sizes,
    move_file,
)
from runspec_fs_core.errors import EditNotAppliedError, FsCoreError
from runspec_fs_core.files import (
    edit_file,
    read_media_file,
    read_multiple_files,
    read_text_file,
    write_file,
)
from runspec_fs_core.info import allowed_directories, get_file_info
from runspec_fs_core.search import search_files

__all__ = [
    # errors
    "FsCoreError",
    "EditNotAppliedError",
    # run-as / privilege escalation
    "build_become",
    "validate_user",
    "current_user",
    "escalation_supported",
    "BECOME_METHODS",
    "BecomeError",
    # read
    "read_text_file",
    "read_media_file",
    "read_multiple_files",
    # write / edit
    "write_file",
    "edit_file",
    # directories
    "create_directory",
    "list_directory",
    "list_directory_with_sizes",
    "directory_tree",
    "move_file",
    # search / info
    "search_files",
    "get_file_info",
    "allowed_directories",
]
