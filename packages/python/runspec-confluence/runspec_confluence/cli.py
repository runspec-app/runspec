"""cli.py — the ``confluence`` entry point.

One binary, a verb per Confluence tool. ``rs.parse("confluence")`` resolves the
subcommand and its args; we build a :class:`ConfluenceConfig` from the environment
(scoped by the global ``--env``), unpack each ``.value`` at the boundary, and call
the matching :mod:`runspec_confluence_core` helper.

Failures (the core :class:`ConfluenceError` and arg-shape ``ValueError``s) become a
JSON ``{"error": ...}`` payload + a non-zero exit.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import runspec as rs
from runspec_confluence_core import (
    ConfluenceConfig,
    ConfluenceError,
    add_comment,
    add_label,
    config_from_env,
    create_page,
    delete_attachment,
    delete_page,
    download_attachments,
    get_attachments,
    get_comments,
    get_labels,
    get_page,
    get_page_ancestors,
    get_page_children,
    get_page_history,
    get_page_restrictions,
    get_space,
    get_space_pages,
    move_page,
    search,
    update_page,
    upload_attachment,
)


def _emit(payload: Any) -> None:
    print(json.dumps(payload, default=str))


def _dispatch(spec: rs.RunSpec, config: ConfluenceConfig) -> Any:
    """Run the active verb against ``config``, unpacking only its declared args."""
    command = spec.runspec_command

    # ── reads ──
    if command == "search":
        return search(config, spec.cql.value, start=spec.start.value, limit=spec.limit.value, expand=spec.expand.value)
    if command == "get-page":
        return get_page(config, page_id=spec.page_id.value, space=spec.space.value, title=spec.title.value, expand=spec.expand.value)
    if command == "get-page-children":
        return get_page_children(config, spec.page_id.value)
    if command == "get-page-ancestors":
        return get_page_ancestors(config, spec.page_id.value)
    if command == "get-page-history":
        return get_page_history(config, spec.page_id.value)
    if command == "get-labels":
        return get_labels(config, spec.page_id.value)
    if command == "get-comments":
        return get_comments(config, spec.page_id.value, limit=spec.limit.value)
    if command == "get-page-restrictions":
        return get_page_restrictions(config, spec.page_id.value)
    if command == "get-space":
        return get_space(config, spec.space_key.value)
    if command == "get-space-pages":
        return get_space_pages(config, spec.space.value, start=spec.start.value, limit=spec.limit.value)

    # ── writes ──
    if command == "create-page":
        return create_page(
            config,
            spec.space.value,
            spec.title.value,
            spec.content.value,
            parent_id=spec.parent_id.value,
            representation=spec.content_format.value,
        )
    if command == "update-page":
        return update_page(
            config,
            spec.page_id.value,
            spec.title.value,
            body=spec.content.value,
            parent_id=spec.parent_id.value,
            representation=spec.content_format.value,
            version_comment=spec.version_comment.value,
            minor_edit=spec.minor_edit.value,
        )
    if command == "delete-page":
        return delete_page(config, spec.page_id.value, recursive=spec.recursive.value)
    if command == "move-page":
        return move_page(
            config,
            spec.space.value,
            spec.page_id.value,
            target_id=spec.target_id.value,
            target_title=spec.target_title.value,
            position=spec.position.value,
        )
    if command == "add-label":
        return add_label(config, spec.page_id.value, spec.label.value)
    if command == "add-comment":
        return add_comment(config, spec.page_id.value, spec.text.value)

    # ── attachments ──
    if command == "get-attachments":
        return get_attachments(config, spec.page_id.value, limit=spec.limit.value, filename=spec.filename.value)
    if command == "download-attachments":
        return download_attachments(config, spec.page_id.value, str(spec.dest.value))
    if command == "upload-attachment":
        return upload_attachment(
            config,
            spec.page_id.value,
            str(spec.file.value),
            name=spec.name.value,
            content_type=spec.content_type.value,
            comment=spec.comment.value,
        )
    if command == "delete-attachment":
        return delete_attachment(config, spec.page_id.value, spec.filename.value)

    # require-command = true makes a bare invocation unreachable.
    raise SystemExit("no command — see `confluence --help`")  # pragma: no cover


def main() -> None:
    spec = rs.parse("confluence")
    try:
        config = config_from_env(env=spec.env.value, personal_token_override=spec.confluence_pat.value)
        result = _dispatch(spec, config)
    except (ConfluenceError, ValueError) as exc:
        _emit({"error": str(exc), "kind": type(exc).__name__})
        sys.exit(1)
    _emit(result)


if __name__ == "__main__":
    main()
