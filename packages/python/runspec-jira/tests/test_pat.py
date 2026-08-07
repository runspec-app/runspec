"""The optional --jira-pat secret arg: parsing, CLI refusal, secret channel, and
that main() threads it into config_from_env as the PAT."""

from pathlib import Path
from typing import Any

from runspec.testing import ParseHarness
from runspec_jira_core import client as core_client

from runspec_jira import cli

SPEC = Path(__file__).parent.parent / "runspec_jira" / "runspec.toml"


def _h() -> ParseHarness:
    return ParseHarness(script_name="jira", config_path=SPEC)


def test_pat_optional_unset_is_none(monkeypatch):
    monkeypatch.delenv("RUNSPEC_JIRA_ARG_JIRA_PAT", raising=False)
    h = _h()
    spec = h.expect_ok(["get-issue", "--key", "PROJ-1"])
    assert spec.jira_pat.value is None
    h.close()


def test_pat_refused_on_cli():
    h = _h()
    # A password arg cannot be passed on the command line.
    h.expect_exit(["get-issue", "--key", "PROJ-1", "--jira-pat", "secret"], contains="password")
    h.close()


def test_pat_from_secret_channel(monkeypatch):
    monkeypatch.setenv("RUNSPEC_JIRA_ARG_JIRA_PAT", "tok-123")
    h = _h()
    spec = h.expect_ok(["get-issue", "--key", "PROJ-1"])
    assert spec.jira_pat.value == "tok-123"
    assert spec.jira_pat.source == "env"
    h.close()


def test_main_threads_pat_into_config(monkeypatch, capsys):
    monkeypatch.setenv("JIRA_URL", "https://acme.atlassian.net")
    monkeypatch.setenv("RUNSPEC_JIRA_ARG_JIRA_PAT", "pat-xyz")
    for var in ("JIRA_USERNAME", "JIRA_API_TOKEN", "JIRA_PERSONAL_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    made: dict[str, Any] = {}

    class _Fake:
        def __getattr__(self, name):
            return lambda *a, **k: {"key": "PROJ-1"}

    def make(**kwargs):
        made.update(kwargs)
        return _Fake()

    monkeypatch.setattr(core_client, "Jira", make)

    h = _h()
    spec = h.expect_ok(["get-issue", "--key", "PROJ-1"])
    h.close()
    monkeypatch.setattr(cli.rs, "parse", lambda name: spec)
    cli.main()

    # config_from_env used the PAT (override) → build_client sent it as a Bearer token.
    assert made.get("token") == "pat-xyz"
