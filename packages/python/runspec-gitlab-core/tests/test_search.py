"""search_code: group code search, the per-project fallback, and file reads —
routed through an httpx.MockTransport (no real network)."""

import threading

import httpx
import pytest

from runspec_gitlab_core import GitlabConfig, GitlabError, group_projects, search_code
from tests.conftest import mock_request

CFG = GitlabConfig(base_url="https://gl", token="tok")

PROJECTS = [
    {"id": 1, "path_with_namespace": "grp/api"},
    {"id": 2, "path_with_namespace": "grp/sub/worker"},
    {"id": 3, "path_with_namespace": "grp/empty", "empty_repo": True},
]

PYPROJECT = '[project]\nname = "api"\ndependencies = [\n  "Requests>=2",\n  "acme-auth>=1.4",\n]\n'


class Router:
    """Answer by URL path; records every request (thread-safe)."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.seen: list[httpx.Request] = []
        self._lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.seen.append(request)
        path = request.url.raw_path.decode().split("?")[0]
        route = self.routes.get(path)
        if route is None:
            return httpx.Response(404, json={"message": "404 Not Found"})
        return route(request) if callable(route) else route

    def paths(self) -> list[str]:
        return [r.url.raw_path.decode().split("?")[0] for r in self.seen]

    def query(self, path: str) -> dict:
        for r in self.seen:
            if r.url.raw_path.decode().split("?")[0] == path:
                return dict(r.url.params)
        raise AssertionError(f"{path} not requested")


UNSUPPORTED = httpx.Response(400, json={"error": "Scope supported only with advanced search or exact code search"})


def _blob(pid, path, data, startline=1):
    return {"project_id": pid, "path": path, "filename": path, "data": data, "startline": startline, "ref": "main"}


def test_group_search_used_when_supported(monkeypatch):
    router = Router(
        {
            "/api/v4/groups/grp/search": httpx.Response(200, json=[_blob(2, "pyproject.toml", '  "acme-auth>=1.4",\n', 7)]),
            "/api/v4/projects/2": httpx.Response(200, json={"id": 2, "path_with_namespace": "grp/sub/worker"}),
        }
    )
    mock_request(monkeypatch, router)
    out = search_code(CFG, "acme-auth", group="grp")
    assert out["strategy"] == "group"
    assert out["matches"] == [{"project": "grp/sub/worker", "file": "pyproject.toml", "ref": "main", "lines": [{"line": 7, "text": '"acme-auth>=1.4",'}]}]
    assert router.query("/api/v4/groups/grp/search")["scope"] == "blobs"
    assert "/api/v4/groups/grp/projects" not in router.paths()


def test_falls_back_to_each_project_when_group_search_unsupported(monkeypatch):
    router = Router(
        {
            "/api/v4/groups/grp%2Fteam/search": UNSUPPORTED,
            "/api/v4/groups/grp%2Fteam/projects": httpx.Response(200, json=PROJECTS),
            "/api/v4/projects/1/search": httpx.Response(200, json=[_blob(1, "pyproject.toml", 'x = 1\n"acme-auth>=1.4",\n', 3)]),
            "/api/v4/projects/2/search": httpx.Response(200, json=[]),
        }
    )
    mock_request(monkeypatch, router)
    out = search_code(CFG, "acme-auth", group="grp/team")
    assert out["strategy"] == "projects"
    assert "not available" in out["note"]
    assert out["projects_searched"] == 2  # the empty repository is skipped
    assert out["projects_with_matches"] == 1
    assert out["matches"][0]["lines"] == [{"line": 4, "text": '"acme-auth>=1.4",'}]
    q = router.query("/api/v4/groups/grp%2Fteam/projects")
    assert q["include_subgroups"] == "true"
    assert q["archived"] == "false"
    assert "/api/v4/projects/3/search" not in router.paths()


def test_falls_back_when_group_search_finds_nothing(monkeypatch):
    router = Router(
        {
            "/api/v4/groups/grp/search": httpx.Response(200, json=[]),
            "/api/v4/groups/grp/projects": httpx.Response(200, json=PROJECTS[:1]),
            "/api/v4/projects/1/search": httpx.Response(200, json=[_blob(1, "a.py", "import acme_auth\n")]),
        }
    )
    mock_request(monkeypatch, router)
    out = search_code(CFG, "acme_auth", group="grp")
    assert out["strategy"] == "projects"
    assert "found nothing" in out["note"]
    assert out["files_matched"] == 1


def test_mode_group_raises_instead_of_falling_back(monkeypatch):
    mock_request(monkeypatch, Router({"/api/v4/groups/grp/search": UNSUPPORTED}))
    with pytest.raises(GitlabError) as err:
        search_code(CFG, "x", group="grp", mode="group")
    assert err.value.status_code == 400


def test_server_error_on_group_search_is_not_swallowed(monkeypatch):
    mock_request(monkeypatch, Router({"/api/v4/groups/grp/search": httpx.Response(500, json={"message": "boom"})}))
    with pytest.raises(GitlabError, match="boom"):
        search_code(CFG, "x", group="grp")


def test_mode_projects_skips_group_search(monkeypatch):
    router = Router(
        {
            "/api/v4/groups/grp/projects": httpx.Response(200, json=PROJECTS[:1]),
            "/api/v4/projects/1/search": httpx.Response(200, json=[]),
        }
    )
    mock_request(monkeypatch, router)
    out = search_code(CFG, "x", group="grp", mode="projects")
    assert "/api/v4/groups/grp/search" not in router.paths()
    assert out["matches"] == [] and "note" not in out


def test_file_path_reads_the_file_in_each_project(monkeypatch):
    router = Router(
        {
            "/api/v4/groups/grp/projects": httpx.Response(200, json=PROJECTS),
            "/api/v4/projects/1/repository/files/pyproject.toml/raw": httpx.Response(200, text=PYPROJECT),
            # project 2 has no pyproject.toml → 404, silently skipped
        }
    )
    mock_request(monkeypatch, router)
    out = search_code(CFG, "requests", group="grp", file_path="pyproject.toml")
    assert out["strategy"] == "files"
    assert "/api/v4/groups/grp/search" not in router.paths()
    assert out["projects_searched"] == 2
    assert out["matches"] == [{"project": "grp/api", "file": "pyproject.toml", "lines": [{"line": 4, "text": '"Requests>=2",'}]}]
    assert "errors" not in out
    assert router.query("/api/v4/projects/1/repository/files/pyproject.toml/raw")["ref"] == "HEAD"


def test_file_path_case_sensitive(monkeypatch):
    router = Router(
        {
            "/api/v4/groups/grp/projects": httpx.Response(200, json=PROJECTS[:1]),
            "/api/v4/projects/1/repository/files/pyproject.toml/raw": httpx.Response(200, text=PYPROJECT),
        }
    )
    mock_request(monkeypatch, router)
    out = search_code(CFG, "requests", group="grp", file_path="pyproject.toml", case_sensitive=True)
    assert out["matches"] == []


def test_filename_filter_sent_to_gitlab_and_applied_locally(monkeypatch):
    router = Router(
        {
            "/api/v4/projects/grp%2Fapi/search": httpx.Response(
                200,
                json=[_blob("grp/api", "pyproject.toml", "acme\n"), _blob("grp/api", "docs/acme.md", "acme\n")],
            ),
        }
    )
    mock_request(monkeypatch, router)
    out = search_code(CFG, "acme", project="grp/api", filename="pyproject.toml")
    assert router.query("/api/v4/projects/grp%2Fapi/search")["search"] == "acme filename:pyproject.toml"
    assert [m["file"] for m in out["matches"]] == ["pyproject.toml"]


def test_filename_glob_is_local_only(monkeypatch):
    router = Router(
        {
            "/api/v4/projects/grp%2Fapi/search": httpx.Response(200, json=[_blob("grp/api", "a/setup.toml", "acme\n"), _blob("grp/api", "b.py", "acme\n")]),
        }
    )
    mock_request(monkeypatch, router)
    out = search_code(CFG, "acme", project="grp/api", filename="*.toml")
    assert router.query("/api/v4/projects/grp%2Fapi/search")["search"] == "acme"
    assert [m["file"] for m in out["matches"]] == ["a/setup.toml"]


def test_ref_skips_group_search(monkeypatch):
    router = Router(
        {
            "/api/v4/groups/grp/projects": httpx.Response(200, json=PROJECTS[:1]),
            "/api/v4/projects/1/search": httpx.Response(200, json=[]),
        }
    )
    mock_request(monkeypatch, router)
    out = search_code(CFG, "x", group="grp", ref="develop")
    assert "/api/v4/groups/grp/search" not in router.paths()
    assert router.query("/api/v4/projects/1/search")["ref"] == "develop"
    assert "ref" in out["note"]


def test_limit_truncates_and_errors_are_collected(monkeypatch):
    many = [{"id": i, "path_with_namespace": f"grp/p{i}"} for i in range(1, 6)]

    def project_search(request):
        pid = request.url.raw_path.decode().split("/")[4]
        if pid == "5":
            return httpx.Response(403, json={"message": "403 Forbidden"})
        return httpx.Response(200, json=[_blob(int(pid), "f.txt", "hit\n")])

    routes = {"/api/v4/groups/grp/projects": httpx.Response(200, json=many)}
    for i in range(1, 6):
        routes[f"/api/v4/projects/{i}/search"] = project_search
    mock_request(monkeypatch, Router(routes))
    out = search_code(CFG, "hit", group="grp", mode="projects", limit=2, workers=1)
    assert out["files_matched"] == 2
    assert len(out["matches"]) == 2
    assert out["truncated"] is True

    out = search_code(CFG, "hit", group="grp", mode="projects", workers=1)
    assert out["files_matched"] == 4
    assert out["error_count"] == 1
    assert out["errors"][0]["project"] == "grp/p5"


def test_group_projects_pages_and_caps(monkeypatch):
    def page(request):
        n = int(request.url.params["page"])
        start = (n - 1) * 100
        size = 100 if n < 3 else 30
        return httpx.Response(200, json=[{"id": start + i} for i in range(size)])

    mock_request(monkeypatch, Router({"/api/v4/groups/grp/projects": page}))
    projects, truncated = group_projects(CFG, "grp")
    assert len(projects) == 230 and truncated is False
    projects, truncated = group_projects(CFG, "grp", max_projects=150)
    assert len(projects) == 150 and truncated is True


def test_argument_checks():
    with pytest.raises(GitlabError, match="exactly one"):
        search_code(CFG, "x")
    with pytest.raises(GitlabError, match="exactly one"):
        search_code(CFG, "x", group="g", project="p")
    with pytest.raises(GitlabError, match="search term"):
        search_code(CFG, "", group="g")
    with pytest.raises(GitlabError, match="mode"):
        search_code(CFG, "x", group="g", mode="nope")
    with pytest.raises(GitlabError, match="file_path"):
        search_code(CFG, "x", group="g", file_path="a", mode="group")
