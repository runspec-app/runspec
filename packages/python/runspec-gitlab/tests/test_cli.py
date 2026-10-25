"""Wrapper logic: real parsed spec → `_dispatch` → core helper, with the core's
httpx routed through a MockTransport (no real network), plus the main() error path."""

import json
from pathlib import Path

import httpx
import pytest
from runspec.testing import ParseHarness
from runspec_gitlab_core import GitlabConfig
from runspec_gitlab_core import client as client_mod

from runspec_gitlab import cli

SPEC = Path(__file__).parent.parent / "runspec_gitlab" / "runspec.toml"
CFG = GitlabConfig(base_url="https://gl", token="tok", project="grp/app")


def _harness() -> ParseHarness:
    return ParseHarness(script_name="gitlab", config_path=SPEC)


def _capture(monkeypatch, response: httpx.Response) -> dict:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # raw_path keeps %2F encoded (the real wire path); .path would decode it.
        seen["path"] = request.url.raw_path.decode().split("?")[0]
        seen["query"] = dict(request.url.params)
        return response

    transport = httpx.MockTransport(handler)

    def fake_request(method, url, **kwargs):
        with httpx.Client(transport=transport) as c:
            return c.request(method, url, **kwargs)

    monkeypatch.setattr(client_mod.httpx, "request", fake_request)
    return seen


# ── _dispatch over a real parsed spec ───────────────────────────────────────


def test_dispatch_list_issues_unpacks_args(monkeypatch) -> None:
    seen = _capture(monkeypatch, httpx.Response(200, json=[{"iid": 1}]))
    h = _harness()
    spec = h.expect_ok(["list-issues", "--project", "grp/app", "--state", "opened", "--labels", "bug"])
    result = cli._dispatch(spec, CFG)
    h.close()
    assert result == [{"iid": 1}]
    assert seen["path"] == "/api/v4/projects/grp%2Fapp/issues"
    assert seen["query"]["state"] == "opened"
    assert seen["query"]["labels"] == "bug"


def test_dispatch_search_projects_without_term_lists_mine(monkeypatch) -> None:
    seen = _capture(monkeypatch, httpx.Response(200, json=[{"id": 1}]))
    h = _harness()
    spec = h.expect_ok(["search-projects", "--owned"])
    result = cli._dispatch(spec, CFG)
    h.close()
    assert result == [{"id": 1}]
    assert seen["path"] == "/api/v4/projects"
    assert seen["query"]["membership"] == "true"
    assert seen["query"]["owned"] == "true"
    assert "search" not in seen["query"]


def test_dispatch_get_job_log_tail(monkeypatch) -> None:
    _capture(monkeypatch, httpx.Response(200, text="a\nb\nc\nd"))
    h = _harness()
    spec = h.expect_ok(["get-job-log", "--project", "grp/app", "--job-id", "5", "--tail", "2"])
    result = cli._dispatch(spec, CFG)
    h.close()
    assert result["trace"] == "c\nd"
    assert result["truncated"] is True


def test_dispatch_mr_conflicts_uses_web_endpoint(monkeypatch) -> None:
    seen = _capture(monkeypatch, httpx.Response(200, json={"files": []}))
    h = _harness()
    spec = h.expect_ok(["get-merge-request-conflicts", "--project", "grp/app", "--mr-iid", "3"])
    result = cli._dispatch(spec, CFG)
    h.close()
    assert result == {"files": []}
    # non-public web endpoint, not /api/v4
    assert seen["path"] == "/grp/app/-/merge_requests/3/conflicts"
    assert "/api/v4" not in seen["path"]


def test_dispatch_whoami(monkeypatch) -> None:
    seen = _capture(monkeypatch, httpx.Response(200, json={"username": "alice"}))
    h = _harness()
    spec = h.expect_ok(["whoami"])
    assert cli._dispatch(spec, CFG) == {"username": "alice"}
    h.close()
    assert seen["path"] == "/api/v4/user"


def test_dispatch_get_file_decodes(monkeypatch) -> None:
    import base64

    encoded = base64.b64encode(b"print('hi')").decode()
    _capture(monkeypatch, httpx.Response(200, json={"encoding": "base64", "content": encoded}))
    h = _harness()
    spec = h.expect_ok(["get-file", "--project", "grp/app", "--file-path", "a.py", "--ref", "main"])
    result = cli._dispatch(spec, CFG)
    h.close()
    assert result["content"] == "print('hi')"


# ── writes ──────────────────────────────────────────────────────────────────


def _record(monkeypatch, routes: dict | None = None) -> list:
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode().split("?")[0]
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, path, body))
        if routes and (request.method, path) in routes:
            return routes[(request.method, path)]
        return httpx.Response(201, json={"ok": True})

    transport = httpx.MockTransport(handler)

    def fake_request(method, url, **kwargs):
        with httpx.Client(transport=transport) as c:
            return c.request(method, url, **kwargs)

    monkeypatch.setattr(client_mod.httpx, "request", fake_request)
    return calls


def test_dispatch_create_issue_labels(monkeypatch) -> None:
    calls = _record(monkeypatch)
    h = _harness()
    spec = h.expect_ok(["create-issue", "--project", "grp/app", "--title", "Bug", "--label", "a", "--label", "b"])
    cli._dispatch(spec, CFG)
    h.close()
    assert calls == [("POST", "/api/v4/projects/grp%2Fapp/issues", {"title": "Bug", "labels": "a,b"})]


def test_dispatch_update_issue_close(monkeypatch) -> None:
    calls = _record(monkeypatch)
    h = _harness()
    spec = h.expect_ok(["update-issue", "--project", "grp/app", "--issue-iid", "4", "--state", "close"])
    cli._dispatch(spec, CFG)
    h.close()
    # unset repeatable args (assignee, labels) don't clear anything
    assert calls == [("PUT", "/api/v4/projects/grp%2Fapp/issues/4", {"state_event": "close"})]


def test_dispatch_merge_keeps_mr_settings(monkeypatch) -> None:
    calls = _record(monkeypatch)
    h = _harness()
    spec = h.expect_ok(["merge-merge-request", "--project", "grp/app", "--mr-iid", "9", "--sha", "abc"])
    cli._dispatch(spec, CFG)
    h.close()
    assert calls == [("PUT", "/api/v4/projects/grp%2Fapp/merge_requests/9/merge", {"sha": "abc"})]


def test_dispatch_write_file_from_content_file(monkeypatch, tmp_path) -> None:
    src = tmp_path / "new.txt"
    src.write_text("hello\n", encoding="utf-8")
    file_url = "/api/v4/projects/grp%2Fapp/repository/files/docs%2Fnew.txt"
    calls = _record(monkeypatch, routes={("GET", file_url): httpx.Response(404, json={"message": "404"})})
    h = _harness()
    spec = h.expect_ok(
        [
            "write-file", "--project", "grp/app", "--file-path", "docs/new.txt",
            "--branch", "main", "--commit-message", "add", "--content-file", str(src),
        ]
    )  # fmt: skip
    result = cli._dispatch(spec, CFG)
    h.close()
    assert result["action"] == "created"
    assert calls[-1] == ("POST", file_url, {"branch": "main", "content": "hello\n", "commit_message": "add"})


def test_dispatch_create_pipeline_variables(monkeypatch) -> None:
    calls = _record(monkeypatch)
    h = _harness()
    spec = h.expect_ok(["create-pipeline", "--project", "grp/app", "--ref", "main", "--variable", "A=1"])
    cli._dispatch(spec, CFG)
    h.close()
    assert calls[-1][2]["variables"] == [{"key": "A", "value": "1", "variable_type": "env_var"}]


def test_dispatch_delete_branch(monkeypatch) -> None:
    calls = _record(monkeypatch)
    h = _harness()
    spec = h.expect_ok(["delete-branch", "--project", "grp/app", "--branch", "old/fix"])
    result = cli._dispatch(spec, CFG)
    h.close()
    assert result == {"deleted": "branch", "branch": "old/fix"}
    assert calls == [("DELETE", "/api/v4/projects/grp%2Fapp/repository/branches/old%2Ffix", None)]


# ── main() error path ───────────────────────────────────────────────────────


def test_main_missing_token_emits_error(monkeypatch, capsys) -> None:
    # No GITLAB_* env → config_from_env raises GitlabError → JSON error + exit 1.
    for var in ("GITLAB_TOKEN", "GITLAB_URL", "GITLAB_ENV"):
        monkeypatch.delenv(var, raising=False)
    # Build the spec via the real parser FIRST, then stub cli's parse to return
    # it — patching parse before this would also break the harness's own call.
    h = _harness()
    spec = h.expect_ok(["whoami"])
    h.close()
    monkeypatch.setattr(cli.rs, "parse", lambda name: spec)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "GitlabError"
    assert "token" in payload["error"].lower()
