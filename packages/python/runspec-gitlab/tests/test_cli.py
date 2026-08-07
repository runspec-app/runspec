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
