"""
test_secret_files.py — {{secret:<credential>}} placeholders in a run's args.

The console resolves each placeholder to a credential the operator allowed into
files (``write_to_files``) on that host and sends the value as
RUNSPEC_SECRET_<NAME> (stdin env frame); the host's runnable fills it in. Saved
secrets are masked out of tool output, and a call carrying a placeholder is
always at least confirm-gated.
"""

from __future__ import annotations

import json

import pytest

import runspec_console.credentials as cred_mod
from runspec_console import secret_files as sf
from runspec_console.bridge import Bridge
from runspec_console.credentials import (
    KEYRING_SERVICE,
    load_credentials,
    save_credentials,
)

SECRETS = {"grafana-cloud": "glc_s3cr3t-token", "jira": "jira-pat-value"}


def _cred(cid, **kw):
    base = {
        "id": cid,
        "label": cid,
        "kind": "token",
        "username": "",
        "scope_hosts": [],
        "scope_runnables": [],
    }
    base.update(kw)
    return base


CREDS = [
    _cred(
        "grafana-cloud",
        label="Grafana Cloud",
        username="123456",
        write_to_files=True,
        scope_hosts=["web-*", "local"],
    ),
    _cred("jira", label="Jira"),
]


def _resolve(args, host="web-01"):
    return sf.resolve(args, host, CREDS, SECRETS.get)


# ── pure ────────────────────────────────────────────────────────────────────


def test_env_name_matches_runspec_fs_core():
    # the host side derives the same name from the same placeholder text
    assert sf.env_name("grafana-cloud") == "RUNSPEC_SECRET_GRAFANA_CLOUD"
    assert (
        sf.env_name("grafana-cloud", "username")
        == "RUNSPEC_SECRET_GRAFANA_CLOUD__USERNAME"
    )
    try:
        from runspec_fs_core.secrets import env_name as core_env_name
    except ImportError:
        return
    for ref in ("grafana-cloud", "Grafana.Cloud", "svc_prod-eu"):
        assert sf.env_name(ref) == core_env_name(ref)
        assert sf.env_name(ref, "username") == core_env_name(ref, "username")


def test_resolve_sends_only_named_credentials():
    res = _resolve(
        {
            "content": 'u="{{secret:grafana-cloud.username}}" p="{{secret:grafana-cloud}}"'
        }
    )
    assert res.errors == []
    assert res.env == {
        "RUNSPEC_SECRET_GRAFANA_CLOUD": "glc_s3cr3t-token",
        "RUNSPEC_SECRET_GRAFANA_CLOUD__USERNAME": "123456",
    }
    assert res.ids == ["grafana-cloud"] and res.labels == ["Grafana Cloud"]


def test_resolve_by_label_spelling():
    res = _resolve({"new_text": ["{{secret:Grafana-Cloud}}"]})
    assert res.env == {"RUNSPEC_SECRET_GRAFANA_CLOUD": "glc_s3cr3t-token"}


def test_credential_not_opted_in_is_refused():
    res = _resolve({"content": "{{secret:jira}}"})
    assert not res.env
    assert (
        "isn't allowed into files" in res.error
        and "Settings → Credentials" in res.error
    )


def test_host_outside_scope_is_refused():
    res = _resolve({"content": "{{secret:grafana-cloud}}"}, host="db-01")
    assert "isn't scoped to host 'db-01'" in res.error and not res.env


def test_unknown_credential_lists_allowed_ones():
    res = _resolve({"content": "{{secret:nope}}"})
    assert "no saved credential named 'nope'" in res.error
    assert "{{secret:grafana-cloud}} (Grafana Cloud)" in res.error


def test_mask_plain_and_json_escaped():
    secrets = {"x": 'qz"xy\\vk-long'}
    out = sf.mask(json.dumps({"content": 'pw = qz"xy\\vk-long'}), secrets)
    assert "vk-long" not in out and "{{secret:x}}" in out
    assert (
        sf.mask("token glc_s3cr3t-token here", SECRETS)
        == "token {{secret:grafana-cloud}} here"
    )
    assert sf.mask("abc", {"short": "abc"}) == "abc"  # too short to mask


# ── bridge wiring ───────────────────────────────────────────────────────────


class _FakeKR:
    def get_password(self, service, name):
        assert service == KEYRING_SERVICE
        return SECRETS.get(name)


class _B:
    _secret_file_env = Bridge._secret_file_env
    _mask_known_secrets = Bridge._mask_known_secrets
    _effective_tool_autonomy = Bridge._effective_tool_autonomy

    def __init__(self, level="autonomous"):
        self._credentials = CREDS
        self._level = level

    def _reload_credentials(self):
        pass


@pytest.fixture
def kr(monkeypatch):
    monkeypatch.setattr(cred_mod, "_keyring", lambda: _FakeKR())


def test_bridge_secret_file_env(kr):
    b = _B()
    assert b._secret_file_env("web-01", {"content": "plain"}) is None
    res = b._secret_file_env("web-01", {"content": "{{secret:grafana-cloud}}"})
    assert res.env == {"RUNSPEC_SECRET_GRAFANA_CLOUD": "glc_s3cr3t-token"}


def test_bridge_masks_every_saved_secret(kr):
    b = _B()
    out = b._mask_known_secrets('token = "glc_s3cr3t-token"\njira = jira-pat-value')
    assert out == 'token = "{{secret:grafana-cloud}}"\njira = {{secret:jira}}'


@pytest.mark.parametrize(
    "level,expected",
    [
        ("autonomous", "confirm"),
        ("supervised", "confirm"),
        ("confirm", "confirm"),
        ("manual", "manual"),
    ],
)
def test_placeholder_floors_autonomy_at_confirm(level, expected, monkeypatch):
    from runspec_console.bridge_autonomy import AutonomyMixin

    monkeypatch.setattr(
        AutonomyMixin, "_tool_autonomy_level", lambda self, n, ti: level
    )
    b = _B(level)
    assert (
        b._effective_tool_autonomy(
            "run_runnable", {"args": {"content": "{{secret:grafana-cloud}}"}}
        )
        == expected
    )
    assert (
        b._effective_tool_autonomy("run_runnable", {"args": {"content": "plain"}})
        == level
    )


def test_write_to_files_round_trips(tmp_path):
    path = tmp_path / "runspec_credentials.toml"
    save_credentials(path, [CREDS[0], CREDS[1]])
    text = path.read_text()
    assert text.count("write_to_files = true") == 1
    loaded = load_credentials(path)
    assert loaded[0]["write_to_files"] is True and "write_to_files" not in loaded[1]
