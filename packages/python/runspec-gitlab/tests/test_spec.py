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
