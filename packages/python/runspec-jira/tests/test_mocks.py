"""The shipped mock responses (runspec_jira/mocks/jira.json) fit the spec and
play back. They are re-recorded with scripts/record_mocks.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import runspec

mock = pytest.importorskip("runspec.mock")

SPEC = Path(__file__).parent.parent / "runspec_jira" / "runspec.toml"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    # Only the package's own mocks: no venv overlay, no RUNSPEC_MOCK_DIR.
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.delenv("RUNSPEC_MOCK_DIR", raising=False)
    monkeypatch.setenv("RUNSPEC_AUDIT", "off")


def test_shipped_mocks_fit_the_spec() -> None:
    candidates = mock.load_candidates("jira", SPEC)
    assert candidates, "runspec_jira/mocks/jira.json is missing or empty"
    assert mock.check_entries("jira", SPEC, candidates) == []


def test_shipped_mocks_hold_no_local_addresses() -> None:
    text = (SPEC.parent / "mocks" / "jira.json").read_text(encoding="utf-8")
    assert "127.0.0.1" not in text and "localhost" not in text


@pytest.mark.parametrize(
    ("argv", "code", "check"),
    [
        (["get-issue", "--key", "OPS-101"], 0, lambda out: out["summary"].startswith("Disk full")),
        (["get-issue", "--key", "OPS-999"], 1, lambda out: out["kind"] == "JiraError"),
        (["search", "--jql", "assignee = currentUser()"], 0, lambda out: out["total"] == 3),
        (["add-comment", "--key", "OPS-101", "--body", "any text"], 0, lambda out: out["id"] == "2101"),
    ],
)
def test_plays_back_without_credentials(monkeypatch, capsys, argv, code, check) -> None:
    for var in ("JIRA_URL", "JIRA_PERSONAL_TOKEN", "JIRA_USERNAME", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("RUNSPEC_MOCK", "1")
    with pytest.raises(SystemExit) as exc:
        runspec.parse("jira", argv=argv, config_path=SPEC)
    assert exc.value.code == code
    assert check(json.loads(capsys.readouterr().out))
