"""Wiring / parsing tests for the `jira` runnable — no mocks, real spec."""

from pathlib import Path

from runspec.testing import ParseHarness

SPEC = Path(__file__).parent.parent / "runspec_jira" / "runspec.toml"

# One value per arg name, reused across every verb that declares it.
VALUES = {
    "key": "PROJ-1",
    "project": "PROJ",
    "project-key": "PROJ",
    "summary": "Fix login",
    "issue-type": "Bug",
    "fields": "{}",  # required JSON string on update-issue; harmless elsewhere
    "assignee": "acc-1",
    "jql": "project = PROJ",
    "status": "Done",
    "body": "a comment",
    "comment-id": "10010",
    "link-type": "Blocks",
    "inward-issue": "PROJ-1",
    "outward-issue": "PROJ-2",
    "issue-key": "PROJ-1",
    "epic-key": "EPIC-1",
    "url": "https://example.com",
    "title": "Docs",
    "link-id": "10100",
    "query": "epic",
    "user": "jbloggs",
    "request-type-id": "17",
    "field-values": "{}",
    "issue-id-or-key": "SD-1",
    "dest": "/tmp/out",
    "file": "/tmp/f.txt",
    "attachment-id": "10500",
    "request-type": "New starter",
    "values": "{}",
    "role": "Service Desk Team",
    "group": "hr-agents",
    "organization": "Payroll",
}


def _harness() -> ParseHarness:
    return ParseHarness(script_name="jira", config_path=SPEC)


def test_smoke_all_verbs() -> None:
    h = _harness()
    done = h.smoke(values=VALUES)
    # Guard against accidentally checking nothing (34 verbs → many checks).
    assert len(done) > 60
    h.close()


def test_global_env_choice_inherited() -> None:
    h = _harness()
    spec = h.expect_ok(["--env", "uat", "get-issue", "--key", "PROJ-1"])
    assert spec.env.value == "uat"
    assert spec.runspec_command == "get-issue"
    h.close()


def test_bad_env_choice_rejected() -> None:
    h = _harness()
    h.expect_exit(["--env", "staging", "get-issue", "--key", "PROJ-1"], contains="env")
    h.close()


def test_fields_multiple_collects_list() -> None:
    h = _harness()
    spec = h.expect_ok(["get-issue", "--key", "PROJ-1", "--fields", "summary", "--fields", "status"])
    assert spec.fields.value == ["summary", "status"]
    h.close()


def test_search_limit_defaults_and_coerces() -> None:
    h = _harness()
    spec = h.expect_ok(["search", "--jql", "project = PROJ"])
    assert spec.limit.value == 50
    assert spec.start.value == 0
    spec2 = h.expect_ok(["search", "--jql", "x", "--limit", "5"])
    assert spec2.limit.value == 5
    assert isinstance(spec2.limit.value, int)
    h.close()


def test_delete_subtasks_flag_defaults_true() -> None:
    h = _harness()
    spec = h.expect_ok(["delete-issue", "--key", "PROJ-1"])
    assert spec.delete_subtasks.value is True
    h.close()


def test_missing_required_key_rejected() -> None:
    h = _harness()
    h.expect_exit(["get-issue"], contains="key")
    h.close()


def test_require_command_enforced() -> None:
    h = _harness()
    h.expect_exit([])
    h.close()
