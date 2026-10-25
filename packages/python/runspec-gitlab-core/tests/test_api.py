"""Read-helper tests — assert each helper hits the right URL/params and shapes
its result, routed through an httpx.MockTransport (no real network)."""

import base64

import httpx
import pytest

from runspec_gitlab_core import (
    GitlabConfig,
    GitlabError,
    get_file,
    get_issue,
    get_job_log,
    get_merge_request_commits,
    get_merge_request_conflicts,
    get_merge_request_diffs,
    get_merge_request_notes,
    get_merge_request_pipelines,
    list_issues,
    list_pipelines,
    list_projects,
    list_tree,
    search_projects,
    whoami,
)
from tests.conftest import mock_request

CFG = GitlabConfig(base_url="https://gl", token="tok", project="grp/app")


def _capture(monkeypatch, response: httpx.Response) -> dict:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # raw_path keeps %2F encoded (the real wire path); .path would decode it.
        seen["path"] = request.url.raw_path.decode().split("?")[0]
        seen["query"] = dict(request.url.params)
        return response

    mock_request(monkeypatch, handler)
    return seen


def test_whoami(monkeypatch):
    seen = _capture(monkeypatch, httpx.Response(200, json={"username": "alice"}))
    assert whoami(CFG) == {"username": "alice"}
    assert seen["path"] == "/api/v4/user"


def test_search_projects_encodes_membership(monkeypatch):
    seen = _capture(monkeypatch, httpx.Response(200, json=[]))
    search_projects(CFG, "runspec", membership=True)
    assert seen["path"] == "/api/v4/projects"
    assert seen["query"]["search"] == "runspec"
    assert seen["query"]["membership"] == "true"
    assert seen["query"]["search_namespaces"] == "true"
    assert "owned" not in seen["query"]


def test_search_projects_without_term_lists_member_projects(monkeypatch):
    seen = _capture(monkeypatch, httpx.Response(200, json=[]))
    search_projects(CFG)
    assert seen["query"]["membership"] == "true"
    assert "search" not in seen["query"]
    assert "search_namespaces" not in seen["query"]


def test_search_projects_plain_term_searches_everything_by_name(monkeypatch):
    # search_namespaces across every public gitlab.com project times out (500)
    seen = _capture(monkeypatch, httpx.Response(200, json=[]))
    search_projects(CFG, "runspec")
    assert seen["query"]["search"] == "runspec"
    assert "membership" not in seen["query"]
    assert "search_namespaces" not in seen["query"]


def test_list_projects_is_mine_and_short(monkeypatch):
    seen = _capture(monkeypatch, httpx.Response(200, json=[]))
    list_projects(CFG, "cli", owned=True)
    assert seen["query"]["membership"] == "true"
    assert seen["query"]["owned"] == "true"
    assert seen["query"]["simple"] == "true"
    assert seen["query"]["search_namespaces"] == "true"


def test_list_projects_full(monkeypatch):
    seen = _capture(monkeypatch, httpx.Response(200, json=[]))
    list_projects(CFG, full=True)
    assert "simple" not in seen["query"]
    assert "search" not in seen["query"]


def test_list_issues_uses_default_project_and_filters(monkeypatch):
    seen = _capture(monkeypatch, httpx.Response(200, json=[{"iid": 1}]))
    list_issues(CFG, state="opened", labels="bug,urgent")
    assert seen["path"] == "/api/v4/projects/grp%2Fapp/issues"
    assert seen["query"]["state"] == "opened"
    assert seen["query"]["labels"] == "bug,urgent"


def test_get_issue_by_iid(monkeypatch):
    seen = _capture(monkeypatch, httpx.Response(200, json={"iid": 5}))
    assert get_issue(CFG, 5) == {"iid": 5}
    assert seen["path"] == "/api/v4/projects/grp%2Fapp/issues/5"


def test_explicit_project_overrides_default(monkeypatch):
    seen = _capture(monkeypatch, httpx.Response(200, json=[]))
    list_issues(CFG, project="99")
    assert seen["path"] == "/api/v4/projects/99/issues"


def test_missing_project_raises(monkeypatch):
    _capture(monkeypatch, httpx.Response(200, json=[]))
    cfg = GitlabConfig(base_url="https://gl", token="tok")  # no default project
    with pytest.raises(GitlabError, match="No project"):
        list_issues(cfg)


def test_mr_diffs_path(monkeypatch):
    seen = _capture(monkeypatch, httpx.Response(200, json=[]))
    get_merge_request_diffs(CFG, 12)
    assert seen["path"] == "/api/v4/projects/grp%2Fapp/merge_requests/12/diffs"


def test_mr_commits_path(monkeypatch):
    seen = _capture(monkeypatch, httpx.Response(200, json=[]))
    get_merge_request_commits(CFG, 12)
    assert seen["path"] == "/api/v4/projects/grp%2Fapp/merge_requests/12/commits"


def test_mr_notes_path_and_sort(monkeypatch):
    seen = _capture(monkeypatch, httpx.Response(200, json=[]))
    get_merge_request_notes(CFG, 12, sort="desc")
    assert seen["path"] == "/api/v4/projects/grp%2Fapp/merge_requests/12/notes"
    assert seen["query"]["sort"] == "desc"


def test_mr_pipelines_path(monkeypatch):
    seen = _capture(monkeypatch, httpx.Response(200, json=[]))
    get_merge_request_pipelines(CFG, 12)
    assert seen["path"] == "/api/v4/projects/grp%2Fapp/merge_requests/12/pipelines"


def test_mr_conflicts_uses_non_api_web_path(monkeypatch):
    # CFG.project is a path ("grp/app") — no id resolution, one request, no /api/v4.
    seen = _capture(monkeypatch, httpx.Response(200, json={"files": []}))
    result = get_merge_request_conflicts(CFG, 12)
    assert result == {"files": []}
    assert seen["path"] == "/grp/app/-/merge_requests/12/conflicts"
    assert "/api/v4" not in seen["path"]


def test_mr_conflicts_resolves_numeric_id_to_path(monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        calls.append(path)
        if path == "/api/v4/projects/99":
            return httpx.Response(200, json={"path_with_namespace": "grp/app"})
        return httpx.Response(200, json={"files": ["x"]})

    mock_request(monkeypatch, handler)
    cfg = GitlabConfig(base_url="https://gl", token="tok", project="99")
    result = get_merge_request_conflicts(cfg, 5)
    assert "/api/v4/projects/99" in calls  # numeric id resolved via the API
    assert "/grp/app/-/merge_requests/5/conflicts" in calls  # then the web endpoint
    assert result == {"files": ["x"]}


def test_list_pipelines_filters(monkeypatch):
    seen = _capture(monkeypatch, httpx.Response(200, json=[]))
    list_pipelines(CFG, ref="main", status="failed")
    assert seen["path"] == "/api/v4/projects/grp%2Fapp/pipelines"
    assert seen["query"]["ref"] == "main"
    assert seen["query"]["status"] == "failed"


def test_get_job_log_tails(monkeypatch):
    body = "\n".join(f"line{i}" for i in range(10))
    seen = _capture(monkeypatch, httpx.Response(200, text=body))
    result = get_job_log(CFG, 77, tail=3)
    assert seen["path"] == "/api/v4/projects/grp%2Fapp/jobs/77/trace"
    assert result["job_id"] == 77
    assert result["truncated"] is True
    assert result["trace"] == "line7\nline8\nline9"


def test_get_job_log_no_tail_not_truncated(monkeypatch):
    _capture(monkeypatch, httpx.Response(200, text="a\nb"))
    result = get_job_log(CFG, 1)
    assert result["truncated"] is False
    assert result["trace"] == "a\nb"


def test_get_file_decodes_base64_text(monkeypatch):
    encoded = base64.b64encode(b"hello world").decode()
    seen = _capture(
        monkeypatch,
        httpx.Response(200, json={"file_path": "README.md", "encoding": "base64", "content": encoded}),
    )
    result = get_file(CFG, "docs/README.md", ref="main")
    assert seen["path"] == "/api/v4/projects/grp%2Fapp/repository/files/docs%2FREADME.md"
    assert seen["query"]["ref"] == "main"
    assert result["content"] == "hello world"


def test_get_file_flags_binary(monkeypatch):
    encoded = base64.b64encode(b"\xff\xfe\x00\x01").decode()
    _capture(monkeypatch, httpx.Response(200, json={"encoding": "base64", "content": encoded}))
    result = get_file(CFG, "logo.png")
    assert result.get("binary") is True
    assert result["content_base64"] == encoded
    assert "content" not in result


def test_list_tree_recursive(monkeypatch):
    seen = _capture(monkeypatch, httpx.Response(200, json=[{"name": "src"}]))
    list_tree(CFG, path="src", ref="main", recursive=True)
    assert seen["path"] == "/api/v4/projects/grp%2Fapp/repository/tree"
    assert seen["query"]["recursive"] == "true"
    assert seen["query"]["path"] == "src"
