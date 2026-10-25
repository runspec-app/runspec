"""cli.py — the ``gitlab`` entry point.

One binary, a verb per GitLab tool (reads + writes). ``rs.parse("gitlab")`` resolves the
subcommand and its args; we build a :class:`GitlabConfig` from the environment
(scoped by the global ``--env``), unpack each ``.value`` at the boundary, and call
the matching :mod:`runspec_gitlab_core` helper.

Failures (the ``runspec_gitlab_core`` :class:`GitlabError` and arg-shape
``ValueError``s) become a JSON ``{"error": ...}`` payload + a non-zero exit.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import runspec as rs
from runspec_gitlab_core import (
    GitlabConfig,
    GitlabError,
    add_issue_note,
    add_merge_request_note,
    cancel_job,
    cancel_pipeline,
    config_from_env,
    create_branch,
    create_issue,
    create_merge_request,
    create_pipeline,
    delete_branch,
    delete_file,
    delete_issue,
    delete_pipeline,
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
    list_issues,
    list_merge_requests,
    list_pipelines,
    list_tree,
    merge_merge_request,
    play_job,
    retry_job,
    retry_pipeline,
    search_projects,
    update_issue,
    update_merge_request,
    whoami,
    write_file,
)


def _chosen(arg: rs.Arg) -> Any:
    """The arg's value when the caller chose it: typed on the command line, or
    set through the automatic ``RUNSPEC_GITLAB_ARG_*`` key (the console's secret
    channel). A value runspec read from one of the declared ``GITLAB_*`` aliases
    is left to :func:`config_from_env`, which reads the same variables but puts
    ``GITLAB_<ENV>_*`` first when ``--env`` is set."""
    if arg.source == "cli":
        return arg.value
    auto_key = "RUNSPEC_GITLAB_ARG_" + arg.name.upper().replace("-", "_")
    return arg.value if os.environ.get(auto_key) is not None else None


def _emit(payload: Any) -> None:
    print(json.dumps(payload, default=str))


def _list(arg: rs.Arg) -> list[str] | None:
    """A repeatable arg's values, or ``None`` when it wasn't given (so the core
    leaves that field unchanged rather than clearing it)."""
    return list(arg.value) if arg.value else None


def _content(spec: rs.RunSpec) -> str:
    """``write-file`` content: ``--content`` verbatim, or the text of
    ``--content-file`` (the group guarantees exactly one is set)."""
    if spec.content_file.value is not None:
        return Path(spec.content_file.value).read_text(encoding="utf-8")
    return spec.content.value


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
    if command == "get-merge-request-diffs":
        return get_merge_request_diffs(
            config,
            mr_iid=spec.mr_iid.value,
            project=spec.project.value,
            per_page=spec.per_page.value,
            page=spec.page.value,
        )
    if command == "get-merge-request-commits":
        return get_merge_request_commits(
            config,
            mr_iid=spec.mr_iid.value,
            project=spec.project.value,
            per_page=spec.per_page.value,
            page=spec.page.value,
        )
    if command == "get-merge-request-notes":
        return get_merge_request_notes(
            config,
            mr_iid=spec.mr_iid.value,
            project=spec.project.value,
            sort=spec.sort.value,
            per_page=spec.per_page.value,
            page=spec.page.value,
        )
    if command == "get-merge-request-pipelines":
        return get_merge_request_pipelines(
            config,
            mr_iid=spec.mr_iid.value,
            project=spec.project.value,
            per_page=spec.per_page.value,
            page=spec.page.value,
        )
    if command == "get-merge-request-conflicts":
        return get_merge_request_conflicts(config, mr_iid=spec.mr_iid.value, project=spec.project.value)
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

    # ── writes (all autonomy = "confirm") ──
    if command == "create-issue":
        return create_issue(
            config,
            title=spec.title.value,
            description=spec.description.value,
            labels=_list(spec.label),
            assignees=_list(spec.assignee),
            milestone_id=spec.milestone_id.value,
            due_date=spec.due_date.value,
            confidential=spec.confidential.value,
            project=spec.project.value,
        )
    if command == "update-issue":
        return update_issue(
            config,
            issue_iid=spec.issue_iid.value,
            title=spec.title.value,
            description=spec.description.value,
            add_labels=_list(spec.add_label),
            remove_labels=_list(spec.remove_label),
            assignees=_list(spec.assignee),
            milestone_id=spec.milestone_id.value,
            due_date=spec.due_date.value,
            state_event=spec.state.value,
            project=spec.project.value,
        )
    if command == "add-issue-note":
        return add_issue_note(
            config,
            issue_iid=spec.issue_iid.value,
            body=spec.body.value,
            internal=spec.internal.value,
            project=spec.project.value,
        )
    if command == "create-merge-request":
        return create_merge_request(
            config,
            source_branch=spec.source_branch.value,
            target_branch=spec.target_branch.value,
            title=spec.title.value,
            description=spec.description.value,
            draft=spec.draft.value,
            labels=_list(spec.label),
            assignees=_list(spec.assignee),
            reviewers=_list(spec.reviewer),
            remove_source_branch=spec.remove_source_branch.value,
            squash=spec.squash.value,
            project=spec.project.value,
        )
    if command == "update-merge-request":
        return update_merge_request(
            config,
            mr_iid=spec.mr_iid.value,
            title=spec.title.value,
            description=spec.description.value,
            target_branch=spec.target_branch.value,
            add_labels=_list(spec.add_label),
            remove_labels=_list(spec.remove_label),
            assignees=_list(spec.assignee),
            reviewers=_list(spec.reviewer),
            state_event=spec.state.value,
            project=spec.project.value,
        )
    if command == "add-merge-request-note":
        return add_merge_request_note(
            config,
            mr_iid=spec.mr_iid.value,
            body=spec.body.value,
            internal=spec.internal.value,
            project=spec.project.value,
        )
    if command == "merge-merge-request":
        return merge_merge_request(
            config,
            mr_iid=spec.mr_iid.value,
            sha=spec.sha.value,
            # An unset flag keeps the merge request's own setting.
            squash=spec.squash.value or None,
            remove_source_branch=spec.remove_source_branch.value or None,
            when_pipeline_succeeds=spec.when_pipeline_succeeds.value,
            merge_commit_message=spec.merge_commit_message.value,
            project=spec.project.value,
        )
    if command == "create-branch":
        return create_branch(config, branch=spec.branch.value, ref=spec.ref.value, project=spec.project.value)
    if command == "write-file":
        return write_file(
            config,
            file_path=spec.file_path.value,
            content=_content(spec),
            branch=spec.branch.value,
            commit_message=spec.commit_message.value,
            start_branch=spec.start_branch.value,
            last_commit_id=spec.last_commit_id.value,
            project=spec.project.value,
        )
    if command == "create-pipeline":
        return create_pipeline(config, ref=spec.ref.value, variables=_list(spec.variable), project=spec.project.value)
    if command == "retry-pipeline":
        return retry_pipeline(config, pipeline_id=spec.pipeline_id.value, project=spec.project.value)
    if command == "cancel-pipeline":
        return cancel_pipeline(config, pipeline_id=spec.pipeline_id.value, project=spec.project.value)
    if command == "retry-job":
        return retry_job(config, job_id=spec.job_id.value, project=spec.project.value)
    if command == "cancel-job":
        return cancel_job(config, job_id=spec.job_id.value, project=spec.project.value)
    if command == "play-job":
        return play_job(config, job_id=spec.job_id.value, variables=_list(spec.variable), project=spec.project.value)

    # ── deletes (autonomy = "manual": human-only) ──
    if command == "delete-issue":
        return delete_issue(config, issue_iid=spec.issue_iid.value, project=spec.project.value)
    if command == "delete-branch":
        return delete_branch(config, branch=spec.branch.value, project=spec.project.value)
    if command == "delete-file":
        return delete_file(
            config,
            file_path=spec.file_path.value,
            branch=spec.branch.value,
            commit_message=spec.commit_message.value,
            last_commit_id=spec.last_commit_id.value,
            project=spec.project.value,
        )
    if command == "delete-pipeline":
        return delete_pipeline(config, pipeline_id=spec.pipeline_id.value, project=spec.project.value)

    # require-command = true makes a bare invocation unreachable.
    raise SystemExit("no command — see `gitlab --help`")  # pragma: no cover


def main() -> None:
    spec = rs.parse("gitlab")
    try:
        config = config_from_env(env=spec.env.value, url=_chosen(spec.gitlab_url), token=_chosen(spec.gitlab_token))
        result = _dispatch(spec, config)
    except (GitlabError, ValueError, OSError) as exc:
        _emit({"error": str(exc), "kind": type(exc).__name__})
        sys.exit(1)
    _emit(result)


if __name__ == "__main__":
    main()
