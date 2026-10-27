"""search.py — find text in the code of every project in a group (GitLab REST v4).

GitLab's group-level code search (``/groups/:id/search?scope=blobs``) needs
Advanced Search (Elasticsearch/OpenSearch) or exact code search (Zoekt). Without
either, the API refuses it with a 400, and the web UI finds nothing across a
group even when the text is there. Per-project code search
(``/projects/:id/search?scope=blobs``) works on every instance, so
:func:`search_code` tries the group search first and, when it is refused or
finds nothing, lists the group's projects (subgroups included) and searches each
one, a few at a time.

Pass ``file_path`` to skip search entirely and read that one file in every
project (e.g. ``pyproject.toml``), matching lines locally. That needs no search
index at all, so it is the most reliable way to find e.g. a dependency name.
"""

from __future__ import annotations

import fnmatch
import posixpath
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import quote

from runspec_gitlab_core.client import GitlabConfig, GitlabError, encode_project, gitlab_request

MODES = ("auto", "group", "projects")
MAX_WORKERS = 32
MAX_LINES_PER_FILE = 5
MAX_LINE_CHARS = 300
MAX_ERRORS_SHOWN = 10
# Status codes that mean "this instance can't do group code search" rather than
# "the request was wrong": 400 is GitLab's "Scope supported only with advanced
# search or exact code search"; 403/404 come back from some versions and proxies.
_GROUP_SEARCH_UNSUPPORTED = (400, 403, 404)


def _is_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in "*?[")


def _blob_query(search: str, filename: str | None) -> str:
    """The search term, plus GitLab's ``filename:`` filter for an exact name
    (globs are matched locally, since GitLab's filter syntax differs)."""
    if filename and not _is_glob(filename):
        return f"{search} filename:{filename}"
    return search


def _name_ok(path: str, filename: str | None) -> bool:
    if not filename:
        return True
    return fnmatch.fnmatch(posixpath.basename(path), filename) or fnmatch.fnmatch(path, filename)


def _lines(text: str, search: str, start: int, case_sensitive: bool) -> list[dict[str, Any]]:
    """Every line of ``text`` containing ``search``, numbered from ``start``."""
    needle = search if case_sensitive else search.lower()
    found = []
    for offset, line in enumerate(text.splitlines()):
        hay = line if case_sensitive else line.lower()
        if needle in hay:
            found.append({"line": start + offset, "text": line.strip()[:MAX_LINE_CHARS]})
    return found


def _file_match(project: str, path: str, ref: str | None, lines: list[dict[str, Any]]) -> dict[str, Any]:
    match: dict[str, Any] = {"project": project, "file": path}
    if ref:
        match["ref"] = ref
    match["lines"] = lines[:MAX_LINES_PER_FILE]
    if len(lines) > MAX_LINES_PER_FILE:
        match["more_lines"] = len(lines) - MAX_LINES_PER_FILE
    return match


def _blob_matches(hits: list[dict[str, Any]], names: dict[Any, str], search: str, filename: str | None, case_sensitive: bool) -> list[dict[str, Any]]:
    """Turn GitLab blob search hits into one match per file.

    A hit carries a chunk of the file (``data``) starting at ``startline``; the
    lines that contain the term are kept. GitLab matches case-insensitively, so
    with ``case_sensitive`` a hit with no exact-case line is dropped.
    """
    by_file: dict[tuple[str, str], dict[str, Any]] = {}
    for hit in hits:
        path = hit.get("path") or hit.get("filename") or ""
        if not _name_ok(path, filename):
            continue
        project = names.get(hit.get("project_id"), str(hit.get("project_id")))
        lines = _lines(hit.get("data") or "", search, int(hit.get("startline") or 1), case_sensitive)
        if not lines:
            if case_sensitive:
                continue
            first = (hit.get("data") or "").strip().splitlines()[:1]
            lines = [{"line": int(hit.get("startline") or 1), "text": first[0].strip()[:MAX_LINE_CHARS] if first else ""}]
        key = (project, path)
        if key in by_file:
            seen = {item["line"] for item in by_file[key]["lines"]}
            by_file[key]["lines"] += [item for item in lines if item["line"] not in seen]
        else:
            by_file[key] = {"project": project, "path": path, "ref": hit.get("ref"), "lines": lines}
    return [_file_match(m["project"], m["path"], m["ref"], m["lines"]) for m in by_file.values()]


def group_projects(
    config: GitlabConfig,
    group: str,
    include_archived: bool = False,
    max_projects: int = 500,
) -> tuple[list[dict[str, Any]], bool]:
    """List the projects in ``group`` and its subgroups (GitLab's short form),
    ordered by path. Archived projects are left out unless ``include_archived``;
    projects only shared with the group are left out.

    Returns ``(projects, truncated)``, where ``truncated`` says the group holds
    more than ``max_projects``.
    """
    projects: list[dict[str, Any]] = []
    page = 1
    while len(projects) <= max_projects:
        batch = gitlab_request(
            "GET",
            f"/groups/{encode_project(group)}/projects",
            config=config,
            params={
                "include_subgroups": True,
                "with_shared": False,
                "archived": None if include_archived else False,
                "simple": True,
                "order_by": "path",
                "sort": "asc",
                "per_page": 100,
                "page": page,
            },
        )
        projects += batch or []
        if not batch or len(batch) < 100:
            break
        page += 1
    return projects[:max_projects], len(projects) > max_projects


def _search_one_project(
    config: GitlabConfig,
    project: dict[str, Any],
    search: str,
    filename: str | None,
    ref: str | None,
    case_sensitive: bool,
    per_page: int,
) -> list[dict[str, Any]]:
    hits = gitlab_request(
        "GET",
        f"/projects/{encode_project(project['id'])}/search",
        config=config,
        params={"scope": "blobs", "search": _blob_query(search, filename), "ref": ref, "per_page": per_page},
    )
    names = {project["id"]: project.get("path_with_namespace") or str(project["id"])}
    return _blob_matches(hits or [], names, search, filename, case_sensitive)


def _read_one_project(
    config: GitlabConfig,
    project: dict[str, Any],
    search: str,
    file_path: str,
    ref: str | None,
    case_sensitive: bool,
) -> list[dict[str, Any]]:
    try:
        text = gitlab_request(
            "GET",
            f"/projects/{encode_project(project['id'])}/repository/files/{quote(file_path, safe='')}/raw",
            config=config,
            params={"ref": ref or "HEAD"},
            raw=True,
        )
    except GitlabError as exc:
        if exc.status_code == 404:  # no such file (or an empty repository)
            return []
        raise
    lines = _lines(text or "", search, 1, case_sensitive)
    name = project.get("path_with_namespace") or str(project["id"])
    return [_file_match(name, file_path, ref, lines)] if lines else []


def _sweep(
    config: GitlabConfig,
    projects: list[dict[str, Any]],
    work: Any,
    limit: int,
    workers: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], int, bool]:
    """Run ``work(config, project)`` over ``projects`` on a thread pool.

    Stops handing out projects once ``limit`` files matched. Returns
    ``(matches, errors, projects_searched, stopped_early)``.
    """
    matches: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    searched = 0
    stopped = False
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, MAX_WORKERS)))
    try:
        futures = {pool.submit(work, config, project): project for project in projects}
        for future in as_completed(futures):
            project = futures[future]
            searched += 1
            try:
                matches += future.result()
            except GitlabError as exc:
                errors.append({"project": project.get("path_with_namespace") or str(project.get("id")), "error": str(exc)})
            if len(matches) >= limit:
                stopped = searched < len(projects)
                break
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return matches, errors, searched, stopped


def _project_names(config: GitlabConfig, ids: set[Any], workers: int) -> dict[Any, str]:
    """``project_id → path_with_namespace`` for the projects group search hit."""

    def one(pid: Any) -> tuple[Any, str]:
        try:
            project = gitlab_request("GET", f"/projects/{encode_project(pid)}", config=config, params={"simple": True})
            return pid, str(project.get("path_with_namespace") or pid)
        except GitlabError:
            return pid, str(pid)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, MAX_WORKERS))) as pool:
        return dict(pool.map(one, ids))


def search_code(
    config: GitlabConfig,
    search: str,
    group: str | None = None,
    project: str | None = None,
    file_path: str | None = None,
    filename: str | None = None,
    ref: str | None = None,
    mode: str = "auto",
    include_archived: bool = False,
    case_sensitive: bool = False,
    limit: int = 50,
    max_projects: int = 500,
    workers: int = 8,
) -> dict[str, Any]:
    """Find ``search`` in the code of a group's projects (or of one project).

    How it searches:

    * ``file_path`` set: reads that file (e.g. ``pyproject.toml``) at ``ref``
      (default branch) in every project and keeps the lines containing
      ``search``. Uses no search index, so it works on every GitLab.
    * ``mode="auto"`` (default): GitLab's group code search first; if the
      instance refuses it (no Advanced Search) or it finds nothing, every
      project in the group and its subgroups is searched in turn.
    * ``mode="group"``: group code search only (errors are raised).
    * ``mode="projects"``: skip straight to searching each project.

    ``filename`` keeps only files with that name (glob allowed, e.g.
    ``*.toml``). ``ref`` searches a branch/tag other than each project's default
    branch (per-project search only). GitLab's search is case-insensitive;
    ``case_sensitive`` drops matches whose case differs. At most ``limit``
    matching files are returned (``truncated`` says there may be more);
    ``max_projects`` caps how many of the group's projects are searched and
    ``workers`` how many run at once.

    Returns ``{"search", "group"|"project", "strategy", "projects_searched",
    "files_matched", "truncated", "matches": [{"project", "file", "lines":
    [{"line", "text"}]}], ...}`` plus ``note`` when group search was skipped,
    and ``errors``/``error_count`` for projects that could not be searched.
    """
    if not search:
        raise GitlabError("Nothing to search for — pass a search term")
    if bool(group) == bool(project):
        raise GitlabError("Pass exactly one of group or project")
    if mode not in MODES:
        raise GitlabError(f"Invalid mode {mode!r} — expected one of {', '.join(MODES)}")
    if file_path and mode == "group":
        raise GitlabError("file_path reads the file in each project; it can't be combined with mode='group'")
    limit = max(1, limit)
    per_page = min(100, max(20, limit))

    result: dict[str, Any] = {"search": search}
    if group:
        result["group"] = group
    else:
        result["project"] = project
    if file_path:
        result["file_path"] = file_path
    elif filename:
        result["filename"] = filename

    # ── group code search (needs Advanced Search / exact code search) ──
    note = None
    if group and not file_path and mode in ("auto", "group") and not (ref and mode == "auto"):
        try:
            hits = gitlab_request(
                "GET",
                f"/groups/{encode_project(group)}/search",
                config=config,
                params={"scope": "blobs", "search": _blob_query(search, filename), "per_page": per_page},
            )
        except GitlabError as exc:
            if mode == "group" or exc.status_code not in _GROUP_SEARCH_UNSUPPORTED:
                raise
            note = f"Group code search is not available on this GitLab ({exc}), so each project was searched instead"
        else:
            hits = hits or []
            names = _project_names(config, {h.get("project_id") for h in hits if h.get("project_id") is not None}, workers)
            matches = _blob_matches(hits, names, search, filename, case_sensitive)
            if matches or mode == "group":
                matches.sort(key=lambda m: (m["project"], m["file"]))
                result.update(
                    strategy="group",
                    files_matched=min(len(matches), limit),
                    truncated=len(hits) >= per_page or len(matches) > limit,
                    matches=matches[:limit],
                )
                return result
            note = "Group code search found nothing (its index may be missing or out of date), so each project was searched instead"
    elif group and ref and mode == "auto" and not file_path:
        note = "Group code search can't target a ref, so each project was searched instead"

    # ── per project ──
    if group:
        projects, projects_truncated = group_projects(config, group, include_archived=include_archived, max_projects=max(1, max_projects))
    else:
        projects, projects_truncated = [{"id": project, "path_with_namespace": project}], False
    projects = [p for p in projects if not p.get("empty_repo")]

    if file_path:

        def work(cfg: GitlabConfig, proj: dict[str, Any]) -> list[dict[str, Any]]:
            return _read_one_project(cfg, proj, search, file_path, ref, case_sensitive)

        strategy = "files"
    else:

        def work(cfg: GitlabConfig, proj: dict[str, Any]) -> list[dict[str, Any]]:
            return _search_one_project(cfg, proj, search, filename, ref, case_sensitive, per_page)

        strategy = "projects"

    matches, errors, searched, stopped = _sweep(config, projects, work, limit, workers)
    matches.sort(key=lambda m: (m["project"], m["file"]))
    result.update(
        strategy=strategy,
        projects_searched=searched,
        projects_with_matches=len({m["project"] for m in matches}),
        files_matched=min(len(matches), limit),
        truncated=stopped or len(matches) > limit,
    )
    if projects_truncated:
        result["projects_truncated"] = True
        result["max_projects"] = max_projects
    if note:
        result["note"] = note
    if errors:
        result["error_count"] = len(errors)
        result["errors"] = errors[:MAX_ERRORS_SHOWN]
    result["matches"] = matches[:limit]
    return result
