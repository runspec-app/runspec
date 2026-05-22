"""
Tests for loader.py — load_raw for runspec.toml format.
"""

from pathlib import Path

import pytest

from runspec.loader import load_raw

FIXTURES = Path(__file__).parents[4] / "tests" / "integration" / "fixtures"


class TestRunspecFormat:
    def test_top_level_section_becomes_runnable(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
[greet]
description = "Say hello"
""")
        result = load_raw(p)
        assert "greet" in result["runnables"]
        assert result["runnables"]["greet"]["description"] == "Say hello"

    def test_config_section_normalised(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
[config]
autonomy-default = "supervised"

[greet]
description = "hi"
""")
        result = load_raw(p)
        assert result["config"]["autonomy_default"] == "supervised"
        assert "config" not in result["runnables"]

    def test_config_defaults_when_absent(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("[greet]\ndescription = 'hi'\n")
        result = load_raw(p)
        assert result["config"]["autonomy_default"] == "confirm"
        assert result["config"]["version"] == "1"
        assert result["config"]["lang"] is None

    def test_multiple_runnables(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
[greet]
description = "Greet"

[compress]
description = "Compress"
""")
        result = load_raw(p)
        assert set(result["runnables"].keys()) == {"greet", "compress"}

    def test_non_dict_scalar_excluded_from_runnables(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
stray_value = "ignored"

[greet]
description = "hi"
""")
        result = load_raw(p)
        assert "stray_value" not in result["runnables"]
        assert "greet" in result["runnables"]


class TestScriptNormalisation:
    def test_name_injected_into_script(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("[greet]\ndescription = 'hi'\n")
        result = load_raw(p)
        assert result["runnables"]["greet"]["name"] == "greet"

    def test_absent_optional_fields_are_none(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("[greet]\n")
        result = load_raw(p)
        s = result["runnables"]["greet"]
        assert s["description"] is None
        assert s["autonomy"] is None
        assert s["autonomy_reason"] is None
        assert s["args"] == {}
        assert s["groups"] == {}
        assert s["commands"] == {}

    def test_autonomy_reason_hyphen_normalised(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
[greet]
autonomy-reason = "Writes output files"
""")
        result = load_raw(p)
        assert result["runnables"]["greet"]["autonomy_reason"] == "Writes output files"

    def test_subcommand_recursed(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
[pipeline]
description = "Pipeline tool"

[pipeline.commands.run]
description = "Run it"

[pipeline.commands.run.args]
input = {type = "path"}
""")
        result = load_raw(p)
        commands = result["runnables"]["pipeline"]["commands"]
        assert "run" in commands
        assert commands["run"]["name"] == "run"
        assert "input" in commands["run"]["args"]


class TestExamplesNormalisation:
    def test_examples_inline_table_form(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
[greet]
examples = [
  {cmd = "greet --name Jason", description = "Greet Jason"},
  {cmd = "greet"},
]
""")
        result = load_raw(p)
        examples = result["runnables"]["greet"]["examples"]
        assert examples == [
            {"cmd": "greet --name Jason", "description": "Greet Jason"},
            {"cmd": "greet", "description": ""},
        ]

    def test_examples_bare_string_shorthand(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
[greet]
examples = ["greet", "greet --name Jason"]
""")
        result = load_raw(p)
        examples = result["runnables"]["greet"]["examples"]
        assert examples == [
            {"cmd": "greet", "description": ""},
            {"cmd": "greet --name Jason", "description": ""},
        ]

    def test_examples_default_empty(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text('[greet]\ndescription = "no examples here"\n')
        result = load_raw(p)
        assert result["runnables"]["greet"]["examples"] == []

    def test_examples_dict_without_cmd_skipped(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
[greet]
examples = [
  {description = "missing cmd field"},
  {cmd = "greet"},
]
""")
        result = load_raw(p)
        examples = result["runnables"]["greet"]["examples"]
        assert examples == [{"cmd": "greet", "description": ""}]

    def test_examples_on_subcommand(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
[pipeline]

[pipeline.commands.run]
examples = [{cmd = "pipeline run"}]
""")
        result = load_raw(p)
        cmd = result["runnables"]["pipeline"]["commands"]["run"]
        assert cmd["examples"] == [{"cmd": "pipeline run", "description": ""}]


class TestArgNormalisation:
    def test_bare_value_shorthand_expanded(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
[greet.args]
verbose = false
""")
        result = load_raw(p)
        arg = result["runnables"]["greet"]["args"]["verbose"]
        assert arg["name"] == "verbose"
        assert arg["default"] is False

    def test_inline_table_parsed(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
[greet.args]
workers = {default = 4, range = [1, 32]}
""")
        result = load_raw(p)
        arg = result["runnables"]["greet"]["args"]["workers"]
        assert arg["default"] == 4
        assert arg["range"] == (1, 32)

    def test_range_list_converted_to_tuple(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
[greet.args]
timeout = {default = 30, range = [1, 300]}
""")
        result = load_raw(p)
        assert isinstance(result["runnables"]["greet"]["args"]["timeout"]["range"], tuple)

    def test_absent_fields_default_correctly(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
[greet.args]
name = {type = "str"}
""")
        result = load_raw(p)
        arg = result["runnables"]["greet"]["args"]["name"]
        assert arg["default"] is None
        assert arg["required"] is None
        assert arg["options"] is None
        assert arg["range"] is None
        assert arg["multiple"] is False
        assert arg["delimiter"] is None
        assert arg["short"] is None
        assert arg["env"] is None
        assert arg["deprecated"] is None
        assert arg["autonomy"] is None

    def test_name_injected_into_arg(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
[greet.args]
output = {type = "path"}
""")
        result = load_raw(p)
        assert result["runnables"]["greet"]["args"]["output"]["name"] == "output"

    def test_meta_passed_through(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
[greet.args.server]
options = ["web-01", "web-02"]
[greet.args.server.meta]
web-01 = {datacenter = "us-east"}
web-02 = {datacenter = "us-west"}
""")
        result = load_raw(p)
        meta = result["runnables"]["greet"]["args"]["server"]["meta"]
        assert meta["web-01"]["datacenter"] == "us-east"
        assert meta["web-02"]["datacenter"] == "us-west"

    def test_meta_absent_is_none(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
[greet.args]
name = {type = "str"}
""")
        result = load_raw(p)
        assert result["runnables"]["greet"]["args"]["name"]["meta"] is None


class TestGroupNormalisation:
    def test_exclusive_group(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
[greet.groups.output-fmt]
exclusive = true
args = ["format", "raw"]
""")
        result = load_raw(p)
        group = result["runnables"]["greet"]["groups"]["output-fmt"]
        assert group["name"] == "output-fmt"
        assert group["exclusive"] is True
        assert group["args"] == ["format", "raw"]
        assert group["inclusive"] is False

    def test_hyphenated_group_fields_normalised(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
[greet.groups.auth]
at-least-one = true
exactly-one = false
args = ["api-key", "token"]
""")
        result = load_raw(p)
        group = result["runnables"]["greet"]["groups"]["auth"]
        assert group["at_least_one"] is True
        assert group["exactly_one"] is False

    def test_group_condition_and_requires(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("""
[greet.groups.creds]
args = ["api-key"]
if = "api-key"
requires = ["endpoint"]
""")
        result = load_raw(p)
        group = result["runnables"]["greet"]["groups"]["creds"]
        assert group["condition"] == "api-key"
        assert group["requires"] == ["endpoint"]


class TestErrorHandling:
    def test_missing_file_raises(self, tmp_path):
        p = tmp_path / "nonexistent.toml"
        with pytest.raises(FileNotFoundError):
            load_raw(p)


class TestLoggingNormalisation:
    def test_logging_absent_returns_none(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("[greet]\ndescription = 'hi'\n")
        assert load_raw(p)["config"]["logging"] is None

    def test_logging_defaults(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("[config.logging]\n\n[greet]\n")
        cfg = load_raw(p)["config"]["logging"]
        assert cfg == {"rotate": "midnight", "keep": 7, "summary": True}

    def test_logging_summary_explicit_false(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("[config.logging]\nsummary = false\n\n[greet]\n")
        assert load_raw(p)["config"]["logging"]["summary"] is False

    def test_logging_summary_explicit_true(self, tmp_path):
        p = tmp_path / "runspec.toml"
        p.write_text("[config.logging]\nsummary = true\n\n[greet]\n")
        assert load_raw(p)["config"]["logging"]["summary"] is True


class TestIntegrationFixtures:
    def test_simple_toml(self):
        result = load_raw(FIXTURES / "simple.toml")
        assert result["config"]["autonomy_default"] == "confirm"

        greet = result["runnables"]["greet"]
        assert greet["description"] == "Greet someone from the command line"
        assert greet["autonomy"] == "autonomous"

        args = greet["args"]
        assert args["name"]["type"] == "str"
        assert args["loud"]["default"] is False
        assert args["times"]["default"] == 1

    def test_complex_toml_config(self):
        result = load_raw(FIXTURES / "complex.toml")
        cfg = result["config"]
        assert cfg["autonomy_default"] == "confirm"
        assert cfg["lang"] == "python"
        assert cfg["version"] == "1"

    def test_complex_toml_subcommands(self):
        result = load_raw(FIXTURES / "complex.toml")
        pipeline = result["runnables"]["pipeline"]
        assert "run" in pipeline["commands"]
        assert "validate" in pipeline["commands"]

    def test_complex_toml_run_command_args(self):
        result = load_raw(FIXTURES / "complex.toml")
        args = result["runnables"]["pipeline"]["commands"]["run"]["args"]

        assert args["input"]["type"] == "path"
        assert args["format"]["options"] == ["json", "csv", "parquet"]
        assert args["format"]["default"] == "json"
        assert args["workers"]["range"] == (1, 32)
        assert args["dry-run"]["default"] is False
        assert args["verbose"]["short"] == "-v"
        assert args["api-key"]["env"] == ["PIPELINE_API_KEY"]
        assert args["api-key"]["autonomy"] == "manual"
        assert args["threads"]["deprecated"] == "use --workers instead"
        assert args["tag"]["multiple"] is True
        assert args["fields"]["delimiter"] == ","

    def test_complex_toml_run_command_metadata(self):
        result = load_raw(FIXTURES / "complex.toml")
        run = result["runnables"]["pipeline"]["commands"]["run"]
        assert run["autonomy"] == "confirm"
        assert run["autonomy_reason"] == "Writes output files and may call external APIs"

    def test_complex_toml_groups(self):
        result = load_raw(FIXTURES / "complex.toml")
        groups = result["runnables"]["pipeline"]["commands"]["run"]["groups"]
        assert groups["input-format"]["exclusive"] is True
        assert groups["input-format"]["args"] == ["format", "raw"]
        assert groups["api-auth"]["inclusive"] is True
        assert groups["api-auth"]["args"] == ["api-key", "api-endpoint"]
