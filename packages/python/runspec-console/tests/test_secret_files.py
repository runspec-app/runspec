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


# ── telling the agent which placeholders it may use ─────────────────────────

FS_RECORD = {
    "name": "filesystem",
    "description": "Read and manipulate the filesystem",
    "commands": {
        "write-file": {
            "description": "Text may hold {{secret:<credential id>}} placeholders.",
            "args": {},
        },
        "read-text-file": {"description": "Read a file", "args": {}},
    },
}


def test_accepts_placeholders():
    assert sf.accepts_placeholders(FS_RECORD)
    assert not sf.accepts_placeholders({"name": "which", "description": "find"})


def test_placeholder_rows_list_allowed_credentials_for_the_hosts():
    rows = sf.placeholder_rows(CREDS, ["web-01", "db-01"])
    assert rows == [
        {
            "placeholder": "{{secret:grafana-cloud}}",
            "label": "Grafana Cloud",
            "username_placeholder": "{{secret:grafana-cloud.username}}",
            "hosts": ["web-01"],
        }
    ]
    assert sf.placeholder_rows(CREDS, ["web-01"])[0].get("hosts") is None
    assert sf.placeholder_rows(CREDS, ["db-01"]) == []


def test_bridge_placeholder_info():
    from runspec_console import bridge_secrets

    b = _B()
    info = bridge_secrets.placeholder_info(b, FS_RECORD, ["web-01"])
    assert info["secret_placeholders"][0]["placeholder"] == "{{secret:grafana-cloud}}"
    assert "Never ask" in info["secret_placeholders_hint"]
    # not scoped to db-01: say so, so the agent asks the operator to allow one
    none = bridge_secrets.placeholder_info(b, FS_RECORD, ["db-01"])
    assert "secret_placeholders" not in none
    assert "Agent may write this into files" in none["secret_placeholders_hint"]
    assert bridge_secrets.placeholder_info(b, {"name": "which"}, ["web-01"]) == {}
    note = bridge_secrets.placeholder_description(b, FS_RECORD, ["web-01"])
    assert "{{secret:grafana-cloud}} (Grafana Cloud" in note


def test_placeholder_problem_bounces_before_the_card():
    from runspec_console import bridge_secrets

    b = _B()
    assert bridge_secrets.placeholder_problem(b, "web-01", {"content": "x"}) is None
    assert (
        bridge_secrets.placeholder_problem(
            b, "web-01", {"content": "{{secret:grafana-cloud}}"}
        )
        is None
    )
    # label spelling works too
    assert (
        bridge_secrets.placeholder_problem(
            b, "web-01", {"content": "{{secret:Grafana-Cloud}}"}
        )
        is None
    )
    msg = bridge_secrets.placeholder_problem(
        b, "web-01", {"content": "{{secret:a6de043ed45b}}"}
    )
    assert msg.startswith("[exit 2]") and "{{secret:grafana-cloud}}" in msg
    assert "isn't scoped" in bridge_secrets.placeholder_problem(
        b, "db-01", {"content": "{{secret:grafana-cloud}}"}
    )
