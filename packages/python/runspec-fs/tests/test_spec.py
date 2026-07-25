"""Wiring / parsing tests for the `fs` runnable — no mocks, real spec."""

from pathlib import Path

from runspec.testing import ParseHarness

SPEC = Path(__file__).parent.parent / "runspec_fs" / "runspec.toml"


def _harness() -> ParseHarness:
    return ParseHarness(script_name="fs", config_path=SPEC)


def test_smoke_all_verbs() -> None:
    h = _harness()
    # Walk the whole verb tree: --help exits 0, require-command is enforced,
    # omitting required args exits 1, and a fully-populated invocation parses.
    h.smoke(
        values={
            "path": ".",
            "pattern": "x",
            "content": "hi",
            "paths": ["."],
            "old-text": ["a"],
            "new-text": ["b"],
        }
    )
    h.close()


def test_read_text_file_coerces() -> None:
    h = _harness()
    spec = h.expect_ok(["read-text-file", "--path", "README.md", "--head", "5"])
    assert spec.runspec_command == "read-text-file"
    assert spec.head.value == 5
    assert spec.tail.value is None
    h.close()


def test_bad_choice_rejected() -> None:
    h = _harness()
    h.expect_exit(
        ["list-directory-with-sizes", "--path", ".", "--sort-by", "nope"],
        contains="sort-by",
    )
    h.close()


def test_missing_required_path_rejected() -> None:
    h = _harness()
    h.expect_exit(["list-directory"], contains="path")
    h.close()


def test_edit_file_repeated_args_build_lists() -> None:
    h = _harness()
    spec = h.expect_ok(
        [
            "edit-file",
            "--path",
            "a.txt",
            "--old-text",
            "foo",
            "--old-text",
            "baz",
            "--new-text",
            "bar",
            "--new-text",
            "qux",
        ]
    )
    assert spec.old_text.value == ["foo", "baz"]
    assert spec.new_text.value == ["bar", "qux"]
    h.close()
