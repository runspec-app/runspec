"""
Tests for the `password` type and the `hint` arg field (runspec 0.30.0).

The password type behaves like `str` for coercion/validation but is refused on
the command line, resolved from the environment, omitted from agent schemas, and
redacted from logs. `hint` is advisory UI placeholder text.
"""

from __future__ import annotations

import textwrap

import pytest

import runspec
from runspec.cli import _build_schema
from runspec.types import coerce


@pytest.fixture()
def spec_dir(tmp_path, monkeypatch):
    (tmp_path / "runspec.toml").write_text(
        textwrap.dedent("""\
            [deploy]
            description = "Deploy"
            [deploy.args.host]
            type = "str"
            hint = "web-01.example.com"
            required = true
            [deploy.args.secret]
            type = "password"
            description = "DB password"
        """),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── coercion ──────────────────────────────────────────────────────────────────


class TestPasswordCoercion:
    def test_password_coerces_like_str(self):
        assert coerce("hunter2", {"type": "password", "name": "secret"}) == "hunter2"

    def test_password_honours_min_length(self):
        with pytest.raises(ValueError):
            coerce("ab", {"type": "password", "name": "secret", "min_length": 8})

    def test_password_in_registry(self):
        assert "password" in runspec.types.list_types()


# ── refused on the command line ─────────────────────────────────────────────────


class TestPasswordRefusedOnCli:
    def test_space_form_refused(self, spec_dir, capsys):
        with pytest.raises(SystemExit) as exc:
            runspec.parse(script_name="deploy", argv=["--host", "h", "--secret", "x"])
        assert exc.value.code == 1
        assert "cannot be passed on the command line" in capsys.readouterr().out

    def test_equals_form_refused(self, spec_dir, capsys):
        with pytest.raises(SystemExit):
            runspec.parse(script_name="deploy", argv=["--host", "h", "--secret=x"])
        assert "cannot be passed on the command line" in capsys.readouterr().out


# ── resolved from the environment ────────────────────────────────────────────────


class TestPasswordFromEnv:
    def test_auto_env_var_resolves(self, spec_dir, monkeypatch):
        monkeypatch.setenv("RUNSPEC_DEPLOY_ARG_SECRET", "fromenv")
        result = runspec.parse(script_name="deploy", argv=["--host", "h"])
        assert result.secret.value == "fromenv"
        assert result.secret.source == "env"


# ── hint plumbing ───────────────────────────────────────────────────────────────


class TestHint:
    def test_hint_on_arg(self, spec_dir, monkeypatch):
        monkeypatch.setenv("RUNSPEC_DEPLOY_ARG_SECRET", "x")
        result = runspec.parse(script_name="deploy", argv=["--host", "h"])
        assert result.host.hint == "web-01.example.com"
        assert result.secret.hint is None


# ── load_spec never prompts ─────────────────────────────────────────────────────


class TestLoadSpecNoPrompt:
    def test_load_spec_does_not_prompt_even_on_a_tty(self, spec_dir, monkeypatch):
        import sys

        # Pretend we're on an interactive terminal — without the load_spec gate,
        # the required password would trigger getpass here. (load_spec still
        # exits on the required args during validation; the point is that getpass
        # is never reached.)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(sys.stderr, "isatty", lambda: True)

        def _boom(*_a, **_k):  # pragma: no cover - must not be called
            raise AssertionError("load_spec must not prompt for secrets")

        monkeypatch.setattr("getpass.getpass", _boom)
        with pytest.raises(SystemExit):
            runspec.load_spec(script_name="deploy", config_path=spec_dir / "runspec.toml")


# ── omitted from emitted agent schemas ───────────────────────────────────────────


class TestPasswordSchemaExclusion:
    def test_password_arg_not_in_schema(self):
        script = {
            "description": "d",
            "args": {
                "host": {"name": "host", "type": "str"},
                "secret": {"name": "secret", "type": "password"},
            },
        }
        schema = _build_schema("deploy", script, "mcp")
        props = schema["inputSchema"]["properties"]
        assert "host" in props
        assert "secret" not in props
        assert "secret" not in schema["inputSchema"].get("required", [])
