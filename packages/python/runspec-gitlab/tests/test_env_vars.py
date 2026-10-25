"""The global args declare the GITLAB_* variables the core reads, so a
runspec-console credential labelled GitLab applies without a scope (the agent
gets it, not only a Forms pick); ``--env`` still reads GITLAB_<ENV>_* first."""

import os
import sys
from pathlib import Path

from runspec.testing import no_run_as_check
from runspec_gitlab_core import GitlabConfig

from runspec_gitlab import cli

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

SPEC = Path(__file__).parent.parent / "runspec_gitlab" / "runspec.toml"


def _config(monkeypatch, argv: list[str], env: dict[str, str]) -> GitlabConfig:
    """The config main() builds for ``argv`` under exactly ``env``."""
    for var in list(os.environ):
        if var.startswith(("GITLAB_", "RUNSPEC_GITLAB_")):
            monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RUNSPEC_AUDIT", "off")
    seen: list[GitlabConfig] = []
    monkeypatch.setattr(cli, "_dispatch", lambda spec, config: seen.append(config) or {})
    monkeypatch.setattr("sys.argv", ["gitlab", *argv, "whoami"])
    with no_run_as_check():
        cli.main()
    return seen[0]


def test_auth_args_declare_the_env_vars_the_core_reads() -> None:
    args = tomllib.loads(SPEC.read_text())["gitlab"]["args"]

    def envs(arg: str) -> list[str]:
        env = args[arg]["env"]
        return [env] if isinstance(env, str) else env

    assert envs("gitlab-url") == ["GITLAB_URL"]
    assert envs("gitlab-token") == ["GITLAB_TOKEN", "GITLAB_PAT"]
    assert args["gitlab-token"]["type"] == "password"


def test_console_credential_vars(monkeypatch) -> None:
    # What a token credential labelled "GitLab" sets.
    config = _config(monkeypatch, [], {"GITLAB_TOKEN": "glpat-1", "GITLAB_PAT": "glpat-1"})
    assert (config.base_url, config.token) == ("https://gitlab.com", "glpat-1")


def test_env_label_beats_unprefixed_aliases(monkeypatch) -> None:
    env = {
        "GITLAB_URL": "https://gitlab.dev",
        "GITLAB_PAT": "dev",
        "GITLAB_PROD_URL": "https://gitlab.prod",
        "GITLAB_PROD_TOKEN": "prod",
    }
    config = _config(monkeypatch, ["--env", "prod"], env)
    assert (config.base_url, config.token) == ("https://gitlab.prod", "prod")


def test_cli_url_and_secret_channel_override(monkeypatch) -> None:
    env = {"GITLAB_URL": "https://gitlab.dev", "GITLAB_TOKEN": "dev", "RUNSPEC_GITLAB_ARG_GITLAB_TOKEN": "from-console"}
    config = _config(monkeypatch, ["--gitlab-url", "https://gitlab.cli"], env)
    assert (config.base_url, config.token) == ("https://gitlab.cli", "from-console")
