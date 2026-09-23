"""The runspec.toml parses and every verb wires up cleanly."""

from __future__ import annotations

from pathlib import Path

from runspec.testing import ParseHarness

SPEC = Path(__file__).parent.parent / "runspec_rundeck" / "runspec.toml"


def _harness() -> ParseHarness:
    return ParseHarness(script_name="rundeck", config_path=SPEC)


def test_spec_smoke() -> None:
    with _harness() as h:
        # --help exits 0 for the runnable + each subcommand, and a valid argv
        # parses for every leaf command (choice/flag/rest aware).
        h.smoke()


def test_verbs_present() -> None:
    with _harness() as h:
        leaves = {tuple(p) for p in h.leaf_command_paths()}
        assert leaves == {("sync",), ("push",), ("list",)}


def test_list_is_autonomous() -> None:
    with _harness() as h:
        assert h._node(["list"]).get("autonomy") == "autonomous"


def test_global_args_inherited_by_sync() -> None:
    with _harness() as h:
        spec = h.expect_ok(["sync", "--project", "ops", "--env", "prod-eu"])
        assert spec.project.value == "ops"
        assert spec.get("env").value == "prod-eu"


def test_sync_choice_and_flags() -> None:
    with _harness() as h:
        spec = h.expect_ok(["sync", "--dupe-option", "create", "--prune", "--all"])
        assert spec.get("dupe-option").value == "create"
        assert spec.prune.value is True
        assert spec.all.value is True


def test_push_takes_positional_path() -> None:
    with _harness() as h:
        spec = h.expect_ok(["push", "jobs/greeter.json"])
        assert str(spec.path.value).endswith("jobs/greeter.json")  # coerced to an absolute path


def test_bad_dupe_option_rejected() -> None:
    with _harness() as h:
        h.expect_exit(["sync", "--dupe-option", "bogus"], contains="dupe-option")
