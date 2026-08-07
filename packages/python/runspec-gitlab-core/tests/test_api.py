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
    get_merge_request_changes,
    list_issues,
    list_pipelines,
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


def test_mr_changes_path(monkeypatch):
    seen = _capture(monkeypatch, httpx.Response(200, json={"changes": []}))
    get_merge_request_changes(CFG, 12)
    assert seen["path"] == "/api/v4/projects/grp%2Fapp/merge_requests/12/changes"


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
