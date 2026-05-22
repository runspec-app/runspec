"""
Compliance tests — every language pack must produce identical results
from these fixtures. The Python pack is the reference implementation.
"""

from pathlib import Path

import pytest

from runspec.loader import load_raw

FIXTURES = Path(__file__).parent / "fixtures"


# ── simple.toml ───────────────────────────────────────────────────────────────


class TestSimpleFixture:
    @pytest.fixture
    def spec(self):
        return load_raw(FIXTURES / "simple.toml")

    def test_config_defaults(self, spec):
        assert spec["config"]["autonomy_default"] == "confirm"

    def test_runnable_present(self, spec):
        assert "greet" in spec["runnables"]

    def test_runnable_description(self, spec):
        assert spec["runnables"]["greet"]["description"] == "Greet someone from the command line"

    def test_runnable_autonomy(self, spec):
        assert spec["runnables"]["greet"]["autonomy"] == "autonomous"

    def test_arg_name_required(self, spec):
        name = spec["runnables"]["greet"]["args"]["name"]
        assert name["type"] == "str"
        # No default → required inferred by inference layer, but loader preserves raw
        assert name["default"] is None

    def test_arg_loud_default(self, spec):
        loud = spec["runnables"]["greet"]["args"]["loud"]
        assert loud["default"] is False

    def test_arg_times_default(self, spec):
        times = spec["runnables"]["greet"]["args"]["times"]
        assert times["default"] == 1


# ── complex.toml ─────────────────────────────────────────────────────────────


class TestComplexFixture:
    @pytest.fixture
    def spec(self):
        return load_raw(FIXTURES / "complex.toml")

    def test_config(self, spec):
        assert spec["config"]["autonomy_default"] == "confirm"
        assert spec["config"]["lang"] == "python"
        assert spec["config"]["version"] == "1"

    def test_pipeline_present(self, spec):
        assert "pipeline" in spec["runnables"]

    def test_subcommands_present(self, spec):
        commands = spec["runnables"]["pipeline"]["commands"]
        assert "run" in commands
        assert "validate" in commands

    def test_run_subcommand_autonomy(self, spec):
        run = spec["runnables"]["pipeline"]["commands"]["run"]
        assert run["autonomy"] == "confirm"
        assert run["autonomy_reason"] == "Writes output files and may call external APIs"

    def test_validate_subcommand_autonomy(self, spec):
        validate = spec["runnables"]["pipeline"]["commands"]["validate"]
        assert validate["autonomy"] == "autonomous"

    def test_run_args_present(self, spec):
        args = spec["runnables"]["pipeline"]["commands"]["run"]["args"]
        expected = {"input", "tag", "fields", "format", "workers", "batch-size", "dry-run", "verbose", "strict", "api-key", "timeout", "threads"}
        assert set(args.keys()) == expected

    def test_choice_arg(self, spec):
        fmt = spec["runnables"]["pipeline"]["commands"]["run"]["args"]["format"]
        assert fmt["options"] == ["json", "csv", "parquet"]
        assert fmt["default"] == "json"

    def test_range_arg(self, spec):
        workers = spec["runnables"]["pipeline"]["commands"]["run"]["args"]["workers"]
        assert workers["default"] == 4
        assert workers["range"] == (1, 32)

    def test_multiple_arg(self, spec):
        tag = spec["runnables"]["pipeline"]["commands"]["run"]["args"]["tag"]
        assert tag["multiple"] is True

    def test_delimiter_arg(self, spec):
        fields = spec["runnables"]["pipeline"]["commands"]["run"]["args"]["fields"]
        assert fields["delimiter"] == ","

    def test_env_arg(self, spec):
        api_key = spec["runnables"]["pipeline"]["commands"]["run"]["args"]["api-key"]
        assert api_key["env"] == ["PIPELINE_API_KEY"]
        assert api_key["autonomy"] == "manual"

    def test_deprecated_arg(self, spec):
        threads = spec["runnables"]["pipeline"]["commands"]["run"]["args"]["threads"]
        assert threads["deprecated"] == "use --workers instead"

    def test_short_flag(self, spec):
        verbose = spec["runnables"]["pipeline"]["commands"]["run"]["args"]["verbose"]
        assert verbose["short"] == "-v"

    def test_groups_present(self, spec):
        groups = spec["runnables"]["pipeline"]["commands"]["run"]["groups"]
        assert "input-format" in groups
        assert "api-auth" in groups

    def test_exclusive_group(self, spec):
        group = spec["runnables"]["pipeline"]["commands"]["run"]["groups"]["input-format"]
        assert group["exclusive"] is True
        assert "format" in group["args"]

    def test_inclusive_group(self, spec):
        group = spec["runnables"]["pipeline"]["commands"]["run"]["groups"]["api-auth"]
        assert group["inclusive"] is True
        assert "api-key" in group["args"]
