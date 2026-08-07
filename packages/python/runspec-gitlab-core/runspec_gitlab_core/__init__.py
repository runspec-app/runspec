"""runspec-gitlab-core — pure-Python GitLab REST (v4) read helpers.

This package depends only on ``httpx`` — it has **no dependency on runspec** and
ships **no runspec.toml and no entry points**, so installing it exposes the
helper functions for import without surfacing any runnables (it is invisible to
``runspec local`` / ``runspec serve`` discovery). ``runspec-gitlab`` depends on it
and wraps each helper as a verb of the ``gitlab`` runnable.

The helpers port the read tools of GitLab's own (hosted) MCP server onto the
public REST API. Every operation takes an explicit :class:`GitlabConfig` first
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
from runspec_gitlab_core.issues import get_issue, list_issues
from runspec_gitlab_core.merge_requests import (
    get_merge_request,
    get_merge_request_commits,
    get_merge_request_conflicts,
    get_merge_request_diffs,
    get_merge_request_notes,
    get_merge_request_pipelines,
    list_merge_requests,
)
from runspec_gitlab_core.pipelines import get_job_log, get_pipeline_jobs, list_pipelines
from runspec_gitlab_core.projects import get_project, resolve_project_path, search_projects, whoami
from runspec_gitlab_core.repository import get_file, list_tree

__all__ = [
    # client / config
    "GitlabConfig",
    "GitlabError",
    "config_from_env",
    "gitlab_request",
    "encode_project",
    "resolve_project_path",
    "VALID_ENVS",
    "DEFAULT_URL",
    # identity / projects
    "whoami",
    "get_project",
    "search_projects",
    # issues
    "list_issues",
    "get_issue",
    # merge requests
    "list_merge_requests",
    "get_merge_request",
    "get_merge_request_diffs",
    "get_merge_request_commits",
    "get_merge_request_notes",
    "get_merge_request_pipelines",
    "get_merge_request_conflicts",
    # pipelines / jobs
    "list_pipelines",
    "get_pipeline_jobs",
    "get_job_log",
    # repository
    "get_file",
    "list_tree",
]
