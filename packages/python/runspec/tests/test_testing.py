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

import textwrap
from pathlib import Path

import pytest

from runspec.testing import ParseHarness, harness

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
            assert str(spec.name) == "Ada"
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
