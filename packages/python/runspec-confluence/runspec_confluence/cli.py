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
    cql_from_text,
    create_page,
    delete_attachment,
    delete_page,
    download_attachments,
    download_diagram,
    get_attachments,
    get_comments,
    get_current_user,
    get_diagram,
    get_labels,
    get_page,
    get_page_ancestors,
    get_page_children,
    get_page_history,
    get_page_restrictions,
    get_space,
    get_space_pages,
    html_to_markdown,
    list_spaces,
    move_page,
    page_macros,
    search,
    search_by_label,
    update_page,
    upload_attachment,
)


def _emit(payload: Any) -> None:
    print(json.dumps(payload, default=str))


def _get_page_maybe_markdown(spec: rs.RunSpec, config: ConfluenceConfig) -> Any:
    """Fetch a page; with --markdown, attach a Markdown rendering of its body.

    When --markdown is set and --expand was not given, force ``body.storage`` so
    there is a body to convert. The raw page is returned unchanged plus a top-level
    ``markdown`` field (only when a body is present)."""
    expand = spec.expand.value
    want_markdown = spec.markdown.value
    if want_markdown and not expand:
        expand = "body.storage,version"
    page = get_page(config, page_id=spec.page_id.value, space=spec.space.value, title=spec.title.value, expand=expand)
    if want_markdown and isinstance(page, dict):
        body = page.get("body") or {}
        section = body.get("storage") or body.get("view") or {}
        html = section.get("value") if isinstance(section, dict) else None
        if html:
            return {**page, "markdown": html_to_markdown(html)}
    return page


def _dispatch(spec: rs.RunSpec, config: ConfluenceConfig) -> Any:
    """Run the active verb against ``config``, unpacking only its declared args."""
    command = spec.runspec_command

    # ── reads ──
    if command == "search":
        return search(config, spec.cql.value, start=spec.start.value, limit=spec.limit.value, expand=spec.expand.value)
    if command == "search-text":
        # siteSearch is Cloud-only (relevance-ranked); Server/DC uses text.
        text_field = "siteSearch" if config.cloud else "text"
        cql = cql_from_text(spec.query.value, space_key=spec.space.value, content_type=spec.content_type.value, text_field=text_field)
        return search(config, cql, start=spec.start.value, limit=spec.limit.value)
    if command == "search-by-label":
        return search_by_label(config, spec.label.value, space_key=spec.space.value, start=spec.start.value, limit=spec.limit.value)
    if command == "whoami":
        return get_current_user(config)
    if command == "list-spaces":
        return list_spaces(config, space_type=spec.space_type.value, start=spec.start.value, limit=spec.limit.value)
    if command == "get-page":
        return _get_page_maybe_markdown(spec, config)
    if command == "get-page-children":
        return get_page_children(config, spec.page_id.value, child_type=spec.child_type.value, start=spec.start.value, limit=spec.limit.value)
    if command == "get-page-ancestors":
        return get_page_ancestors(config, spec.page_id.value)
    if command == "get-page-history":
        return get_page_history(config, spec.page_id.value)
    if command == "get-labels":
        return get_labels(config, spec.page_id.value)
    if command == "get-comments":
        return get_comments(config, spec.page_id.value, start=spec.start.value, limit=spec.limit.value)
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
        return get_attachments(config, spec.page_id.value, start=spec.start.value, limit=spec.limit.value, filename=spec.filename.value)
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

    # ── macros + diagrams ──
    if command == "list-macros":
        return page_macros(config, spec.page_id.value)
    if command == "get-diagram":
        return get_diagram(config, spec.page_id.value, name=spec.name.value, include_source=spec.include_source.value)
    if command == "download-diagram":
        return download_diagram(config, spec.page_id.value, str(spec.dest.value), name=spec.name.value)

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
