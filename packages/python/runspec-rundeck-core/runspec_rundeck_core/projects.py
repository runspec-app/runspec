"""projects.py — Rundeck project existence + create-if-missing.

A job import 404s when the target project doesn't exist, so `ensure_project`
mirrors the "create when missing" instinct at the project level.
"""

from __future__ import annotations

from typing import Any

from runspec_rundeck_core.client import RundeckConfig, RundeckError, rundeck_request


def project_exists(config: RundeckConfig, project: str | None = None) -> bool:
    """True if the project exists on the instance (a 404 means it doesn't)."""
    name = config.require_project(project)
    try:
        rundeck_request("GET", f"/project/{name}", config=config)
    except RundeckError as exc:
        if "HTTP 404" in str(exc):
            return False
        raise
    return True


def create_project(config: RundeckConfig, project: str | None = None, *, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a project. Returns the created project object."""
    name = config.require_project(project)
    body: dict[str, Any] = {"name": name}
    if settings:
        body["config"] = settings
    return rundeck_request("POST", "/projects", config=config, json_body=body)


def ensure_project(config: RundeckConfig, project: str | None = None, *, settings: dict[str, Any] | None = None) -> bool:
    """Create the project if it's missing. Returns True when it was created."""
    name = config.require_project(project)
    if project_exists(config, name):
        return False
    try:
        create_project(config, name, settings=settings)
    except RundeckError as exc:
        # A concurrent CI job may have created it between the check and the create.
        if "HTTP 409" in str(exc) and project_exists(config, name):
            return False
        raise
    return True
