"""cli.py — the ``fs`` entry point.

One binary, a verb per Filesystem MCP tool. ``rs.parse("fs")`` resolves the
subcommand and its args; we dispatch on ``spec.runspec_command``, unpack each
``.value`` at the boundary, and delegate the actual work to
:mod:`runspec_fs_core`. Failures (the ``runspec_fs_core`` errors, the stdlib
``OSError`` family, and arg-shape ``ValueError``s such as an ``--old-text`` /
``--new-text`` count mismatch) become a JSON ``{"error": ...}`` payload + a
non-zero exit — the shape the CLI/agent surface expects.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import runspec as rs
from runspec_fs_core import (
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
from runspec_fs_core.errors import FsCoreError


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, default=str))


def _pair_edits(old_text: list[str], new_text: list[str]) -> list[tuple[str, str]]:
    """Zip the two parallel ``--old-text`` / ``--new-text`` lists into
    ``(old, new)`` pairs, erroring if their counts differ."""
    if len(old_text) != len(new_text):
        raise ValueError(f"--old-text / --new-text count mismatch: {len(old_text)} vs {len(new_text)} (pair them by position)")
    return list(zip(old_text, new_text, strict=False))


def _dispatch(spec: rs.RunSpec) -> dict[str, Any]:
    command = spec.runspec_command

    if command == "read-text-file":
        return read_text_file(spec.path.value, head=spec.head.value, tail=spec.tail.value)
    if command == "read-media-file":
        return read_media_file(spec.path.value)
    if command == "read-multiple-files":
        return read_multiple_files(spec.paths.value)
    if command == "list-directory":
        return list_directory(spec.path.value)
    if command == "list-directory-with-sizes":
        return list_directory_with_sizes(spec.path.value, sort_by=spec.sort_by.value)
    if command == "directory-tree":
        return directory_tree(spec.path.value, exclude=spec.exclude.value or None)
    if command == "search-files":
        return search_files(spec.path.value, spec.pattern.value, exclude=spec.exclude.value or None)
    if command == "get-file-info":
        return get_file_info(spec.path.value)
    if command == "list-allowed-directories":
        return allowed_directories()
    if command == "write-file":
        return write_file(spec.path.value, spec.content.value)
    if command == "edit-file":
        edits = _pair_edits(spec.old_text.value, spec.new_text.value)
        return edit_file(spec.path.value, edits, dry_run=spec.dry_run.value)
    if command == "create-directory":
        return create_directory(spec.path.value)
    if command == "move-file":
        return move_file(spec.source.value, spec.destination.value)

    # require-command = true makes a bare invocation unreachable.
    raise SystemExit("no command — see `fs --help`")  # pragma: no cover


def main() -> None:
    spec = rs.parse("fs")
    try:
        result = _dispatch(spec)
    except (FsCoreError, OSError, ValueError) as exc:
        _emit({"error": str(exc), "kind": type(exc).__name__})
        sys.exit(1)
    _emit(result)


if __name__ == "__main__":
    main()
