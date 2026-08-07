"""runspec-jira-core — Jira Software/Core + Service Desk helpers over atlassian-python-api.

This package depends only on ``atlassian-python-api`` — it has **no dependency on
runspec** and ships **no runspec.toml and no entry points**, so installing it
exposes the helper functions for import without surfacing any runnables (it is
invisible to ``runspec local`` / ``runspec serve`` discovery). ``runspec-jira``
depends on it and wraps each helper as a verb of the ``jira`` runnable.

Every operation takes an explicit :class:`JiraConfig` first argument, so a private
(e.g. Nexus-hosted) wrapper package can inject credentials in code, bake in a
default ``service_desk_id`` / ``project_key`` / ``env``, and build "one runnable
per project" on top. Each function returns plain data and raises
:class:`JiraError` on failure. Cloud and Server/Data Center are both supported.
"""

from runspec_jira_core.client import (
    build_client,
    build_servicedesk,
    parse_json_arg,
    translate_errors,
)
from runspec_jira_core.comments import add_comment, edit_comment
from runspec_jira_core.config import VALID_ENVS, JiraConfig, config_from_env
from runspec_jira_core.errors import JiraError
from runspec_jira_core.fields import search_fields
from runspec_jira_core.issues import (
    assign_issue,
    create_issue,
    delete_issue,
    get_issue,
    update_issue,
)
from runspec_jira_core.links import (
    create_issue_link,
    create_remote_issue_link,
    get_link_types,
    link_to_epic,
    remove_issue_link,
)
from runspec_jira_core.projects import (
    get_all_projects,
    get_create_fields,
    get_project_components,
    get_project_issue_types,
    search_projects,
)
from runspec_jira_core.search import get_project_issues, jql_search
from runspec_jira_core.servicedesk import (
    create_customer_request,
    get_request,
    list_request_type_fields,
    list_request_types,
)
from runspec_jira_core.transitions import list_transitions, transition_issue
from runspec_jira_core.users import get_user_profile, search_assignable_users
from runspec_jira_core.watchers import add_watcher, get_watchers, remove_watcher
from runspec_jira_core.worklog import add_worklog, get_worklog

__all__ = [
    # config / client
    "JiraConfig",
    "JiraError",
    "config_from_env",
    "build_client",
    "build_servicedesk",
    "parse_json_arg",
    "translate_errors",
    "VALID_ENVS",
    # issues
    "get_issue",
    "create_issue",
    "update_issue",
    "assign_issue",
    "delete_issue",
    # search
    "jql_search",
    "get_project_issues",
    # transitions
    "list_transitions",
    "transition_issue",
    # comments
    "add_comment",
    "edit_comment",
    # links
    "get_link_types",
    "create_issue_link",
    "link_to_epic",
    "create_remote_issue_link",
    "remove_issue_link",
    # worklog
    "get_worklog",
    "add_worklog",
    # projects
    "get_all_projects",
    "search_projects",
    "get_project_components",
    "get_project_issue_types",
    "get_create_fields",
    # fields
    "search_fields",
    # users
    "get_user_profile",
    "search_assignable_users",
    # watchers
    "get_watchers",
    "add_watcher",
    "remove_watcher",
    # service desk
    "list_request_types",
    "list_request_type_fields",
    "create_customer_request",
    "get_request",
]
