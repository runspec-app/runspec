"""
Tests for runspec.testing.ParseHarness — and a worked example of how a
developer scaffolds tests for a real, subcommand-heavy runnable.

The `static-data` fixture is the migrated trading-symbol runnable from
docs/design/new_example_toml.md: ~11 subcommands, parent-level args
(region/env/source/jira), patterns, multiple values, and require-command.
It stands in for any large migrated runnable so the patterns below transfer
directly.

Two layers are demonstrated:

  Layer 1 — wiring/parsing, no mocks (TestStaticDataWiring, TestSmoke).
  Layer 2 — the actual work, mocking only the I/O boundary (TestLayerTwoPattern).
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

import runspec
from runspec.testing import ParseHarness, harness, no_run_as_check

posix_only = pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX uid model required")

FIXTURES = Path(__file__).parent / "fixtures"
STATIC_DATA = FIXTURES / "static_data.toml"


@pytest.fixture()
def h() -> ParseHarness:
    return ParseHarness(script_name="static-data", config_path=STATIC_DATA)


# ── Layer 1: wiring / parsing (no mocks) ──────────────────────────────────────


class TestStaticDataWiring:
    def test_subcommand_is_detected(self, h: ParseHarness) -> None:
        spec = h.expect_ok(["create", "-s", "AAPL,MSFT", "--region", "americas", "--env", "prod"])
        assert spec.runspec_command == "create"
        assert spec.runspec_command_path == ["create"]

    def test_types_are_coerced(self, h: ParseHarness) -> None:
        spec = h.expect_ok(["create", "-s", "AAPL", "--price", "12.5", "--region", "americas", "--env", "prod"])
        assert spec.symbols.value == ["AAPL"]  # multiple + delimiter -> list
        assert spec.price.value == 12.5  # float
        assert isinstance(spec.price.value, float)

    def test_flag_defaults_false(self, h: ParseHarness) -> None:
        spec = h.expect_ok(["create", "-s", "AAPL", "--region", "americas", "--env", "prod"])
        assert spec.persist.value is False

    def test_missing_required_symbols_exits(self, h: ParseHarness) -> None:
        # `symbols` is required on `create`; omit it.
        out = h.expect_exit(["create", "--region", "americas", "--env", "prod"])
        assert "symbol" in out.lower() or "-s" in out

    def test_bad_choice_names_the_flag(self, h: ParseHarness) -> None:
        h.expect_exit(
            ["create", "-s", "AAPL", "--region", "mars", "--env", "prod"],
            contains="--region",
        )

    def test_pattern_violation_is_rejected(self, h: ParseHarness) -> None:
        # symbols pattern forbids whitespace / lowercase.
        h.expect_exit(["show", "-s", "not a symbol", "--region", "americas", "--env", "prod"])

    def test_require_command_enforced(self, h: ParseHarness) -> None:
        # static-data sets require-command = true.
        h.expect_exit([])

    def test_help_exits_zero(self, h: ParseHarness) -> None:
        h.expect_exit(["--help"], code=0)
        h.expect_exit(["create", "--help"], code=0, contains="--price")


# ── Layer 1: auto-coverage across the whole tree (the high-leverage bit) ───────


class TestSmoke:
    def test_smoke_covers_every_subcommand(self, h: ParseHarness) -> None:
        # One call: --help works for the runnable and every subcommand,
        # require-command is enforced, and every command with required args
        # rejects an empty invocation. `values` supplies shared arg values so
        # leaves are also exercised with a full, valid invocation.
        performed = h.smoke(
            values={
                "region": "americas",
                "env": "prod",
                "symbols": "AAPL",
                "price": "12.5",
                "adv": "1000",
                "trades": "50",
                "ipo_date": "20240101",
                "volume": "100",
                "field": "SomeField",
                "value": "x",
            }
        )
        # 11 subcommands -> plenty of checks; guard against silently doing nothing.
        assert len(performed) > 20
        assert "create (valid)" in performed
        assert "create -> missing required" in performed
        assert "(no command) -> require-command" in performed

    def test_command_discovery(self, h: ParseHarness) -> None:
        names = {p[-1] for p in h.command_paths()}
        assert {"show", "create", "clone", "refresh"} <= names
        assert h.required_args(["create"]) == ["symbols"]
        assert h.requires_command() is True


# A subcommand whose two members form an exactly-one group, plus an exclusive
# group, so smoke must include at most one member of each even when `values`
# supplies all of them.
GROUPED = textwrap.dedent("""\
    [emit]
    [emit.commands.run]
    description = "emit output"
    [emit.commands.run.args]
    json = {type = "flag"}
    yaml = {type = "flag"}
    out  = {type = "str", required = false}
    err  = {type = "str", required = false}
    [emit.commands.run.groups.fmt]
    exactly-one = true
    args = ["json", "yaml"]
    [emit.commands.run.groups.sink]
    exclusive = true
    args = ["out", "err"]
""")


class TestSmokeGroupAware:
    def test_smoke_does_not_violate_exclusive_groups(self) -> None:
        # Supplying *all* group members must not build a mutually-exclusive
        # invocation — smoke includes at most one member of each group.
        with harness("emit", toml=GROUPED) as h:
            performed = h.smoke(values={"json": True, "yaml": True, "out": "a", "err": "b"})
        assert "run (valid)" in performed  # the valid invocation actually ran

    def test_exactly_one_group_with_no_member_skips_valid(self) -> None:
        # No member supplied for the exactly-one group -> can't construct a
        # valid call, so the valid-invocation check is skipped (not failed).
        with harness("emit", toml=GROUPED) as h:
            performed = h.smoke(values={"out": "a"})
        assert "run (valid)" not in performed


# Presence groups — at-least-one / inclusive / if->requires — the constraints
# that require a member to be *present* (as opposed to exclusive/exactly-one,
# which cap). smoke must add a fabricatable member so the built invocation
# satisfies the group instead of building one the validator rejects.
AT_LEAST_ONE = textwrap.dedent("""\
    [emit]
    [emit.commands.run]
    [emit.commands.run.args]
    verbose = {type = "flag"}
    quiet   = {type = "flag"}
    [emit.commands.run.groups.mode]
    at-least-one = true
    args = ["verbose", "quiet"]
""")

# An at-least-one group whose only members are plain strings with no supplied
# value — nothing to fabricate, so the leaf's valid check is skipped, not failed.
AT_LEAST_ONE_UNFABRICATABLE = textwrap.dedent("""\
    [emit]
    [emit.commands.run]
    [emit.commands.run.args]
    a = {type = "str", required = false}
    b = {type = "str", required = false}
    [emit.commands.run.groups.src]
    at-least-one = true
    args = ["a", "b"]
""")

INCLUSIVE = textwrap.dedent("""\
    [emit]
    [emit.commands.run]
    [emit.commands.run.args]
    tls = {type = "flag"}
    ca  = {type = "choice", options = ["system", "bundled"]}
    [emit.commands.run.groups.secure]
    inclusive = true
    args = ["tls", "ca"]
""")

CONDITION = textwrap.dedent("""\
    [emit]
    [emit.commands.run]
    [emit.commands.run.args]
    deploy = {type = "flag"}
    target = {type = "choice", options = ["stage", "prod"]}
    [emit.commands.run.groups.dep]
    if = "deploy"
    requires = ["target"]
""")


class TestSmokePresenceGroups:
    def test_at_least_one_fabricates_a_member(self) -> None:
        # No member supplied -> smoke fabricates a member (the flag) so the
        # built invocation satisfies at-least-one instead of being rejected.
        with harness("emit", toml=AT_LEAST_ONE) as h:
            performed = h.smoke(values={})
        assert "run (valid)" in performed

    def test_at_least_one_honours_a_supplied_member(self) -> None:
        with harness("emit", toml=AT_LEAST_ONE) as h:
            performed = h.smoke(values={"quiet": True})
        assert "run (valid)" in performed

    def test_at_least_one_unfabricatable_skips_valid(self) -> None:
        # Members are plain strings with no value -> nothing to fabricate, so
        # the valid-invocation check is skipped (not failed).
        with harness("emit", toml=AT_LEAST_ONE_UNFABRICATABLE) as h:
            performed = h.smoke(values={})
        assert "run (valid)" not in performed
        # Supplying one member makes a valid invocation constructible again.
        with harness("emit", toml=AT_LEAST_ONE_UNFABRICATABLE) as h:
            performed = h.smoke(values={"a": "x"})
        assert "run (valid)" in performed

    def test_inclusive_completes_the_group(self) -> None:
        # One member supplied -> smoke adds the other (a fabricatable choice) so
        # the all-or-none inclusive group is satisfied.
        with harness("emit", toml=INCLUSIVE) as h:
            performed = h.smoke(values={"tls": True})
        assert "run (valid)" in performed

    def test_inclusive_none_supplied_is_valid(self) -> None:
        # inclusive is all-or-none: supplying nothing is a valid invocation.
        with harness("emit", toml=INCLUSIVE) as h:
            performed = h.smoke(values={})
        assert "run (valid)" in performed

    def test_condition_fills_required_arg(self) -> None:
        # The condition arg is supplied -> smoke adds the `requires` member so
        # the if->requires constraint is satisfied.
        with harness("emit", toml=CONDITION) as h:
            performed = h.smoke(values={"deploy": True})
        assert "run (valid)" in performed

    def test_condition_untriggered_is_valid(self) -> None:
        # Condition arg absent -> the requirement is inactive, empty call valid.
        with harness("emit", toml=CONDITION) as h:
            performed = h.smoke(values={})
        assert "run (valid)" in performed


# The same arg name (`status`, a choice) declares DIFFERENT options on each
# subcommand — the classic case where one shared `values` entry can't be valid
# everywhere.
PER_CMD_CHOICES = textwrap.dedent("""\
    [tix]
    require-command = true
    [tix.commands.open]
    [tix.commands.open.args]
    status = {type = "choice", options = ["new", "triage"]}
    [tix.commands.close]
    [tix.commands.close.args]
    status = {type = "choice", options = ["done", "wontfix"]}
""")


class TestSmokeChoiceAware:
    def test_shared_value_is_adapted_per_subcommand(self) -> None:
        # "new" is only valid under `open`; smoke substitutes a valid option for
        # `close` instead of failing.
        with harness("tix", toml=PER_CMD_CHOICES) as h:
            performed = h.smoke(values={"status": "new"})
        assert "open (valid)" in performed
        assert "close (valid)" in performed

    def test_choice_args_need_no_value_at_all(self) -> None:
        # With no value supplied, a (required) choice arg is filled from each
        # leaf's own options, so both subcommands still get a valid invocation.
        with harness("tix", toml=PER_CMD_CHOICES) as h:
            performed = h.smoke(values={})
        assert "open (valid)" in performed
        assert "close (valid)" in performed


# ── Layer 2: the actual work — mock only the I/O boundary ──────────────────────


class TestLayerTwoPattern:
    """How to test the work a subcommand does. Keep main() thin —
    `parse() -> plain function -> print` — then test the plain function directly
    and patch its single external call. Here we simulate that with a tiny stand-in
    module so the pattern is self-contained.
    """

    def test_logic_function_with_mocked_boundary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Stand-in for your real module: a pure logic function + the one I/O call.
        calls: list[tuple] = []

        def fake_store_call(region: str, action: str, symbols: list[str], **fields):
            calls.append((region, action, tuple(symbols), fields))
            return {"ok": True, "applied": list(symbols)}

        def create_symbols(region, symbols, *, price=None, _call=fake_store_call):
            # the kind of plain function your main() should delegate to
            return _call(region, "create", symbols, price=price)

        # The harness gives you the parsed RunSpec to feed the logic function.
        h = ParseHarness(script_name="static-data", config_path=STATIC_DATA)
        spec = h.expect_ok(["create", "-s", "AAPL,MSFT", "--price", "12.5", "--region", "americas", "--env", "prod"])

        result = create_symbols(spec.region.value, spec.symbols.value, price=spec.price.value)

        assert result == {"ok": True, "applied": ["AAPL", "MSFT"]}
        assert calls == [("americas", "create", ("AAPL", "MSFT"), {"price": 12.5})]


# ── Harness behaviour itself ──────────────────────────────────────────────────


GREET = textwrap.dedent("""\
    [config]
    autonomy-default = "confirm"

    [greet]
    description = "Greet someone"

    [greet.args]
    name = {type = "str", required = true}
    loud = {default = false}
    times = {default = 1}
""")


class TestHarnessApi:
    def test_inline_toml_and_context_manager(self) -> None:
        with ParseHarness(script_name="greet", toml=GREET) as g:
            spec = g.expect_ok(["--name", "Ada", "--times", "3"])
            assert spec.name.value == "Ada"
            assert spec.times.value == 3
            assert spec.loud.value is False

    def test_factory_helper(self) -> None:
        with harness("greet", toml=GREET) as g:
            g.expect_exit([], contains="name")  # missing required

    def test_requires_exactly_one_source(self) -> None:
        with pytest.raises(ValueError):
            ParseHarness(script_name="greet")
        with pytest.raises(ValueError):
            ParseHarness(script_name="greet", toml=GREET, config_path=STATIC_DATA)

    def test_unknown_runnable_raises(self) -> None:
        with pytest.raises(KeyError):
            ParseHarness(script_name="nope", toml=GREET)

    def test_expect_ok_failure_is_informative(self) -> None:
        with harness("greet", toml=GREET) as g:
            with pytest.raises(AssertionError) as exc:
                g.expect_ok([])  # missing required -> should have exited
            assert "to parse" in str(exc.value)

    def test_expect_exit_when_parse_succeeds_raises(self) -> None:
        with harness("greet", toml=GREET) as g:
            with pytest.raises(AssertionError) as exc:
                g.expect_exit(["--name", "Ada"])  # this parses fine
            assert "expected" in str(exc.value).lower()


# A runnable that declares run_as for a user the test suite is never running as.
# A nonexistent name is a deterministic mismatch on any POSIX host (the gate
# reports "does not exist"), so this stands in for the real "run as the service
# account" case without depending on which accounts exist on the runner.
RUN_AS_SPEC = """
[greet]
run_as = "nope-user-xyz"
[greet.args]
name = {type = "str", required = true}
[greet.commands.hi]
description = "say hi"
"""


class TestRunAsIsNotEnforced:
    """The harness must not apply the run_as identity gate — testing a
    runnable's wiring cannot depend on *who* runs the suite (mirrors how
    secret prompting is disabled)."""

    def test_expect_ok_ignores_run_as(self) -> None:
        with harness("greet", toml=RUN_AS_SPEC) as g:
            spec = g.expect_ok(["--name", "Ada"])
            assert spec.name.value == "Ada"

    def test_smoke_ignores_run_as(self) -> None:
        with harness("greet", toml=RUN_AS_SPEC) as g:
            performed = g.smoke(values={"name": "Ada"})
            assert any("(valid)" in label for label in performed)

    def test_validation_still_runs_under_run_as(self) -> None:
        # Skipping the identity gate must not skip ordinary arg validation.
        with harness("greet", toml=RUN_AS_SPEC) as g:
            g.expect_exit([], contains="name")  # missing required


class TestNoRunAsCheck:
    """no_run_as_check() lets a main()-driven test — one that calls the real
    runspec.parse() inside main() — skip the identity gate the harness can't
    reach."""

    def _spec(self, tmp_path: Path) -> Path:
        cfg = tmp_path / "runspec.toml"
        cfg.write_text(RUN_AS_SPEC, encoding="utf-8")
        return cfg

    @posix_only
    def test_real_parse_trips_the_gate_without_the_helper(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # This is what a drive_main() test hits: parse() run for real, gate live.
        cfg = self._spec(tmp_path)
        with pytest.raises(SystemExit) as exc:
            runspec.parse("greet", argv=["--name", "Ada"], config_path=cfg)
        assert exc.value.code == 1
        assert "Refusing to run 'greet'" in capsys.readouterr().out

    @posix_only
    def test_helper_lets_the_real_parse_through(self, tmp_path: Path) -> None:
        cfg = self._spec(tmp_path)
        with no_run_as_check():
            spec = runspec.parse("greet", argv=["--name", "Ada"], config_path=cfg)
        assert spec.name.value == "Ada"

    def test_helper_restores_the_original_after_the_block(self) -> None:
        from runspec import parser

        original = parser.verify_run_as_identity
        with no_run_as_check():
            assert parser.verify_run_as_identity is not original  # patched inside
        assert parser.verify_run_as_identity is original  # restored after
