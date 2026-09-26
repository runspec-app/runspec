"""An unscoped credential auto-applies only where a runnable declares it.

It used to mean "every runnable on every host": an operator who saved a
credential without setting a scope (e.g. an API key meant for the console, or a
token for one tool) had its name-derived env vars injected into every agent and
MCP-server run across the whole fleet. Now a credential with no scope reaches a
run only when the runnable's own interface asks for it — an arg ``env`` equal to
one of the credential's env var names, or an arg its ``username_arg`` /
``secret_arg`` binds. ``["*"]`` restores "everywhere" deliberately.
"""

from __future__ import annotations

from typing import Any

from runspec_console.bridge import Bridge


class _Stub:
    _scoped_credentials = Bridge._scoped_credentials
    _scope_matches = staticmethod(Bridge._scope_matches)
    _has_scope = staticmethod(Bridge._has_scope)
    _declared_use = staticmethod(Bridge._declared_use)
    _declared_interface = Bridge._declared_interface

    def __init__(
        self, credentials: list[dict[str, Any]], runnables: list[dict[str, Any]]
    ):
        self._credentials = credentials
        self._runnables_cache = runnables


def _cred(label: str, kind: str = "token", **extra: Any) -> dict[str, Any]:
    return {
        "id": label.lower().replace(" ", "-"),
        "label": label,
        "kind": kind,
        **extra,
    }


RUNNABLES = [
    {"name": "disk-usage", "host": "web-1", "args": []},
    {
        "name": "jsm",
        "host": "web-1",
        "args": [{"name": "env", "type": "choice"}],
        "commands": {
            "raise": {"name": "raise", "args": [{"name": "summary", "env": "JSM_PAT"}]},
        },
    },
    {
        "name": "deploy",
        "host": "web-1",
        "args": [{"name": "password", "type": "password"}],
    },
    {
        "name": "legacy",
        "host": "web-1",
        "args": [{"name": "token", "env": ["LEGACY_TOKEN", "OTHER"]}],
    },
]


def _labels(stub: _Stub, host: str, runnable: str) -> list[str]:
    return [c["label"] for c in stub._scoped_credentials(host, runnable)]


def test_unscoped_credential_no_longer_reaches_every_runnable():
    stub = _Stub([_cred("Anthropic")], RUNNABLES)
    assert _labels(stub, "web-1", "disk-usage") == []
    assert _labels(stub, "web-1", "deploy") == []


def test_unscoped_credential_applies_where_an_arg_declares_its_env_var():
    # "JSM" (token) sets JSM_TOKEN / JSM_PAT; a subcommand arg declares JSM_PAT.
    stub = _Stub([_cred("JSM")], RUNNABLES)
    assert _labels(stub, "web-1", "jsm") == ["JSM"]
    assert _labels(stub, "web-1", "disk-usage") == []


def test_env_declared_as_a_list_and_case_insensitive():
    stub = _Stub([_cred("legacy")], RUNNABLES)
    assert _labels(stub, "web-1", "legacy") == ["legacy"]


def test_unscoped_credential_applies_where_it_binds_a_declared_arg():
    stub = _Stub([_cred("Deploy key", secret_arg="password")], RUNNABLES)
    assert _labels(stub, "web-1", "deploy") == ["Deploy key"]
    assert _labels(stub, "web-1", "disk-usage") == []


def test_declared_use_is_per_host():
    # The same runnable name on a host where it isn't discovered declares nothing.
    stub = _Stub([_cred("JSM")], RUNNABLES)
    assert _labels(stub, "web-2", "jsm") == []


def test_star_scope_restores_everywhere_deliberately():
    stub = _Stub([_cred("Everywhere", scope_hosts=["*"])], RUNNABLES)
    assert _labels(stub, "web-1", "disk-usage") == ["Everywhere"]
    assert _labels(stub, "other-host", "anything") == ["Everywhere"]


def test_scoped_credentials_behave_as_before():
    stub = _Stub(
        [
            _cred("Web only", scope_hosts=["web-*"]),
            _cred("Deploy only", scope_runnables=["deploy"]),
            _cred("Both", scope_hosts=["db-*"], scope_runnables=["deploy"]),
        ],
        RUNNABLES,
    )
    assert _labels(stub, "web-1", "disk-usage") == ["Web only"]
    assert _labels(stub, "web-1", "deploy") == ["Web only", "Deploy only"]
    assert _labels(stub, "db-1", "deploy") == ["Deploy only", "Both"]


def test_blank_scope_entries_count_as_unscoped():
    stub = _Stub([_cred("Blank", scope_hosts=["  "], scope_runnables=[])], RUNNABLES)
    assert _labels(stub, "web-1", "disk-usage") == []
