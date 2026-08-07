"""cli.py — the ``jira`` entry point.

One binary, a verb per Jira tool. ``rs.parse("jira")`` resolves the subcommand and
its args; we build a :class:`JiraConfig` from the environment (scoped by the global
``--env``), unpack each ``.value`` at the boundary, and call the matching
:mod:`runspec_jira_core` helper. JSON-object arguments travel as raw strings and
are parsed inside the core op.

Failures (the core :class:`JiraError` and arg-shape ``ValueError``s) become a JSON
``{"error": ...}`` payload + a non-zero exit.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import runspec as rs
from runspec_jira_core import (
    JiraConfig,
    JiraError,
    add_attachment,
    add_comment,
    add_watcher,
    add_worklog,
    assign_issue,
    config_from_env,
    create_customer_request,
    create_issue,
    create_issue_link,
    create_remote_issue_link,
    delete_issue,
    download_attachments,
    edit_comment,
    get_all_projects,
    get_create_fields,
    get_issue,
    get_link_types,
    get_project_components,
    get_project_issue_types,
    get_project_issues,
    get_request,
    get_user_profile,
    get_watchers,
    get_worklog,
    jql_search,
    link_to_epic,
    list_attachments,
    list_request_type_fields,
    list_request_types,
    list_transitions,
    remove_attachment,
    remove_issue_link,
    remove_watcher,
    search_assignable_users,
    search_fields,
    search_projects,
    transition_issue,
    update_issue,
)


def _emit(payload: Any) -> None:
    print(json.dumps(payload, default=str))


def _dispatch(spec: rs.RunSpec, config: JiraConfig) -> Any:
    """Run the active verb against ``config``, unpacking only its declared args."""
    command = spec.runspec_command

    # ── issues ──
    if command == "get-issue":
        return get_issue(config, spec.key.value, fields=spec.fields.value or None, expand=spec.expand.value)
    if command == "create-issue":
        return create_issue(
            config,
            spec.project.value,
            spec.summary.value,
            spec.issue_type.value,
            description=spec.description.value,
            additional_fields=spec.additional_fields.value,
        )
    if command == "update-issue":
        return update_issue(config, spec.key.value, fields_json=spec.fields.value)
    if command == "assign-issue":
        return assign_issue(config, spec.key.value, spec.assignee.value)
    if command == "delete-issue":
        return delete_issue(config, spec.key.value, delete_subtasks=spec.delete_subtasks.value)

    # ── search ──
    if command == "search":
        return jql_search(
            config,
            spec.jql.value,
            fields=spec.fields.value or None,
            start=spec.start.value,
            limit=spec.limit.value,
            expand=spec.expand.value,
        )
    if command == "get-project-issues":
        return get_project_issues(config, spec.project_key.value, limit=spec.limit.value)

    # ── transitions ──
    if command == "list-transitions":
        return list_transitions(config, spec.key.value)
    if command == "transition-issue":
        return transition_issue(config, spec.key.value, status=spec.status.value, fields_json=spec.fields.value)

    # ── comments ──
    if command == "add-comment":
        return add_comment(config, spec.key.value, spec.body.value, visibility_json=spec.visibility.value)
    if command == "edit-comment":
        return edit_comment(config, spec.key.value, spec.comment_id.value, spec.body.value)

    # ── links ──
    if command == "get-link-types":
        return get_link_types(config)
    if command == "create-issue-link":
        return create_issue_link(
            config,
            spec.link_type.value,
            spec.inward_issue.value,
            spec.outward_issue.value,
            comment=spec.comment.value,
        )
    if command == "link-to-epic":
        return link_to_epic(config, spec.issue_key.value, spec.epic_key.value, epic_link_field=spec.epic_link_field.value)
    if command == "create-remote-issue-link":
        return create_remote_issue_link(
            config,
            spec.issue_key.value,
            spec.url.value,
            spec.title.value,
            relationship=spec.relationship.value,
        )
    if command == "remove-issue-link":
        return remove_issue_link(config, spec.link_id.value)

    # ── worklog ──
    if command == "get-worklog":
        return get_worklog(config, spec.key.value)
    if command == "add-worklog":
        return add_worklog(
            config,
            spec.key.value,
            time_sec=spec.time_sec.value,
            started=spec.started.value,
            comment=spec.comment.value,
            worklog_json=spec.worklog_json.value,
        )

    # ── projects ──
    if command == "get-all-projects":
        return get_all_projects(config)
    if command == "search-projects":
        return search_projects(config, spec.query.value)
    if command == "get-project-components":
        return get_project_components(config, spec.project_key.value)
    if command == "get-project-issue-types":
        return get_project_issue_types(config, spec.project_key.value)
    if command == "get-create-fields":
        return get_create_fields(config, spec.project_key.value)

    # ── fields ──
    if command == "search-fields":
        return search_fields(config, query=spec.query.value)

    # ── users ──
    if command == "get-user-profile":
        return get_user_profile(config, username=spec.username.value, account_id=spec.account_id.value, key=spec.key.value)
    if command == "search-assignable-users":
        return search_assignable_users(config, spec.project_key.value, start=spec.start.value, limit=spec.limit.value)

    # ── watchers ──
    if command == "get-watchers":
        return get_watchers(config, spec.key.value)
    if command == "add-watcher":
        return add_watcher(config, spec.key.value, spec.user.value)
    if command == "remove-watcher":
        return remove_watcher(config, spec.key.value, user=spec.user.value, account_id=spec.account_id.value)

    # ── service desk ──
    if command == "list-request-types":
        return list_request_types(config, service_desk_id=spec.service_desk_id.value)
    if command == "list-request-type-fields":
        return list_request_type_fields(config, spec.request_type_id.value, service_desk_id=spec.service_desk_id.value)
    if command == "raise-request":
        return create_customer_request(
            config,
            spec.request_type_id.value,
            spec.field_values.value,
            service_desk_id=spec.service_desk_id.value,
            raise_on_behalf_of=spec.raise_on_behalf_of.value,
        )
    if command == "get-request":
        return get_request(config, spec.issue_id_or_key.value)

    # ── attachments ──
    if command == "list-attachments":
        return list_attachments(config, spec.key.value)
    if command == "download-attachments":
        return download_attachments(config, spec.key.value, str(spec.dest.value))
    if command == "add-attachment":
        return add_attachment(config, spec.key.value, str(spec.file.value))
    if command == "remove-attachment":
        return remove_attachment(config, spec.attachment_id.value)

    # require-command = true makes a bare invocation unreachable.
    raise SystemExit("no command — see `jira --help`")  # pragma: no cover


def main() -> None:
    spec = rs.parse("jira")
    try:
        config = config_from_env(env=spec.env.value, personal_token_override=spec.jira_pat.value)
        result = _dispatch(spec, config)
    except (JiraError, ValueError) as exc:
        _emit({"error": str(exc), "kind": type(exc).__name__})
        sys.exit(1)
    _emit(result)


if __name__ == "__main__":
    main()
