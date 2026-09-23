"""Unit tests for the job/project ops and the sync composition."""

from __future__ import annotations

import json

import httpx

from runspec_rundeck_core import (
    RundeckConfig,
    ensure_project,
    import_jobs,
    list_jobs,
    project_exists,
    prune_jobs,
    summarize_import,
    sync_target,
)
from runspec_rundeck_core.jobs import import_error_details

from .conftest import mock_request


def _cfg(**over):
    base = dict(url="https://rd", token="tok", project="ops", api_version=41)
    base.update(over)
    return RundeckConfig(**base)


def _job(name, uuid, group="runspec"):
    return {"name": name, "uuid": uuid, "group": group}


def test_import_jobs_posts_body_and_params(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["ctype"] = request.headers.get("Content-Type")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"succeeded": [{"name": "a"}], "failed": [], "skipped": []})

    mock_request(monkeypatch, handler)
    jobs = [_job("a", "u1")]
    result = import_jobs(_cfg(), jobs)
    assert seen["path"] == "/api/41/project/ops/jobs/import"
    assert seen["query"] == {"format": "json", "dupeOption": "update", "uuidOption": "preserve"}
    assert seen["ctype"] == "application/json"
    assert seen["body"] == jobs
    assert result.ok
    assert len(result.succeeded) == 1


def test_import_jobs_reports_failures(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"succeeded": [], "failed": [{"name": "bad", "error": "boom"}], "skipped": []},
        )

    mock_request(monkeypatch, handler)
    result = import_jobs(_cfg(), [_job("bad", "u")])
    assert not result.ok
    assert import_error_details(result) == ["bad: boom"]
    assert "1 failed" in summarize_import(result)


def test_list_jobs_scopes_by_group(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json=[{"id": "u1", "name": "a", "group": "runspec"}])

    mock_request(monkeypatch, handler)
    jobs = list_jobs(_cfg(), group="runspec")
    assert seen["query"] == {"groupPath": "runspec"}
    assert jobs[0]["id"] == "u1"


def test_prune_deletes_only_unkept(monkeypatch):
    deleted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {"id": "keep", "name": "a", "group": "runspec"},
                    {"id": "stale", "name": "b", "group": "runspec"},
                ],
            )
        # DELETE
        deleted.append(request.url.path)
        return httpx.Response(204)

    mock_request(monkeypatch, handler)
    result = prune_jobs(_cfg(), "runspec", {"keep"})
    assert deleted == ["/api/41/job/stale"]
    assert result == [{"id": "stale", "name": "b", "group": "runspec"}]


def test_project_exists_true_and_false(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if "missing" in request.url.path:
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json={"name": "ops"})

    mock_request(monkeypatch, handler)
    assert project_exists(_cfg()) is True
    assert project_exists(_cfg(project="missing")) is False


def test_ensure_project_creates_when_missing(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(404, json={"message": "no"})
        return httpx.Response(201, json={"name": "ops"})

    mock_request(monkeypatch, handler)
    created = ensure_project(_cfg())
    assert created is True
    assert ("POST", "/api/41/projects") in calls


def test_sync_target_imports_and_prunes(monkeypatch):
    events = []

    def handler(request: httpx.Request) -> httpx.Response:
        events.append((request.method, request.url.path))
        if request.url.path.endswith("/jobs/import"):
            return httpx.Response(200, json={"succeeded": [{"name": "a"}], "failed": [], "skipped": []})
        if request.method == "GET" and request.url.path.endswith("/jobs"):
            return httpx.Response(
                200,
                json=[
                    {"id": "u1", "name": "a", "group": "runspec"},
                    {"id": "old", "name": "z", "group": "runspec"},
                ],
            )
        return httpx.Response(204)  # DELETE

    mock_request(monkeypatch, handler)
    jobs = [_job("a", "u1")]
    result = sync_target(_cfg(), jobs, prune=True)
    assert result.ok
    assert len(result.imported) == 1
    assert [p["id"] for p in result.pruned] == ["old"]


def test_sync_target_skips_prune_when_import_failed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs/import"):
            return httpx.Response(200, json={"succeeded": [], "failed": [{"name": "a", "error": "x"}], "skipped": []})
        raise AssertionError("prune must not run when import failed")

    mock_request(monkeypatch, handler)
    result = sync_target(_cfg(), [_job("a", "u1")], prune=True)
    assert not result.ok
    assert result.pruned == []
