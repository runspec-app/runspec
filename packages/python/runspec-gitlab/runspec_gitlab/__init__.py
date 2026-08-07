"""runspec-gitlab — GitLab reads as a runspec runnable (the ``gitlab`` runnable).

Ported from the
`GitLab MCP server <https://docs.gitlab.com/user/model_context_protocol/mcp_server/>`_.
The pure REST logic lives in ``runspec-gitlab-core`` (importable with **no**
runspec dependency); this package wraps each helper as a verb of the ``gitlab``
runnable and is what ``runspec local`` / ``runspec serve`` discover.

The core helpers are re-exported here for convenience / back-compat, so a caller
that already depends on ``runspec-gitlab`` can ``from runspec_gitlab import
list_issues`` without also naming ``runspec-gitlab-core``.
"""

from runspec_gitlab_core import (
    GitlabConfig,
    GitlabError,
    config_from_env,
    encode_project,
    get_file,
    get_issue,
    get_job_log,
    get_merge_request,
    get_merge_request_commits,
    get_merge_request_conflicts,
    get_merge_request_diffs,
    get_merge_request_notes,
    get_merge_request_pipelines,
    get_pipeline_jobs,
    get_project,
    gitlab_request,
    list_issues,
    list_merge_requests,
    list_pipelines,
    list_tree,
    search_projects,
    whoami,
)

__all__ = [
    "GitlabConfig",
    "GitlabError",
    "config_from_env",
    "gitlab_request",
    "encode_project",
    "whoami",
    "get_project",
    "search_projects",
    "list_issues",
    "get_issue",
    "list_merge_requests",
    "get_merge_request",
    "get_merge_request_diffs",
    "get_merge_request_commits",
    "get_merge_request_notes",
    "get_merge_request_pipelines",
    "get_merge_request_conflicts",
    "list_pipelines",
    "get_pipeline_jobs",
    "get_job_log",
    "get_file",
    "list_tree",
]
