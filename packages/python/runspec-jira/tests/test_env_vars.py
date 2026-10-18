"""The global args declare the JIRA_* variables the core reads, so a
runspec-console credential applies without a scope and the Env files window
lists them; ``--env`` still reads JIRA_<ENV>_* first."""

import os
import sys
from pathlib import Path

from runspec.testing import no_run_as_check
from runspec_jira_core import JiraConfig

from runspec_jira import cli

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

SPEC = Path(__file__).parent.parent / "runspec_jira" / "runspec.toml"


def _config(monkeypatch, argv: list[str], env: dict[str, str]) -> JiraConfig:
    """The config main() builds for ``argv`` under exactly ``env``."""
    for var in list(os.environ):
        if var.startswith(("JIRA_", "RUNSPEC_JIRA_")):
            monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RUNSPEC_AUDIT", "off")
    seen: list[JiraConfig] = []
    monkeypatch.setattr(cli, "_dispatch", lambda spec, config: seen.append(config) or {})
    monkeypatch.setattr("sys.argv", ["jira", *argv, *["get-issue", "--key", "PROJ-1"]])
    with no_run_as_check():
        cli.main()
    return seen[0]


def test_auth_args_declare_the_env_vars_the_core_reads() -> None:
    args = tomllib.loads(SPEC.read_text())["jira"]["args"]

    def envs(arg: str) -> list[str]:
        env = args[arg]["env"]
        return [env] if isinstance(env, str) else env

    assert envs("jira-url") == ["JIRA_URL"]
    assert envs("jira-pat") == ["JIRA_PERSONAL_TOKEN", "JIRA_PAT", "JIRA_TOKEN"]
    assert envs("jira-user") == ["JIRA_USERNAME", "JIRA_USER"]
    assert envs("jira-password") == ["JIRA_API_TOKEN", "JIRA_PASSWORD"]


def test_console_credential_vars(monkeypatch) -> None:
    # What a username + password credential labelled "Jira" sets.
    env = {"JIRA_URL": "https://jira.dev", "JIRA_USER": "ops", "JIRA_USERNAME": "ops", "JIRA_PASSWORD": "pw"}
    config = _config(monkeypatch, [], env)
    assert (config.url, config.username, config.api_token) == ("https://jira.dev", "ops", "pw")


def test_env_label_beats_unprefixed_aliases(monkeypatch) -> None:
    env = {
        "JIRA_URL": "https://jira.dev",
        "JIRA_PAT": "dev",
        "JIRA_PROD_URL": "https://jira.prod",
        "JIRA_PROD_PERSONAL_TOKEN": "prod",
    }
    config = _config(monkeypatch, ["--env", "prod"], env)
    assert (config.url, config.personal_token) == ("https://jira.prod", "prod")


def test_cli_url_and_secret_channel_override(monkeypatch) -> None:
    env = {"JIRA_URL": "https://jira.dev", "JIRA_PAT": "dev", "RUNSPEC_JIRA_ARG_JIRA_PAT": "from-console"}
    config = _config(monkeypatch, ["--jira-url", "https://jira.cli"], env)
    assert (config.url, config.personal_token) == ("https://jira.cli", "from-console")
