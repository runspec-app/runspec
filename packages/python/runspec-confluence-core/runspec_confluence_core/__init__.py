"""runspec-confluence-core — Confluence helpers over atlassian-python-api.

This package depends only on ``atlassian-python-api`` — it has **no dependency on
runspec** and ships **no runspec.toml and no entry points**, so installing it
exposes the helper functions for import without surfacing any runnables (it is
invisible to ``runspec local`` / ``runspec serve`` discovery). ``runspec-confluence``
depends on it and wraps each helper as a verb of the ``confluence`` runnable.

Every operation takes an explicit :class:`ConfluenceConfig` first argument, so a
private (e.g. Nexus-hosted) wrapper package can inject credentials in code, bake in
a default ``space`` / ``env``, and build on top. Each function returns plain data
and raises :class:`ConfluenceError` on failure. Cloud and Server/Data Center are
both supported.
"""

from runspec_confluence_core.attachments import (
    delete_attachment,
    download_attachments,
    get_attachments,
    upload_attachment,
)
from runspec_confluence_core.client import build_client, translate_errors
from runspec_confluence_core.config import (
    DEFAULT_MAX_BACKOFF_RETRIES,
    DEFAULT_MAX_BACKOFF_SECONDS,
    VALID_ENVS,
    ConfluenceConfig,
    config_from_env,
    detect_verify,
)
from runspec_confluence_core.content import (
    add_comment,
    add_label,
    get_comments,
    get_labels,
    get_page_history,
    get_page_restrictions,
)
from runspec_confluence_core.diagrams import (
    decode_drawio,
    decode_gliffy,
    download_diagram,
    get_diagram,
    page_macros,
)
from runspec_confluence_core.errors import ConfluenceError
from runspec_confluence_core.macros import list_macros, macro_kind, storage_to_html
from runspec_confluence_core.mdconvert import markdown_to_storage
from runspec_confluence_core.pages import (
    create_page,
    delete_page,
    get_page,
    get_page_ancestors,
    get_page_children,
    move_page,
    update_page,
)
from runspec_confluence_core.search import cql_from_text, search, search_by_label
from runspec_confluence_core.spaces import get_space, get_space_pages, list_spaces
from runspec_confluence_core.summarise import (
    html_to_markdown,
    simplify_attachment,
    simplify_comment,
    simplify_page,
    simplify_space,
    simplify_user,
)
from runspec_confluence_core.users import get_current_user

__all__ = [
    # config / client
    "ConfluenceConfig",
    "ConfluenceError",
    "config_from_env",
    "detect_verify",
    "build_client",
    "translate_errors",
    "VALID_ENVS",
    "DEFAULT_MAX_BACKOFF_RETRIES",
    "DEFAULT_MAX_BACKOFF_SECONDS",
    # pages
    "get_page",
    "get_page_children",
    "get_page_ancestors",
    "create_page",
    "update_page",
    "delete_page",
    "move_page",
    # spaces
    "list_spaces",
    "get_space_pages",
    "get_space",
    # users
    "get_current_user",
    # search
    "search",
    "search_by_label",
    "cql_from_text",
    # content
    "get_page_history",
    "get_labels",
    "add_label",
    "get_comments",
    "add_comment",
    "get_page_restrictions",
    # attachments
    "get_attachments",
    "download_attachments",
    "upload_attachment",
    "delete_attachment",
    # macros + diagrams
    "list_macros",
    "macro_kind",
    "storage_to_html",
    "page_macros",
    "get_diagram",
    "download_diagram",
    "decode_drawio",
    "decode_gliffy",
    # markdown (write) / summarise (read)
    "markdown_to_storage",
    "html_to_markdown",
    "simplify_page",
    "simplify_comment",
    "simplify_space",
    "simplify_user",
    "simplify_attachment",
]
