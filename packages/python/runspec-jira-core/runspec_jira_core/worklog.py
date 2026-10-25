"""worklog.py — read and add work log entries."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from runspec_jira_core.client import build_client, parse_json_arg, translate_errors
from runspec_jira_core.config import JiraConfig
from runspec_jira_core.errors import JiraError


@translate_errors
def get_worklog(config: JiraConfig, key: str) -> Any:
    """Return an issue's work log entries."""
    return build_client(config).issue_get_worklog(key)


@translate_errors
def add_worklog(
    config: JiraConfig,
    key: str,
    *,
    time_sec: int | None = None,
    started: str | None = None,
    comment: str | None = None,
    worklog_json: str | None = None,
) -> Any:
    """Log work on an issue. Provide ``--time-sec`` (with optional ``--started``,
    default now, and ``--comment``), or a full ``--worklog-json`` object for
    advanced payloads."""
    client = build_client(config)
    if worklog_json:
        payload = parse_json_arg("worklog-json", worklog_json)
        return client.issue_add_json_worklog(key, payload)
    if time_sec is None:
        raise JiraError("add-worklog needs --time-sec (seconds) or --worklog-json")
    if not started:
        # Jira's own "Log work" dialog defaults the start to now.
        started = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S.000%z")
    return client.issue_worklog(key, started, time_sec, comment=comment)
