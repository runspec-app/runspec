"""Wiring / parsing tests for the `gitlab` runnable — no mocks, real spec."""

from pathlib import Path

from runspec.testing import ParseHarness

SPEC = Path(__file__).parent.parent / "runspec_gitlab" / "runspec.toml"


def _harness() -> ParseHarness:
    return ParseHarness(script_name="gitlab", config_path=SPEC)


def test_smoke_all_verbs() -> None:
    h = _harness()
    # Walk the whole verb tree: --help exits 0, require-command is enforced,
    # omitting required args exits 1, and a fully-populated invocation parses.
    h.smoke(
        values={
            "project": "grp/app",
            "search": "x",
            "issue-iid": 1,
            "mr-iid": 1,
            "pipeline-id": 1,
            "job-id": 1,
            "file-path": "README.md",
            "title": "t",
            "body": "b",
            "source-branch": "fix",
            "target-branch": "main",
            "branch": "feat",
            "ref": "main",
            "commit-message": "m",
            "content": "x",
        }
    )
    h.close()


def test_int_args_coerce() -> None:
    h = _harness()
    spec = h.expect_ok(["get-issue", "--project", "grp/app", "--issue-iid", "42"])
    assert spec.runspec_command == "get-issue"
    assert spec.issue_iid.value == 42
    assert isinstance(spec.issue_iid.value, int)
    h.close()


def test_default_per_page() -> None:
    h = _harness()
    spec = h.expect_ok(["list-issues", "--project", "grp/app"])
    assert spec.per_page.value == 20
    assert spec.page.value == 1
    assert spec.state.value is None
    h.close()


def test_global_env_choice_inherited() -> None:
    h = _harness()
    spec = h.expect_ok(["--env", "uat", "whoami"])
    assert spec.env.value == "uat"
    h.close()


def test_get_file_ref_defaults_to_head() -> None:
    h = _harness()
    spec = h.expect_ok(["get-file", "--project", "grp/app", "--file-path", "a.py"])
    assert spec.ref.value == "HEAD"
    h.close()


def test_bad_state_choice_rejected() -> None:
    h = _harness()
    h.expect_exit(
        ["list-issues", "--project", "grp/app", "--state", "nope"],
        contains="state",
    )
    h.close()


def test_missing_required_issue_iid_rejected() -> None:
    h = _harness()
    h.expect_exit(["get-issue", "--project", "grp/app"], contains="issue-iid")
    h.close()


def test_list_tree_recursive_flag() -> None:
    h = _harness()
    spec = h.expect_ok(["list-tree", "--project", "grp/app", "--recursive"])
    assert spec.recursive.value is True
    h.close()


READ_VERBS = {
    "whoami", "get-project", "list-projects", "search-projects", "list-issues", "get-issue",
    "list-merge-requests", "get-merge-request", "get-merge-request-diffs",
    "get-merge-request-commits", "get-merge-request-notes",
    "get-merge-request-pipelines", "get-merge-request-conflicts",
    "list-pipelines", "get-pipeline-jobs", "get-job-log", "get-file", "list-tree",
}  # fmt: skip


def test_reads_autonomous_writes_confirm_deletes_manual() -> None:
    try:
        import tomllib
    except ImportError:  # Python 3.10
        import tomli as tomllib

    commands = tomllib.loads(SPEC.read_text())["gitlab"]["commands"]
    deletes = {name for name in commands if name.startswith("delete-")}
    assert deletes == {"delete-issue", "delete-branch", "delete-file", "delete-pipeline"}
    for name, cmd in commands.items():
        if name in READ_VERBS:
            expected = "autonomous"
        elif name in deletes:
            expected = "manual"  # human-only: an agent can never run a delete
        else:
            expected = "confirm"
        assert cmd["autonomy"] == expected, name
    assert len(commands) - len(READ_VERBS) - len(deletes) == 15


def test_write_file_needs_exactly_one_content_source() -> None:
    h = _harness()
    base = ["write-file", "--project", "grp/app", "--file-path", "a", "--branch", "b", "--commit-message", "m"]
    h.expect_exit(base, contains="content")
    h.expect_exit([*base, "--content", "x", "--content-file", "f"], contains="content")
    spec = h.expect_ok([*base, "--content", "x"])
    assert spec.content.value == "x"
    h.close()


def test_repeatable_labels_and_unset_is_empty() -> None:
    h = _harness()
    spec = h.expect_ok(["create-issue", "--project", "grp/app", "--title", "t", "--label", "a", "--label", "b"])
    assert list(spec.label.value) == ["a", "b"]
    assert not spec.assignee.value
    h.close()
