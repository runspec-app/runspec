"""Secret placeholders: fill from RUNSPEC_SECRET_*, mask in output, file modes."""

import json
import os
import stat
from pathlib import Path

import pytest

from runspec_fs_core import SecretNotAvailableError, edit_file, write_file
from runspec_fs_core.secrets import (
    env_name,
    fill_secrets,
    fill_verb_secrets,
    find_placeholders,
    mask_secrets,
    mask_text,
)

ENV = {
    "RUNSPEC_SECRET_GRAFANA_CLOUD": "glc_s3cr3t-token",
    "RUNSPEC_SECRET_GRAFANA_CLOUD__USERNAME": "123456",
    "AWS_SECRET_ACCESS_KEY": "do-not-copy-me",
}

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")


def test_env_name() -> None:
    assert env_name("grafana-cloud") == "RUNSPEC_SECRET_GRAFANA_CLOUD"
    assert env_name("grafana-cloud", "username") == "RUNSPEC_SECRET_GRAFANA_CLOUD__USERNAME"
    assert env_name("svc.prod--eu") == "RUNSPEC_SECRET_SVC_PROD_EU"
    with pytest.raises(ValueError):
        env_name("--")


def test_find_placeholders() -> None:
    text = "u={{secret:grafana-cloud.username}} p={{secret:grafana-cloud}} again {{secret:grafana-cloud}} {{ secret:x }}"
    assert find_placeholders(text) == [("grafana-cloud", "username"), ("grafana-cloud", "secret")]


def test_fill_and_mask() -> None:
    text = 'username = "{{secret:grafana-cloud.username}}"\npassword = "{{secret:grafana-cloud}}"\n'
    filled, masks = fill_secrets(text, ENV)
    assert filled == 'username = "123456"\npassword = "glc_s3cr3t-token"\n'
    # only the secret is masked, never the username
    assert masks == {"glc_s3cr3t-token": "{{secret:grafana-cloud}}"}
    assert mask_secrets({"diff": ["+ glc_s3cr3t-token"], "n": 3}, masks) == {"diff": ["+ {{secret:grafana-cloud}}"], "n": 3}


def test_only_runspec_secret_vars_are_read() -> None:
    with pytest.raises(SecretNotAvailableError, match="RUNSPEC_SECRET_AWS_SECRET_ACCESS_KEY"):
        fill_secrets("{{secret:AWS_SECRET_ACCESS_KEY}}", ENV)


def test_missing_and_too_short_values_are_errors() -> None:
    with pytest.raises(SecretNotAvailableError, match="nope"):
        fill_secrets("{{secret:nope}}", ENV)
    with pytest.raises(SecretNotAvailableError):
        fill_secrets("{{secret:tiny}}", {"RUNSPEC_SECRET_TINY": "abc"})


def test_mask_text_catches_json_escaped_value() -> None:
    masks = {'qz"xy\\vk': "{{secret:x}}"}
    printed = json.dumps({"diff": '+ password = qz"xy\\vk'})
    assert "vk" not in mask_text(printed, masks)
    assert "{{secret:x}}" in mask_text(printed, masks)


def test_fill_verb_secrets_write_and_edit() -> None:
    kw, masks, ids = fill_verb_secrets("write-file", {"path": "p", "content": "t={{secret:grafana-cloud}}"}, ENV)
    assert kw["content"] == "t=glc_s3cr3t-token" and kw["private"] is True
    assert ids == ["grafana-cloud"] and masks

    kw, masks, ids = fill_verb_secrets(
        "edit-file",
        {"path": "p", "edits": [["{{secret:grafana-cloud}}", "{{secret:grafana-cloud}}"]], "dry_run": False},
        ENV,
    )
    # old text is left alone (an edit can't probe whether a file holds a secret)
    assert kw["edits"] == [["{{secret:grafana-cloud}}", "glc_s3cr3t-token"]]

    kw, masks, ids = fill_verb_secrets("write-file", {"path": "p", "content": "plain"}, ENV)
    assert kw == {"path": "p", "content": "plain"} and not masks and not ids

    kw, masks, _ = fill_verb_secrets("read-text-file", {"path": "{{secret:grafana-cloud}}"}, ENV)
    assert kw == {"path": "{{secret:grafana-cloud}}"} and not masks


@posix_only
def test_new_private_file_is_owner_only(tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        out = write_file(tmp_path / "token", "s3cr3t", private=True)
        assert stat.S_IMODE((tmp_path / "token").stat().st_mode) == 0o600
        assert out["mode"] == "0600" and "warning" not in out

        write_file(tmp_path / "plain", "x")
        assert stat.S_IMODE((tmp_path / "plain").stat().st_mode) == 0o644
    finally:
        os.umask(old)


@posix_only
def test_mode_arg_and_existing_file_keeps_mode(tmp_path: Path) -> None:
    old = os.umask(0o077)
    try:
        out = write_file(tmp_path / "cfg", "x", mode=0o640, private=True)
        assert out["mode"] == "0640"  # set even though the umask would narrow it
    finally:
        os.umask(old)

    f = tmp_path / "existing"
    f.write_text("old")
    f.chmod(0o644)
    out = write_file(f, "new {{secret}}", private=True)
    assert stat.S_IMODE(f.stat().st_mode) == 0o644
    assert "every user" in out["warning"]

    out = write_file(f, "newer", mode=0o600, private=True)
    assert out["mode"] == "0600" and "warning" not in out


@posix_only
def test_edit_file_mode(tmp_path: Path) -> None:
    f = tmp_path / "cfg"
    f.write_text("token = X\n")
    f.chmod(0o644)
    out = edit_file(f, [("X", "s3cr3t")], mode=0o600, private=True)
    assert out["mode"] == "0600"
    assert f.read_text() == "token = s3cr3t\n"


def test_bad_mode_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_file(tmp_path / "x", "y", mode=0o17777)
