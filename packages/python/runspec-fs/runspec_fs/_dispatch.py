"""Verb → core-helper dispatch, importing only ``runspec_fs_core`` (no runspec).

Shared by ``cli.py`` (the in-process path) and ``_worker.py`` (the ``--run-as``
path), so both map a verb name + a plain-JSON kwargs dict to the same core
helper. Keeping it runspec-free means the worker — which runs as a *different* OS
user under sudo/su — needs only the pure core on its import path, not runspec.
"""

from __future__ import annotations

from typing import Any

from runspec_fs_core import (
    allowed_directories,
    create_directory,
    directory_size,
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


def run_verb(verb: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Run ``verb`` with plain ``kwargs`` (already unpacked from the parsed spec,
    JSON-round-trip safe). Raises on failure like the core helpers do."""
    if verb == "read-text-file":
        return read_text_file(kwargs["path"], head=kwargs.get("head"), tail=kwargs.get("tail"))
    if verb == "read-media-file":
        return read_media_file(kwargs["path"])
    if verb == "read-multiple-files":
        return read_multiple_files(kwargs["paths"])
    if verb == "list-directory":
        return list_directory(kwargs["path"])
    if verb == "list-directory-with-sizes":
        return list_directory_with_sizes(kwargs["path"], sort_by=kwargs["sort_by"])
    if verb == "directory-size":
        return directory_size(kwargs["path"], limit=kwargs["limit"], depth=kwargs["depth"])
    if verb == "directory-tree":
        return directory_tree(kwargs["path"], exclude=kwargs.get("exclude"))
    if verb == "search-files":
        return search_files(kwargs["path"], kwargs["pattern"], exclude=kwargs.get("exclude"))
    if verb == "get-file-info":
        return get_file_info(kwargs["path"])
    if verb == "list-allowed-directories":
        return allowed_directories()
    if verb == "write-file":
        return write_file(kwargs["path"], kwargs["content"])
    if verb == "edit-file":
        edits = [tuple(pair) for pair in kwargs["edits"]]
        return edit_file(kwargs["path"], edits, dry_run=kwargs["dry_run"])
    if verb == "create-directory":
        return create_directory(kwargs["path"])
    if verb == "move-file":
        return move_file(kwargs["source"], kwargs["destination"])
    raise ValueError(f"unknown verb: {verb!r}")
