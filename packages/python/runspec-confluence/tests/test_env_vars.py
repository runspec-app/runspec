"""The global args declare the CONFLUENCE_* variables the core reads, so a
runspec-console credential applies without a scope and the Env files window
lists them; ``--env`` still reads CONFLUENCE_<ENV>_* first."""

import os
import sys
from pathlib import Path

from runspec.testing import no_run_as_check
from runspec_confluence_core import ConfluenceConfig

from runspec_confluence import cli

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

SPEC = Path(__file__).parent.parent / "runspec_confluence" / "runspec.toml"


def _config(monkeypatch, argv: list[str], env: dict[str, str]) -> ConfluenceConfig:
    """The config main() builds for ``argv`` under exactly ``env``."""
    for var in list(os.environ):
        if var.startswith(("CONFLUENCE_", "RUNSPEC_CONFLUENCE_")):
            monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RUNSPEC_AUDIT", "off")
    seen: list[ConfluenceConfig] = []
    monkeypatch.setattr(cli, "_dispatch", lambda spec, config: seen.append(config) or {})
    monkeypatch.setattr("sys.argv", ["confluence", *argv, *["get-page", "--page-id", "1"]])
    with no_run_as_check():
        cli.main()
    return seen[0]


def test_auth_args_declare_the_env_vars_the_core_reads() -> None:
    args = tomllib.loads(SPEC.read_text())["confluence"]["args"]

    def envs(arg: str) -> list[str]:
        env = args[arg]["env"]
        return [env] if isinstance(env, str) else env

    assert envs("confluence-url") == ["CONFLUENCE_URL"]
    assert envs("confluence-pat") == ["CONFLUENCE_PERSONAL_TOKEN", "CONFLUENCE_PAT", "CONFLUENCE_TOKEN"]
    assert envs("confluence-user") == ["CONFLUENCE_USERNAME", "CONFLUENCE_USER"]
    assert envs("confluence-password") == ["CONFLUENCE_API_TOKEN", "CONFLUENCE_PASSWORD"]


def test_console_credential_vars(monkeypatch) -> None:
    # What a username + password credential labelled "Confluence" sets.
    env = {"CONFLUENCE_URL": "https://confluence.dev", "CONFLUENCE_USER": "ops", "CONFLUENCE_USERNAME": "ops", "CONFLUENCE_PASSWORD": "pw"}
    config = _config(monkeypatch, [], env)
    assert (config.url, config.username, config.api_token) == ("https://confluence.dev", "ops", "pw")


def test_env_label_beats_unprefixed_aliases(monkeypatch) -> None:
    env = {
        "CONFLUENCE_URL": "https://confluence.dev",
        "CONFLUENCE_PAT": "dev",
        "CONFLUENCE_PROD_URL": "https://confluence.prod",
        "CONFLUENCE_PROD_PERSONAL_TOKEN": "prod",
    }
    config = _config(monkeypatch, ["--env", "prod"], env)
    assert (config.url, config.personal_token) == ("https://confluence.prod", "prod")


def test_cli_url_and_secret_channel_override(monkeypatch) -> None:
    env = {"CONFLUENCE_URL": "https://confluence.dev", "CONFLUENCE_PAT": "dev", "RUNSPEC_CONFLUENCE_ARG_CONFLUENCE_PAT": "from-console"}
    config = _config(monkeypatch, ["--confluence-url", "https://confluence.cli"], env)
    assert (config.url, config.personal_token) == ("https://confluence.cli", "from-console")
