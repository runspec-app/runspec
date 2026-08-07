"""cli.py — the ``gitlab`` entry point.

One binary, a verb per GitLab MCP read tool. ``rs.parse("gitlab")`` resolves the
subcommand and its args; we build a :class:`GitlabConfig` from the environment
(scoped by the global ``--env``), unpack each ``.value`` at the boundary, and call
the matching :mod:`runspec_gitlab_core` helper.

Failures (the ``runspec_gitlab_core`` :class:`GitlabError` and arg-shape
``ValueError``s) become a JSON ``{"error": ...}`` payload + a non-zero exit.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import runspec as rs
from runspec_gitlab_core import (
    GitlabConfig,
    GitlabError,
    config_from_env,
    get_file,
    get_issue,
    get_job_log,
    get_merge_request,
    get_merge_request_changes,
    get_pipeline_jobs,
    get_project,
    list_issues,
    list_merge_requests,
    list_pipelines,
    list_tree,
    search_projects,
    whoami,
)


def _emit(payload: Any) -> None:
    print(json.dumps(payload, default=str))


def _dispatch(spec: rs.RunSpec, config: GitlabConfig) -> Any:
    """Run the active verb against ``config``, unpacking ``spec.<arg>.value`` for
    only the args the verb declares (globals + its own)."""
    command = spec.runspec_command

    if command == "whoami":
        return whoami(config)
    if command == "get-project":
        return get_project(config, project=spec.project.value)
    if command == "search-projects":
        return search_projects(
            config,
            search=spec.search.value,
            membership=spec.membership.value,
            per_page=spec.per_page.value,
            page=spec.page.value,
        )
    if command == "list-issues":
        return list_issues(
            config,
            project=spec.project.value,
            state=spec.state.value,
            labels=spec.labels.value,
            search=spec.search.value,
            per_page=spec.per_page.value,
            page=spec.page.value,
        )
    if command == "get-issue":
        return get_issue(config, issue_iid=spec.issue_iid.value, project=spec.project.value)
    if command == "list-merge-requests":
        return list_merge_requests(
            config,
            project=spec.project.value,
            state=spec.state.value,
            per_page=spec.per_page.value,
            page=spec.page.value,
        )
    if command == "get-merge-request":
        return get_merge_request(config, mr_iid=spec.mr_iid.value, project=spec.project.value)
    if command == "get-merge-request-changes":
        return get_merge_request_changes(config, mr_iid=spec.mr_iid.value, project=spec.project.value)
    if command == "list-pipelines":
        return list_pipelines(
            config,
            project=spec.project.value,
            ref=spec.ref.value,
            status=spec.status.value,
            per_page=spec.per_page.value,
            page=spec.page.value,
        )
    if command == "get-pipeline-jobs":
        return get_pipeline_jobs(
            config,
            pipeline_id=spec.pipeline_id.value,
            project=spec.project.value,
            per_page=spec.per_page.value,
            page=spec.page.value,
        )
    if command == "get-job-log":
        return get_job_log(config, job_id=spec.job_id.value, project=spec.project.value, tail=spec.tail.value)
    if command == "get-file":
        return get_file(config, file_path=spec.file_path.value, ref=spec.ref.value, project=spec.project.value)
    if command == "list-tree":
        return list_tree(
            config,
            path=spec.path.value,
            ref=spec.ref.value,
            recursive=spec.recursive.value,
            project=spec.project.value,
            per_page=spec.per_page.value,
            page=spec.page.value,
        )

    # require-command = true makes a bare invocation unreachable.
    raise SystemExit("no command — see `gitlab --help`")  # pragma: no cover


def main() -> None:
    spec = rs.parse("gitlab")
    try:
        config = config_from_env(env=spec.env.value)
        result = _dispatch(spec, config)
    except (GitlabError, ValueError) as exc:
        _emit({"error": str(exc), "kind": type(exc).__name__})
        sys.exit(1)
    _emit(result)


if __name__ == "__main__":
    main()
