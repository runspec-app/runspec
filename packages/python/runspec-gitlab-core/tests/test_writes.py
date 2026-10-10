"""Write-helper tests — assert each helper sends the right method, URL and JSON
body, routed through an httpx.MockTransport (no real network)."""

import json

import httpx
import pytest

from runspec_gitlab_core import (
    GitlabConfig,
    GitlabError,
    add_issue_note,
    add_merge_request_note,
    cancel_job,
    cancel_pipeline,
    create_branch,
    create_issue,
    create_merge_request,
    create_pipeline,
    delete_branch,
    delete_file,
    delete_issue,
    delete_pipeline,
    merge_merge_request,
    play_job,
    resolve_user_ids,
    retry_job,
    retry_pipeline,
    update_issue,
    update_merge_request,
    write_file,
)
from tests.conftest import mock_request

CFG = GitlabConfig(base_url="https://gl", token="tok", project="grp/app")


def _recorder(monkeypatch, routes: dict | None = None, default: httpx.Response | None = None) -> list:
    """Record every request as (method, path, query, body). ``routes`` maps
    ``(method, path)`` to a response; anything else gets ``default``."""
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode().split("?")[0]
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, path, dict(request.url.params), body))
        if routes and (request.method, path) in routes:
            return routes[(request.method, path)]
        return default or httpx.Response(201, json={"ok": True})

    mock_request(monkeypatch, handler)
    return calls


# ── users ───────────────────────────────────────────────────────────────────


def test_resolve_user_ids(monkeypatch):
    calls = _recorder(monkeypatch, default=httpx.Response(200, json=[{"id": 7}]))
    assert resolve_user_ids(CFG, ["@alice", "42"]) == [7, 42]
    assert calls == [("GET", "/api/v4/users", {"username": "alice"}, None)]
    assert resolve_user_ids(CFG, None) is None


def test_resolve_user_ids_unknown(monkeypatch):
    _recorder(monkeypatch, default=httpx.Response(200, json=[]))
    with pytest.raises(GitlabError, match="Unknown GitLab user 'ghost'"):
        resolve_user_ids(CFG, ["ghost"])


# ── issues ──────────────────────────────────────────────────────────────────


def test_create_issue_body(monkeypatch):
    calls = _recorder(monkeypatch, routes={("GET", "/api/v4/users"): httpx.Response(200, json=[{"id": 3}])})
    create_issue(CFG, "Broken", description="desc", labels=["bug", " ui "], assignees=["bob"], due_date="2026-11-01")
    method, path, _, body = calls[-1]
    assert (method, path) == ("POST", "/api/v4/projects/grp%2Fapp/issues")
    assert body == {
        "title": "Broken",
        "description": "desc",
        "labels": "bug,ui",
        "assignee_ids": [3],
        "due_date": "2026-11-01",
    }


def test_update_issue_close_and_labels(monkeypatch):
    calls = _recorder(monkeypatch)
    update_issue(CFG, 5, add_labels=["done"], state_event="close")
    assert calls == [
        ("PUT", "/api/v4/projects/grp%2Fapp/issues/5", {}, {"add_labels": "done", "state_event": "close"}),
    ]


def test_update_issue_needs_a_field(monkeypatch):
    calls = _recorder(monkeypatch)
    with pytest.raises(ValueError, match="Nothing to update"):
        update_issue(CFG, 5)
    assert calls == []


def test_add_issue_note(monkeypatch):
    calls = _recorder(monkeypatch)
    add_issue_note(CFG, 5, "hi", internal=True)
    assert calls == [("POST", "/api/v4/projects/grp%2Fapp/issues/5/notes", {}, {"body": "hi", "internal": True})]


# ── merge requests ──────────────────────────────────────────────────────────


def test_create_merge_request_draft(monkeypatch):
    calls = _recorder(monkeypatch)
    create_merge_request(CFG, "fix", "main", "Fix login", draft=True, squash=True)
    method, path, _, body = calls[-1]
    assert (method, path) == ("POST", "/api/v4/projects/grp%2Fapp/merge_requests")
    assert body == {"source_branch": "fix", "target_branch": "main", "title": "Draft: Fix login", "squash": True}


def test_create_merge_request_draft_not_doubled(monkeypatch):
    calls = _recorder(monkeypatch)
    create_merge_request(CFG, "fix", "main", "Draft: Fix login", draft=True)
    assert calls[-1][3]["title"] == "Draft: Fix login"


def test_update_merge_request(monkeypatch):
    calls = _recorder(monkeypatch)
    update_merge_request(CFG, 9, target_branch="release", state_event="reopen")
    assert calls == [
        (
            "PUT",
            "/api/v4/projects/grp%2Fapp/merge_requests/9",
            {},
            {"target_branch": "release", "state_event": "reopen"},
        )
    ]


def test_update_merge_request_needs_a_field(monkeypatch):
    _recorder(monkeypatch)
    with pytest.raises(ValueError, match="Nothing to update"):
        update_merge_request(CFG, 9)


def test_add_merge_request_note(monkeypatch):
    calls = _recorder(monkeypatch)
    add_merge_request_note(CFG, 9, "LGTM")
    assert calls == [("POST", "/api/v4/projects/grp%2Fapp/merge_requests/9/notes", {}, {"body": "LGTM"})]


def test_merge_merge_request_keeps_mr_settings_by_default(monkeypatch):
    calls = _recorder(monkeypatch, default=httpx.Response(200, json={"state": "merged"}))
    assert merge_merge_request(CFG, 9, sha="abc") == {"state": "merged"}
    assert calls == [("PUT", "/api/v4/projects/grp%2Fapp/merge_requests/9/merge", {}, {"sha": "abc"})]


def test_merge_merge_request_when_pipeline_succeeds(monkeypatch):
    calls = _recorder(monkeypatch)
    merge_merge_request(CFG, 9, when_pipeline_succeeds=True, squash=False)
    assert calls[-1][3] == {"merge_when_pipeline_succeeds": True, "squash": False}


# ── repository ──────────────────────────────────────────────────────────────


def test_create_branch(monkeypatch):
    calls = _recorder(monkeypatch)
    create_branch(CFG, "feat", "main")
    assert calls == [
        ("POST", "/api/v4/projects/grp%2Fapp/repository/branches", {}, {"branch": "feat", "ref": "main"}),
    ]


FILE = "/api/v4/projects/grp%2Fapp/repository/files/src%2Fa.py"


def test_write_file_creates_when_missing(monkeypatch):
    calls = _recorder(
        monkeypatch,
        routes={("GET", FILE): httpx.Response(404, json={"message": "404 File Not Found"})},
        default=httpx.Response(201, json={"file_path": "src/a.py", "branch": "main"}),
    )
    out = write_file(CFG, "src/a.py", "x = 1\n", "main", "add a", last_commit_id="zzz")
    assert out["action"] == "created"
    assert calls[0] == ("GET", FILE, {"ref": "main"}, None)
    # last_commit_id only guards an update
    assert calls[1] == ("POST", FILE, {}, {"branch": "main", "content": "x = 1\n", "commit_message": "add a"})


def test_write_file_updates_from_start_branch(monkeypatch):
    calls = _recorder(
        monkeypatch,
        routes={("GET", FILE): httpx.Response(200, json={"file_path": "src/a.py"})},
        default=httpx.Response(200, json={"file_path": "src/a.py", "branch": "feat"}),
    )
    out = write_file(CFG, "src/a.py", "x", "feat", "edit", start_branch="main", last_commit_id="abc")
    assert out["action"] == "updated"
    assert calls[0][2] == {"ref": "main"}
    assert calls[1] == (
        "PUT",
        FILE,
        {},
        {"branch": "feat", "start_branch": "main", "content": "x", "commit_message": "edit", "last_commit_id": "abc"},
    )


def test_write_file_surfaces_non_404(monkeypatch):
    _recorder(monkeypatch, routes={("GET", FILE): httpx.Response(403, json={"message": "403 Forbidden"})})
    with pytest.raises(GitlabError) as exc:
        write_file(CFG, "src/a.py", "x", "main", "m")
    assert exc.value.status_code == 403


# ── pipelines / jobs ────────────────────────────────────────────────────────


def test_create_pipeline_variables(monkeypatch):
    calls = _recorder(monkeypatch)
    create_pipeline(CFG, "main", variables=["DEPLOY=1", "MSG=a=b"])
    assert calls == [
        (
            "POST",
            "/api/v4/projects/grp%2Fapp/pipeline",
            {},
            {
                "ref": "main",
                "variables": [
                    {"key": "DEPLOY", "value": "1", "variable_type": "env_var"},
                    {"key": "MSG", "value": "a=b", "variable_type": "env_var"},
                ],
            },
        )
    ]


def test_create_pipeline_bad_variable(monkeypatch):
    _recorder(monkeypatch)
    with pytest.raises(ValueError, match="expected KEY=VALUE"):
        create_pipeline(CFG, "main", variables=["NOPE"])


@pytest.mark.parametrize(
    ("fn", "path"),
    [
        (retry_pipeline, "/api/v4/projects/grp%2Fapp/pipelines/11/retry"),
        (cancel_pipeline, "/api/v4/projects/grp%2Fapp/pipelines/11/cancel"),
        (retry_job, "/api/v4/projects/grp%2Fapp/jobs/11/retry"),
        (cancel_job, "/api/v4/projects/grp%2Fapp/jobs/11/cancel"),
    ],
)
def test_pipeline_job_controls(monkeypatch, fn, path):
    calls = _recorder(monkeypatch)
    fn(CFG, 11)
    assert calls == [("POST", path, {}, None)]


def test_play_job_variables(monkeypatch):
    calls = _recorder(monkeypatch)
    play_job(CFG, 11, variables=["ENV=prod"])
    assert calls == [
        ("POST", "/api/v4/projects/grp%2Fapp/jobs/11/play", {}, {"job_variables_attributes": [{"key": "ENV", "value": "prod"}]}),
    ]


def test_api_error_carries_status(monkeypatch):
    _recorder(monkeypatch, default=httpx.Response(409, json={"message": "Branch already exists"}))
    with pytest.raises(GitlabError, match="Branch already exists") as exc:
        create_branch(CFG, "feat", "main")
    assert exc.value.status_code == 409


# ── deletes ─────────────────────────────────────────────────────────────────


def test_delete_issue(monkeypatch):
    calls = _recorder(monkeypatch, default=httpx.Response(204))
    assert delete_issue(CFG, 5) == {"deleted": "issue", "issue_iid": 5}
    assert calls == [("DELETE", "/api/v4/projects/grp%2Fapp/issues/5", {}, None)]


def test_delete_branch_encodes_name(monkeypatch):
    calls = _recorder(monkeypatch, default=httpx.Response(204))
    assert delete_branch(CFG, "feature/x")["branch"] == "feature/x"
    assert calls == [("DELETE", "/api/v4/projects/grp%2Fapp/repository/branches/feature%2Fx", {}, None)]


def test_delete_file(monkeypatch):
    calls = _recorder(monkeypatch, default=httpx.Response(204))
    delete_file(CFG, "src/a.py", "main", "remove a", last_commit_id="abc")
    assert calls == [
        ("DELETE", FILE, {}, {"branch": "main", "commit_message": "remove a", "last_commit_id": "abc"}),
    ]


def test_delete_pipeline(monkeypatch):
    calls = _recorder(monkeypatch, default=httpx.Response(204))
    assert delete_pipeline(CFG, 11) == {"deleted": "pipeline", "pipeline_id": 11}
    assert calls == [("DELETE", "/api/v4/projects/grp%2Fapp/pipelines/11", {}, None)]


def test_delete_refused_surfaces_error(monkeypatch):
    _recorder(monkeypatch, default=httpx.Response(403, json={"message": "403 Forbidden"}))
    with pytest.raises(GitlabError) as exc:
        delete_issue(CFG, 5)
    assert exc.value.status_code == 403
