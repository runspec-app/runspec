"""runspec-confluence — a Confluence runnable (pages, search, comments, labels,
restrictions) over ``atlassian-python-api``, on Cloud and Server/Data Center.

The logic lives in the dependency-light ``runspec-confluence-core``; this package
is the thin ``confluence`` runnable wrapper. The core helpers are re-exported here
for back-compat so ``from runspec_confluence import get_page`` works without naming
the core package.
"""

from runspec_confluence_core import (
    ConfluenceConfig,
    ConfluenceError,
    add_comment,
    add_label,
    config_from_env,
    create_page,
    delete_page,
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
)

__all__ = [
    "ConfluenceConfig",
    "ConfluenceError",
    "config_from_env",
    "get_page",
    "get_page_children",
    "get_page_ancestors",
    "create_page",
    "update_page",
    "delete_page",
    "move_page",
    "get_space_pages",
    "get_space",
    "search",
    "get_page_history",
    "get_labels",
    "add_label",
    "get_comments",
    "add_comment",
    "get_page_restrictions",
]
