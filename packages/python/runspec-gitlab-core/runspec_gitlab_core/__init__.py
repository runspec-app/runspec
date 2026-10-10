"""runspec-gitlab-core — pure-Python GitLab REST (v4) read + write helpers.

This package depends only on ``httpx`` — it has **no dependency on runspec** and
ships **no runspec.toml and no entry points**, so installing it exposes the
helper functions for import without surfacing any runnables (it is invisible to
``runspec local`` / ``runspec serve`` discovery). ``runspec-gitlab`` depends on it
and wraps each helper as a verb of the ``gitlab`` runnable.

The helpers port the tools of GitLab's own (hosted) MCP server onto the public
REST API, plus REST reads and writes it has no tool for. Every operation takes an explicit :class:`GitlabConfig` first
argument, so a private (e.g. Nexus-hosted) wrapper package can inject credentials
in code, bake in a default ``project`` / ``env``, and build "one runnable per
project" on top without re-implementing the REST plumbing. Each function does the
work and returns plain data; failures raise :class:`GitlabError`.
"""

from runspec_gitlab_core.client import (
    DEFAULT_URL,
    VALID_ENVS,
    GitlabConfig,
    GitlabError,
    config_from_env,
    encode_project,
    gitlab_request,
)
from runspec_gitlab_core.issues import (
    add_issue_note,
    create_issue,
    delete_issue,
    get_issue,
    list_issues,
    update_issue,
)
from runspec_gitlab_core.merge_requests import (
    add_merge_request_note,
    create_merge_request,
    get_merge_request,
    get_merge_request_commits,
    get_merge_request_conflicts,
    get_merge_request_diffs,
    get_merge_request_notes,
    get_merge_request_pipelines,
    list_merge_requests,
    merge_merge_request,
    update_merge_request,
)
from runspec_gitlab_core.pipelines import (
    cancel_job,
    cancel_pipeline,
    create_pipeline,
    delete_pipeline,
    get_job_log,
    get_pipeline_jobs,
    list_pipelines,
    play_job,
    retry_job,
    retry_pipeline,
)
from runspec_gitlab_core.projects import get_project, resolve_project_path, resolve_user_ids, search_projects, whoami
from runspec_gitlab_core.repository import (
    create_branch,
    delete_branch,
    delete_file,
    get_file,
    list_tree,
    write_file,
)

__all__ = [
    # client / config
    "GitlabConfig",
    "GitlabError",
    "config_from_env",
    "gitlab_request",
    "encode_project",
    "resolve_project_path",
    "resolve_user_ids",
    "VALID_ENVS",
    "DEFAULT_URL",
    # identity / projects
    "whoami",
    "get_project",
    "search_projects",
    # issues
    "list_issues",
    "get_issue",
    "create_issue",
    "update_issue",
    "add_issue_note",
    "delete_issue",
    # merge requests
    "list_merge_requests",
    "get_merge_request",
    "get_merge_request_diffs",
    "get_merge_request_commits",
    "get_merge_request_notes",
    "get_merge_request_pipelines",
    "get_merge_request_conflicts",
    "create_merge_request",
    "update_merge_request",
    "add_merge_request_note",
    "merge_merge_request",
    # pipelines / jobs
    "list_pipelines",
    "get_pipeline_jobs",
    "get_job_log",
    "create_pipeline",
    "retry_pipeline",
    "cancel_pipeline",
    "retry_job",
    "cancel_job",
    "play_job",
    "delete_pipeline",
    # repository
    "get_file",
    "list_tree",
    "create_branch",
    "write_file",
    "delete_branch",
    "delete_file",
]
