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
        expected = {"input", "tag", "fields", "format", "workers", "batch-size", "dry-run", "verbose", "strict", "api-key", "timeout", "threads", "label"}
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

    def test_string_validation_arg(self, spec):
        label = spec["runnables"]["pipeline"]["commands"]["run"]["args"]["label"]
        assert label["pattern"] == "[a-z][a-z0-9-]+"
        assert label["min_length"] == 3
        assert label["max_length"] == 32

    def test_require_command_runnable(self, spec):
        db = spec["runnables"]["db"]
        assert db["require_command"] is True
        assert "migrate" in db["commands"]
        assert "seed" in db["commands"]
        # Defaults to False where unset (e.g. the pipeline runnable).
        assert spec["runnables"]["pipeline"]["require_command"] is False

    def test_discoverable_field(self, spec):
        # The loader preserves the raw boolean; absent → None (visible downstream).
        assert spec["runnables"]["pipeline"]["discoverable"] is True
        assert spec["runnables"]["db"]["discoverable"] is False
        assert spec["runnables"]["pipeline"]["commands"]["run"]["discoverable"] is None


# ── audit ledgers ─────────────────────────────────────────────────────────────
#
# fixtures/audit/python.jsonl was written by the Python pack, node.jsonl by the
# Node pack (numbers, escapes and key order chosen to hit every place the two
# languages' JSON differs). Each pack must verify both, and extend either.


class TestAuditLedgers:
    @pytest.mark.parametrize("name", ["python.jsonl", "node.jsonl"])
    def test_ledger_verifies(self, name):
        from runspec.auditlog import verify

        result = verify(FIXTURES / "audit" / name)
        assert result["ok"], result
        assert result["records"] == 4

    @pytest.mark.parametrize("name", ["python.jsonl", "node.jsonl"])
    def test_edited_line_is_caught(self, name, tmp_path):
        from runspec.auditlog import verify

        lines = (FIXTURES / "audit" / name).read_text(encoding="utf-8").splitlines(keepends=True)
        lines[2] = lines[2].replace('"checked"', '"changed"', 1)
        bad = tmp_path / "audit.jsonl"
        bad.write_text("".join(lines), encoding="utf-8")
        result = verify(bad)
        assert not result["ok"] and result["first_bad"] == 3

    def test_python_extends_a_node_ledger(self, tmp_path, monkeypatch):
        from runspec.auditlog import audit, verify

        monkeypatch.delenv("RUNSPEC_AUDIT", raising=False)
        (tmp_path / "audit.jsonl").write_bytes((FIXTURES / "audit" / "node.jsonl").read_bytes())
        audit("fixture.append", "succeeded", detail={"n": 1.0}, ledger_dir=tmp_path, syslog=False)
        result = verify(tmp_path / "audit.jsonl")
        assert result["ok"], result
        assert result["records"] == 5
