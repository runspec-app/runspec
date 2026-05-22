"""
Tests for parser._parse_argv — raw argv → dict conversion.
Tests for parse() — clean error handling (no tracebacks to user).
"""

from __future__ import annotations

import textwrap

import pytest

import runspec
from runspec.parser import _apply_env, _parse_argv

# ── parse() error handling ────────────────────────────────────────────────────


@pytest.fixture()
def spec_dir(tmp_path, monkeypatch):
    (tmp_path / "runspec.toml").write_text(
        textwrap.dedent("""\
            [clean]
            [clean.args.directory]
            type = "path"
            default = "."
            [clean.args.count]
            type = "int"
            default = 10
            [clean.args.format]
            type = "choice"
            options = ["text", "json"]
            default = "text"
            [clean.args.name]
            type = "str"
            required = true
        """),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestParseCleanErrors:
    def test_missing_required_arg_exits_cleanly(self, spec_dir, capsys):
        with pytest.raises(SystemExit) as exc:
            runspec.parse(script_name="clean", argv=[])
        assert exc.value.code == 1
        assert "Missing required argument" in capsys.readouterr().out

    def test_invalid_type_exits_cleanly(self, spec_dir, capsys):
        with pytest.raises(SystemExit) as exc:
            runspec.parse(script_name="clean", argv=["--name", "x", "--count", "abc"])
        assert exc.value.code == 1
        assert "--count" in capsys.readouterr().out

    def test_invalid_choice_exits_cleanly(self, spec_dir, capsys):
        with pytest.raises(SystemExit) as exc:
            runspec.parse(script_name="clean", argv=["--name", "x", "--format", "xml"])
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "--format" in out
        assert "text" in out

    def test_no_config_exits_cleanly(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            runspec.parse(script_name="clean", argv=[])
        assert exc.value.code == 1
        assert "runspec.toml" in capsys.readouterr().out

    def test_valid_args_return_runspec(self, spec_dir):
        result = runspec.parse(script_name="clean", argv=["--name", "hello"])
        assert result.name == "hello"

    def test_runspec_config_env_var_overrides_walk_up(self, tmp_path, monkeypatch):
        """RUNSPEC_CONFIG points parse() at a specific TOML, bypassing find_config."""
        # Set up the actual spec at a path that cwd-walk would NOT find
        spec_dir = tmp_path / "actual" / "package"
        spec_dir.mkdir(parents=True)
        (spec_dir / "runspec.toml").write_text(
            textwrap.dedent("""\
                [greet]
                [greet.args]
                name = {type = "str", required = true}
            """),
            encoding="utf-8",
        )
        # cwd is somewhere unrelated — no runspec.toml on the walk-up path
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        monkeypatch.chdir(unrelated)
        # Set the env var explicitly
        monkeypatch.setenv("RUNSPEC_CONFIG", str(spec_dir / "runspec.toml"))

        result = runspec.parse(script_name="greet", argv=["--name", "hello"])
        assert result.name == "hello"

    def test_parse_finds_config_next_to_caller_module(self, tmp_path, monkeypatch):
        """Simulates the installed-package case: `clean` is invoked from cwd
        that has no runspec.toml, but the calling module ships one beside it.
        parse() should find that one via call-stack introspection."""
        import importlib
        import sys

        pkg_dir = tmp_path / "installed_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
        (pkg_dir / "runspec.toml").write_text(
            textwrap.dedent("""\
                [clean]
                [clean.args.name]
                type = "str"
                required = true
            """),
            encoding="utf-8",
        )
        (pkg_dir / "entrypoint.py").write_text(
            textwrap.dedent("""\
                import runspec

                def main(argv):
                    return runspec.parse(script_name="clean", argv=argv)
            """),
            encoding="utf-8",
        )

        unrelated = tmp_path / "elsewhere"
        unrelated.mkdir()
        monkeypatch.chdir(unrelated)
        monkeypatch.syspath_prepend(str(tmp_path))

        try:
            mod = importlib.import_module("installed_pkg.entrypoint")
            result = mod.main(["--name", "hello"])
        finally:
            sys.modules.pop("installed_pkg.entrypoint", None)
            sys.modules.pop("installed_pkg", None)

        assert result.name.value == "hello"
        assert result.__runspec_source__ == pkg_dir / "runspec.toml"

    def test_explicit_config_path_beats_env_var(self, tmp_path, monkeypatch):
        """parse(config_path=...) wins over RUNSPEC_CONFIG."""
        env_spec_dir = tmp_path / "env"
        env_spec_dir.mkdir()
        (env_spec_dir / "runspec.toml").write_text(
            '[greet]\n[greet.args]\nname = {type = "str", default = "from-env"}\n',
            encoding="utf-8",
        )
        explicit_spec_dir = tmp_path / "explicit"
        explicit_spec_dir.mkdir()
        (explicit_spec_dir / "runspec.toml").write_text(
            '[greet]\n[greet.args]\nname = {type = "str", default = "from-explicit"}\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("RUNSPEC_CONFIG", str(env_spec_dir / "runspec.toml"))

        result = runspec.parse(script_name="greet", argv=[], config_path=explicit_spec_dir / "runspec.toml")
        assert result.name.value == "from-explicit"


class TestPrintHelp:
    """Tests for the spec-driven --help renderer."""

    def test_help_renders_args_and_examples(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "runspec.toml").write_text(
            textwrap.dedent("""\
                [greet]
                description = "Greet someone"
                examples = [
                  {cmd = "greet --name Jason", description = "Greet Jason"},
                  {cmd = "greet"},
                ]
                [greet.args]
                name = {type = "str", description = "Person to greet", required = true}
            """),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            runspec.parse(script_name="greet", argv=["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Usage: greet" in out
        assert "Greet someone" in out
        assert "--name" in out
        assert "Person to greet" in out
        assert "Examples:" in out
        assert "greet --name Jason" in out
        assert "# Greet Jason" in out

    def test_help_renders_commands_section(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "runspec.toml").write_text(
            textwrap.dedent("""\
                [pipeline]
                description = "Run a pipeline"
                examples = [{cmd = "pipeline run", description = "Run it"}]
                [pipeline.commands.run]
                description = "Run the pipeline"
                [pipeline.commands.validate]
                description = "Validate without running"
            """),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            runspec.parse(script_name="pipeline", argv=["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Usage: pipeline <command>" in out
        assert "Commands:" in out
        assert "run" in out
        assert "Run the pipeline" in out
        assert "validate" in out
        assert "Validate without running" in out
        assert "Run 'pipeline <command> --help'" in out

    def test_help_for_subcommand_shows_subcommand_name(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "runspec.toml").write_text(
            textwrap.dedent("""\
                [pipeline]
                [pipeline.commands.run]
                description = "Run the pipeline"
                [pipeline.commands.run.args]
                input = {type = "path"}
            """),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            runspec.parse(script_name="pipeline", argv=["run", "--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Usage: pipeline run" in out
        assert "--input" in out

    def test_help_renders_positionals_and_rest(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "runspec.toml").write_text(
            textwrap.dedent("""\
                [jump]
                description = "Jump to a remote"
                [jump.args]
                fmt        = {type = "choice", description = "Format", options = ["text", "json"], default = "text"}
                host       = {type = "str",  description = "Target host", position = 1, required = false}
                tool       = {type = "str",  description = "Tool to run", position = 2, required = false}
                extra-args = {type = "rest", description = "Args passed to the tool"}
            """),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            runspec.parse(script_name="jump", argv=["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out

        # Usage line should show positionals as <name> and rest as [-- <name>...]
        assert "[<host>]" in out
        assert "[<tool>]" in out
        assert "[-- <extra-args>...]" in out

        # Should have a Positional arguments section
        assert "Positional arguments:" in out
        assert "<host>" in out
        assert "Target host" in out
        assert "-- <extra-args>..." in out
        assert "Args passed to the tool" in out

        # When positionals exist, flag header is "Options:" not "Arguments:"
        assert "Options:" in out
        assert "\nArguments:\n" not in out

    def test_help_renders_short_flags(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "runspec.toml").write_text(
            textwrap.dedent("""\
                [deploy]
                description = "Deploy something"
                [deploy.args]
                verbose = {type = "flag", short = "-v", default = false, description = "Verbose mode"}
                env     = {type = "str",  short = "-e", required = true,  description = "Target env"}
            """),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            runspec.parse(script_name="deploy", argv=["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        # Short form rendered alongside the long form
        assert "-v, --verbose" in out
        assert "-e, --env" in out

    def test_help_without_examples_omits_section(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "runspec.toml").write_text(
            textwrap.dedent("""\
                [greet]
                description = "no examples"
                [greet.args]
                name = {type = "str", default = "world"}
            """),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            runspec.parse(script_name="greet", argv=["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Examples:" not in out


class TestParseArgvHappyPath:
    def test_string_arg(self):
        result = _parse_argv(["--env", "prod"], {"env": {"type": "str"}})
        assert result == {"env": "prod"}

    def test_int_arg(self):
        result = _parse_argv(["--count", "5"], {"count": {"type": "int"}})
        assert result == {"count": "5"}

    def test_float_arg(self):
        result = _parse_argv(["--threshold", "0.9"], {"threshold": {"type": "float"}})
        assert result == {"threshold": "0.9"}

    def test_path_arg(self):
        result = _parse_argv(["--directory", "/tmp"], {"directory": {"type": "path"}})
        assert result == {"directory": "/tmp"}

    def test_choice_arg(self):
        result = _parse_argv(["--format", "json"], {"format": {"type": "choice", "options": ["text", "json"]}})
        assert result == {"format": "json"}

    def test_flag_no_value(self):
        result = _parse_argv(["--dry-run"], {"dry-run": {"type": "flag"}})
        assert result == {"dry_run": True}

    def test_key_equals_value(self):
        result = _parse_argv(["--env=prod"], {"env": {"type": "str"}})
        assert result == {"env": "prod"}

    def test_hyphenated_name_normalised(self):
        result = _parse_argv(["--dry-run"], {"dry-run": {"type": "flag"}})
        assert "dry_run" in result

    def test_short_flag(self):
        result = _parse_argv(["-n", "42"], {"count": {"type": "int", "short": "-n"}})
        assert result == {"count": "42"}

    def test_short_flag_boolean(self):
        result = _parse_argv(["-v"], {"verbose": {"type": "flag", "short": "-v"}})
        assert result == {"verbose": True}

    def test_unknown_flag_exits_cleanly(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _parse_argv(["--unknown"], {"env": {"type": "str"}})
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "Unknown argument" in out
        assert "--unknown" in out

    def test_unknown_key_equals_value_exits_cleanly(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _parse_argv(["--unknown=foo"], {"env": {"type": "str"}})
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "--unknown" in out

    def test_extra_positional_exits_cleanly(self, capsys):
        # spec only has one positional; passing two errors on the extra
        spec = {"target": {"type": "str", "position": 1}}
        with pytest.raises(SystemExit) as exc:
            _parse_argv(["foo", "bar"], spec)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "Unknown argument" in out
        assert "bar" in out

    def test_unknown_with_no_positionals_exits_cleanly(self, capsys):
        # No positionals declared at all — bare token is an error
        with pytest.raises(SystemExit) as exc:
            _parse_argv(["bareword", "--env", "prod"], {"env": {"type": "str"}})
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "bareword" in out

    def test_unknown_after_separator_with_no_rest_arg(self, capsys):
        # `--` used but no rest arg declared — trailing tokens are errors
        with pytest.raises(SystemExit) as exc:
            _parse_argv(["--env", "prod", "--", "foo", "bar"], {"env": {"type": "str"}})
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "foo" in out or "bar" in out

    def test_error_lists_valid_flags(self, capsys):
        with pytest.raises(SystemExit):
            _parse_argv(["--xyz"], {"env": {"type": "str"}, "verbose": {"type": "flag"}})
        out = capsys.readouterr().out
        assert "--env" in out
        assert "--verbose" in out


class TestParseArgvCollisionDetection:
    """Spec-level collisions caught at parse time — no silent shadowing."""

    def test_duplicate_short_errors(self, capsys):
        spec = {
            "verbose": {"type": "flag", "short": "-v"},
            "version": {"type": "flag", "short": "-v"},  # collides
        }
        with pytest.raises(SystemExit) as exc:
            _parse_argv([], spec)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "-v" in out
        assert "verbose" in out
        assert "version" in out

    def test_short_h_reserved(self, capsys):
        spec = {"help-mode": {"type": "flag", "short": "-h"}}
        with pytest.raises(SystemExit) as exc:
            _parse_argv([], spec)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "-h" in out
        assert "reserved" in out.lower()

    def test_duplicate_position_errors(self, capsys):
        spec = {
            "first": {"type": "str", "position": 1},
            "second": {"type": "str", "position": 1},  # collides
        }
        with pytest.raises(SystemExit) as exc:
            _parse_argv([], spec)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "Position 1" in out or "position 1" in out.lower()

    def test_multiple_rest_args_errors(self, capsys):
        spec = {
            "extra1": {"type": "rest"},
            "extra2": {"type": "rest"},  # second rest arg
        }
        with pytest.raises(SystemExit) as exc:
            _parse_argv([], spec)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "rest" in out.lower()
        assert "extra1" in out
        assert "extra2" in out

    def test_no_collision_when_shorts_differ(self):
        # Sanity: distinct shorts work fine
        spec = {
            "verbose": {"type": "flag", "short": "-v"},
            "env": {"type": "str", "short": "-e"},
        }
        # Should not raise
        result = _parse_argv(["-v", "-e", "prod"], spec)
        assert result["verbose"] is True
        assert result["env"] == "prod"

    def test_absent_arg_is_none(self):
        result = _parse_argv([], {"env": {"type": "str"}})
        assert result == {"env": None}

    def test_multiple_values_accumulated(self):
        result = _parse_argv(["--file", "a.txt", "--file", "b.txt"], {"file": {"type": "str", "multiple": True}})
        assert result == {"file": ["a.txt", "b.txt"]}


class TestParseArgvMissingValue:
    def test_str_arg_missing_value(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _parse_argv(["--env"], {"env": {"type": "str"}})
        assert exc.value.code == 1
        assert "--env requires a value" in capsys.readouterr().out

    def test_int_arg_missing_value(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _parse_argv(["--count"], {"count": {"type": "int"}})
        assert exc.value.code == 1
        assert "--count requires a value" in capsys.readouterr().out

    def test_float_arg_missing_value(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _parse_argv(["--threshold"], {"threshold": {"type": "float"}})
        assert exc.value.code == 1
        assert "--threshold requires a value" in capsys.readouterr().out

    def test_path_arg_missing_value(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _parse_argv(["--directory"], {"directory": {"type": "path"}})
        assert exc.value.code == 1
        assert "--directory requires a value" in capsys.readouterr().out

    def test_choice_arg_missing_value_shows_options(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _parse_argv(["--format"], {"format": {"type": "choice", "options": ["text", "json"]}})
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "--format requires a value" in out
        assert "text" in out
        assert "json" in out

    def test_missing_value_when_followed_by_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _parse_argv(
                ["--format", "--delete"],
                {"format": {"type": "choice", "options": ["text", "json"]}, "delete": {"type": "flag"}},
            )
        assert exc.value.code == 1
        assert "--format requires a value" in capsys.readouterr().out

    def test_flag_type_is_exempt(self):
        # flag args never need a value — this must NOT raise
        result = _parse_argv(["--verbose"], {"verbose": {"type": "flag"}})
        assert result == {"verbose": True}


# ── _apply_env — env resolution tier ─────────────────────────────────────────


def _spec(**kwargs):
    """Minimal normalised arg spec for _apply_env tests."""
    return {"type": "int", "env": None, **kwargs}


class TestApplyEnv:
    def test_runspec_arg_var_provides_default(self, monkeypatch):
        """RUNSPEC_ARG_<ARGNAME> is picked up automatically when no CLI value."""
        monkeypatch.setenv("RUNSPEC_ARG_QUALITY", "95")
        result = _apply_env({"quality": None}, {"quality": _spec()})
        assert result["quality"] == "95"

    def test_cli_value_wins_over_runspec_arg_var(self, monkeypatch):
        """Explicit CLI arg takes precedence — RUNSPEC_ARG_* is ignored."""
        monkeypatch.setenv("RUNSPEC_ARG_QUALITY", "95")
        result = _apply_env({"quality": "80"}, {"quality": _spec()})
        assert result["quality"] == "80"

    def test_developer_alias_fallback(self, monkeypatch):
        """env = [...] alias is checked when RUNSPEC_ARG_* is not set."""
        monkeypatch.setenv("CI_QUALITY", "70")
        monkeypatch.delenv("RUNSPEC_ARG_QUALITY", raising=False)
        result = _apply_env({"quality": None}, {"quality": _spec(env=["CI_QUALITY"])})
        assert result["quality"] == "70"

    def test_runspec_arg_var_wins_over_developer_alias(self, monkeypatch):
        """RUNSPEC_ARG_* takes precedence over developer-declared env aliases."""
        monkeypatch.setenv("RUNSPEC_ARG_QUALITY", "95")
        monkeypatch.setenv("CI_QUALITY", "70")
        result = _apply_env({"quality": None}, {"quality": _spec(env=["CI_QUALITY"])})
        assert result["quality"] == "95"

    def test_hyphenated_arg_name(self, monkeypatch):
        """Hyphens in arg names become underscores in the RUNSPEC_ARG_* key."""
        monkeypatch.setenv("RUNSPEC_ARG_DRY_RUN", "1")
        result = _apply_env({"dry_run": None}, {"dry-run": _spec(type="flag")})
        assert result["dry_run"] == "1"

    def test_multiple_aliases_first_match_wins(self, monkeypatch):
        """When multiple aliases declared, first one found is used."""
        monkeypatch.setenv("SECOND_VAR", "second")
        monkeypatch.delenv("RUNSPEC_ARG_QUALITY", raising=False)
        monkeypatch.delenv("FIRST_VAR", raising=False)
        result = _apply_env({"quality": None}, {"quality": _spec(env=["FIRST_VAR", "SECOND_VAR"])})
        assert result["quality"] == "second"
