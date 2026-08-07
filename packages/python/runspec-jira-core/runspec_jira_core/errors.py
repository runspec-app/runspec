"""errors.py — the exception type raised across runspec-jira-core.

Every operation function raises :class:`JiraError` on configuration problems, bad
JSON arguments, and non-2xx API responses (the underlying ``atlassian-python-api``
raises ``requests`` exceptions, which the core translates into ``JiraError`` at the
operation boundary so the wrapper catches one fixed type).
"""

from __future__ import annotations


class JiraError(Exception):
    """Raised on config problems, invalid arguments, and Jira API errors."""
