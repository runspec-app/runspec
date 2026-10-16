"""Wrapper logic: real parsed spec → `_dispatch` → core helper, with the core's
atlassian client faked (no real SDK calls), plus the main() error path."""

import json
from pathlib import Path
from typing import Any

import pytest
from runspec.testing import ParseHarness
from runspec_jira_core import JiraConfig
from runspec_jira_core import client as core_client

from runspec_jira import cli

SPEC = Path(__file__).parent.parent / "runspec_jira" / "runspec.toml"
CLOUD = JiraConfig(url="https://acme.atlassian.net", cloud=True, username="me@x.com", api_token="tok", service_desk_id="5")
DC = JiraConfig(url="https://jira.corp", cloud=False, personal_token="pat")


class FakeClient:
    def __init__(self, returns: dict[str, Any] | None = None):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._returns = returns or {}

    def __getattr__(self, name: str):
        def method(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            return self._returns.get(name)

        return method

    def last(self, name: str) -> tuple[tuple, dict]:
        for called, args, kwargs in reversed(self.calls):
            if called == name:
                return args, kwargs
        raise AssertionError(f"{name} not called; saw {[c[0] for c in self.calls]}")


@pytest.fixture
def fake(monkeypatch):
    jira = FakeClient()
    sd = FakeClient()
    monkeypatch.setattr(core_client, "Jira", lambda **kw: jira)
    monkeypatch.setattr(core_client, "ServiceDesk", lambda **kw: sd)
    return jira, sd


def _harness() -> ParseHarness:
    return ParseHarness(script_name="jira", config_path=SPEC)


def test_dispatch_get_issue(fake) -> None:
    jira, _ = fake
    jira._returns["get_issue"] = {"key": "PROJ-1"}
    h = _harness()
    spec = h.expect_ok(["get-issue", "--key", "PROJ-1", "--fields", "summary"])
    result = cli._dispatch(spec, CLOUD)
    h.close()
    assert result == {"key": "PROJ-1"}
    args, kwargs = jira.last("get_issue")
    assert args == ("PROJ-1",)
    assert kwargs["fields"] == "summary"


def test_dispatch_get_issue_is_compact_by_default(fake) -> None:
    jira, _ = fake
    jira._returns["get_issue"] = {"key": "PROJ-1", "fields": {"summary": "s", "status": {"name": "Open"}, "customfield_1": None}}
    h = _harness()
    spec = h.expect_ok(["get-issue", "--key", "PROJ-1", "--include", "comments,links"])
    result = cli._dispatch(spec, CLOUD)
    h.close()
    assert result["status"] == "Open"
    assert result["url"] == "https://acme.atlassian.net/browse/PROJ-1"
    assert result["comments"] == [] and result["links"] == []
    assert "fields" not in result


def test_dispatch_get_issue_raw_flag(fake) -> None:
    jira, _ = fake
    jira._returns["get_issue"] = {"key": "PROJ-1", "fields": {}}
    h = _harness()
    spec = h.expect_ok(["get-issue", "--key", "PROJ-1", "--raw"])
    assert cli._dispatch(spec, CLOUD) == {"key": "PROJ-1", "fields": {}}
    h.close()


def test_dispatch_search_compact_and_raw(fake) -> None:
    jira, _ = fake
    jira._returns["jql"] = {"startAt": 0, "total": 40, "issues": [{"key": "P-1", "fields": {"summary": "s"}}]}
    h = _harness()
    page = cli._dispatch(h.expect_ok(["search", "--jql", "project = P"]), CLOUD)
    assert page["issues"] == [{"key": "P-1", "summary": "s"}]
    assert page["truncated"] is True and page["next_start"] == 1
    assert jira.last("jql")[1]["limit"] == 25
    raw = cli._dispatch(h.expect_ok(["search", "--jql", "project = P", "--fields", "summary"]), CLOUD)
    h.close()
    assert raw["issues"][0]["fields"] == {"summary": "s"}
    assert jira.last("jql")[1]["fields"] == "summary"


def test_dispatch_project_issues_pages(fake) -> None:
    jira, _ = fake
    jira._returns["jql"] = {"startAt": 25, "total": 26, "issues": [{"key": "P-1", "fields": {}}]}
    h = _harness()
    page = cli._dispatch(h.expect_ok(["get-project-issues", "--project-key", "P", "--start", "25"]), CLOUD)
    h.close()
    assert page["truncated"] is False and page["start"] == 25


def _run_main(monkeypatch, capsys, argv, *, tty: bool, agent: bool = False) -> str:
    monkeypatch.setenv("JIRA_URL", "https://acme.atlassian.net")
    monkeypatch.setenv("JIRA_PERSONAL_TOKEN", "pat")
    if agent:
        monkeypatch.setenv("RUNSPEC_AGENT", "1")
    else:
        monkeypatch.delenv("RUNSPEC_AGENT", raising=False)
    h = _harness()
    spec = h.expect_ok(argv)
    h.close()
    monkeypatch.setattr(cli.rs, "parse", lambda name: spec)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: tty, raising=False)
    cli.main()
    return capsys.readouterr().out


ISSUE = {"key": "PROJ-1", "fields": {"summary": "Fix login", "status": {"name": "Open"}, "assignee": {"displayName": "Sam"}}}


def test_terminal_gets_readable_text(fake, monkeypatch, capsys) -> None:
    fake[0]._returns["get_issue"] = ISSUE
    out = _run_main(monkeypatch, capsys, ["get-issue", "--key", "PROJ-1"], tty=True)
    assert out.startswith("PROJ-1  Fix login")
    assert "Assignee" in out and "Sam" in out


def test_pipe_gets_json(fake, monkeypatch, capsys) -> None:
    fake[0]._returns["get_issue"] = ISSUE
    assert json.loads(_run_main(monkeypatch, capsys, ["get-issue", "--key", "PROJ-1"], tty=False))["status"] == "Open"


def test_agent_gets_json_even_on_a_tty(fake, monkeypatch, capsys) -> None:
    fake[0]._returns["get_issue"] = ISSUE
    assert json.loads(_run_main(monkeypatch, capsys, ["get-issue", "--key", "PROJ-1"], tty=True, agent=True))["status"] == "Open"


def test_terminal_raw_stays_json(fake, monkeypatch, capsys) -> None:
    fake[0]._returns["get_issue"] = ISSUE
    assert json.loads(_run_main(monkeypatch, capsys, ["get-issue", "--key", "PROJ-1", "--raw"], tty=True))["key"] == "PROJ-1"


def test_terminal_search_is_a_table(fake, monkeypatch, capsys) -> None:
    fake[0]._returns["jql"] = {"startAt": 0, "total": 3, "issues": [ISSUE]}
    out = _run_main(monkeypatch, capsys, ["search", "--jql", "x"], tty=True)
    assert out.splitlines()[0].startswith("KEY")
    assert "1 of 3 — next page: --start 1" in out


def test_dispatch_create_issue_merges_json(fake) -> None:
    jira, _ = fake
    h = _harness()
    spec = h.expect_ok(["create-issue", "--project", "P", "--summary", "s", "--issue-type", "Bug", "--additional-fields", '{"labels":["x"]}'])
    cli._dispatch(spec, CLOUD)
    h.close()
    _, kwargs = jira.last("create_issue")
    assert kwargs["fields"]["labels"] == ["x"]
    assert kwargs["fields"]["project"] == {"key": "P"}


def test_dispatch_transition_issue(fake) -> None:
    jira, _ = fake
    h = _harness()
    spec = h.expect_ok(["transition-issue", "--key", "PROJ-1", "--status", "Done"])
    cli._dispatch(spec, CLOUD)
    h.close()
    args, _ = jira.last("set_issue_status")
    assert args == ("PROJ-1", "Done")


def test_dispatch_raise_request_uses_servicedesk(fake) -> None:
    _, sd = fake
    h = _harness()
    spec = h.expect_ok(["raise-request", "--request-type-id", "17", "--field-values", '{"summary":"x"}'])
    cli._dispatch(spec, CLOUD)
    h.close()
    args, kwargs = sd.last("create_customer_request")
    assert args[0] == "5"  # falls back to config.service_desk_id
    assert args[1] == "17"
    assert args[2] == {"summary": "x"}


def test_dispatch_bad_json_raises_jira_error(fake) -> None:
    h = _harness()
    spec = h.expect_ok(["update-issue", "--key", "PROJ-1", "--fields", "{oops"])
    with pytest.raises(Exception, match="not valid JSON"):
        cli._dispatch(spec, CLOUD)
    h.close()


def test_main_missing_config_emits_error(monkeypatch, capsys) -> None:
    for var in ("JIRA_URL", "JIRA_USERNAME", "JIRA_API_TOKEN", "JIRA_PERSONAL_TOKEN", "JIRA_ENV"):
        monkeypatch.delenv(var, raising=False)
    h = _harness()
    spec = h.expect_ok(["get-issue", "--key", "PROJ-1"])
    h.close()
    monkeypatch.setattr(cli.rs, "parse", lambda name: spec)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "JiraError"
