"""projects.py — project discovery and create-metadata (field discovery).

Create metadata moved between Jira versions. The old
``GET /rest/api/2/issue/createmeta?projectKeys=…&expand=projects.issuetypes.fields``
was deprecated in Jira 8.4 and **removed in Data Center 9.0**; its replacements
are ``issue/createmeta/{project}/issuetypes`` and
``issue/createmeta/{project}/issuetypes/{id}`` (paged; Cloud has them too). Jira
before 8.4 only has the old one. The helpers here call the new endpoints and
fall back to the old one when the server answers 404, so one code path works on
every version, and they return the same shape either way.
"""

from __future__ import annotations

from typing import Any

import requests

from runspec_jira_core.client import build_client, translate_errors
from runspec_jira_core.config import JiraConfig
from runspec_jira_core.errors import JiraError

_CREATEMETA = "rest/api/2/issue/createmeta"
_PAGE_SIZE = 50
# The list key of a createmeta page: Data Center uses ``values``; Cloud uses
# ``issueTypes`` / ``fields`` (with ``createMetaIssueType`` / ``results`` as
# older spellings).
_PAGE_KEYS = ("values", "issueTypes", "fields", "createMetaIssueType", "results")


@translate_errors
def get_all_projects(config: JiraConfig) -> Any:
    """List every project visible to the configured credentials."""
    return build_client(config).get_all_projects()


@translate_errors
def search_projects(config: JiraConfig, query: str) -> list[dict[str, Any]]:
    """Filter visible projects by a case-insensitive substring of name or key."""
    needle = query.lower()
    projects = build_client(config).get_all_projects()
    return [p for p in projects if needle in str(p.get("name", "")).lower() or needle in str(p.get("key", "")).lower()]


@translate_errors
def get_project_components(config: JiraConfig, project_key: str) -> Any:
    """List a project's components."""
    return build_client(config).get_project_components(project_key)


def _is_not_found(exc: requests.HTTPError) -> bool:
    return getattr(getattr(exc, "response", None), "status_code", None) == 404


def _page_items(page: Any) -> list[Any]:
    if isinstance(page, list):
        return page
    if isinstance(page, dict):
        for key in _PAGE_KEYS:
            if isinstance(page.get(key), list):
                return page[key]
    return []


def _all_pages(client: Any, path: str) -> list[Any]:
    """GET every page of a createmeta listing (``startAt``/``maxResults`` paging)."""
    items: list[Any] = []
    start = 0
    while True:
        page = client.get(path, params={"startAt": start, "maxResults": _PAGE_SIZE})
        batch = _page_items(page)
        items.extend(batch)
        start += len(batch)
        if not batch or not isinstance(page, dict) or page.get("isLast") is True:
            return items
        total = page.get("total")
        if isinstance(total, int) and start >= total:
            return items
        if total is None and "isLast" not in page and len(batch) < _PAGE_SIZE:
            return items


def _legacy_project(client: Any, project_key: str, *, fields: bool) -> dict[str, Any]:
    """The project entry from the pre-9.0 ``issue/createmeta`` endpoint."""
    params: dict[str, Any] = {"projectKeys": project_key}
    if fields:
        params["expand"] = "projects.issuetypes.fields"
    body = client.get(_CREATEMETA, params=params) or {}
    projects = body.get("projects") or []
    if not projects:
        raise JiraError(f"Project {project_key!r} not found, or you can't create issues in it")
    project: dict[str, Any] = projects[0]
    return project


def _issue_types(client: Any, project_key: str) -> tuple[list[dict[str, Any]], bool]:
    """``(issue types, legacy?)`` — new endpoint first, old one on a 404."""
    try:
        return _all_pages(client, f"{_CREATEMETA}/{project_key}/issuetypes"), False
    except requests.HTTPError as exc:
        if not _is_not_found(exc):
            raise
        try:
            project = _legacy_project(client, project_key, fields=False)
        except requests.HTTPError:
            raise exc from None  # both 404: the project is the problem, not the version
        return list(project.get("issuetypes") or []), True


def _match_issue_type(types: list[dict[str, Any]], issue_type: str) -> list[dict[str, Any]]:
    wanted = issue_type.casefold()
    found = [t for t in types if wanted in (str(t.get("id", "")), str(t.get("name", "")).casefold())]
    if not found:
        names = ", ".join(str(t.get("name")) for t in types)
        raise JiraError(f"No issue type {issue_type!r} in this project (issue types: {names})")
    return found


@translate_errors
def get_project_issue_types(config: JiraConfig, project_key: str) -> list[dict[str, Any]]:
    """List the issue types available when creating in a project (every page)."""
    types, _ = _issue_types(build_client(config), project_key)
    return types


@translate_errors
def get_create_fields(config: JiraConfig, project_key: str, issue_type: str | None = None) -> dict[str, Any]:
    """Return the create-screen field metadata for a project — the discovery step
    before ``create-issue`` with ``--additional-fields``.

    The result keeps the classic createmeta shape on every Jira version:
    ``{"projects": [{"key", "issuetypes": [{"id", "name", …, "fields": {field id:
    {"required", "schema", "name", "allowedValues", …}}}]}]}``. ``issue_type``
    (a name, case-insensitive, or an id) limits it to one type; on Jira 9+ that
    is one request instead of one per issue type.
    """
    client = build_client(config)
    types, legacy = _issue_types(client, project_key)
    if issue_type:
        types = _match_issue_type(types, issue_type)
    if legacy:
        project = _legacy_project(client, project_key, fields=True)
        keep = {str(t.get("id")) for t in types}
        project["issuetypes"] = [t for t in project.get("issuetypes") or [] if str(t.get("id")) in keep]
        return {"projects": [project]}
    issuetypes = []
    for itype in types:
        rows = _all_pages(client, f"{_CREATEMETA}/{project_key}/issuetypes/{itype.get('id')}")
        fields = {str(f.get("fieldId") or f.get("key")): f for f in rows if isinstance(f, dict)}
        issuetypes.append({**itype, "fields": fields})
    return {"projects": [{"key": project_key, "issuetypes": issuetypes}]}
