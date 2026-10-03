"""
test_gitsync.py — the git layer behind Config Sync.

Builds a throwaway local repo with two tags (the config.toml differs between
them) and checks that gitsync lists the tags, reads the right file bytes per tag,
silently skips a file that doesn't exist at the ref, and raises GitSyncError for
an unknown ref. Uses a local on-disk repo (no network); skipped when dulwich or
git isn't available.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from runspec_console import gitsync

pytestmark = [
    pytest.mark.skipif(
        not gitsync.available(), reason="dulwich (git-sync extra) not installed"
    ),
    pytest.mark.skipif(
        shutil.which("git") is None, reason="git not available to build the fixture"
    ),
]


@pytest.fixture
def repo(tmp_path):
    """A local repo with tags v1.0.0 (config a=1) and v2.0.0 (config a=2)."""
    path = tmp_path / "cfgrepo"
    path.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    def git(*args):
        subprocess.run(
            ["git", "-C", str(path), *args], check=True, capture_output=True, env=env
        )

    subprocess.run(["git", "init", "-q", str(path)], check=True)
    git("config", "commit.gpgsign", "false")
    (path / "config.toml").write_text("a = 1\n")
    (path / "runspec_hosts.toml").write_text('[[host]]\nname = "h1"\n')
    git("add", "-A")
    git("commit", "-qm", "v1")
    git("tag", "v1.0.0")
    (path / "config.toml").write_text("a = 2\n")
    git("add", "-A")
    git("commit", "-qm", "v2")
    git("tag", "v2.0.0")
    return str(path)


@pytest.fixture
def layered_repo(tmp_path):
    """A repo whose config.toml differs by subdir: _base (a=1) and teams/x (a=2)."""
    path = tmp_path / "layered"
    path.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    def git(*args):
        subprocess.run(
            ["git", "-C", str(path), *args], check=True, capture_output=True, env=env
        )

    subprocess.run(["git", "init", "-q", str(path)], check=True)
    git("config", "commit.gpgsign", "false")
    (path / "_base").mkdir()
    (path / "teams" / "x").mkdir(parents=True)
    (path / "_base" / "config.toml").write_text("a = 1\n")
    (path / "teams" / "x" / "config.toml").write_text("a = 2\n")
    git("add", "-A")
    git("commit", "-qm", "v1")
    git("tag", "v1.0.0")
    return str(path)


def test_fetch_layers_single_fetch_reads_each_subpath(layered_repo, monkeypatch):
    calls = {"n": 0}
    real = gitsync._fetch_tree

    def spy(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(gitsync, "_fetch_tree", spy)
    r = gitsync.fetch_layers(
        layered_repo,
        "v1.0.0",
        ["config.toml"],
        gitsync.GitAuth(scheme="local"),
        ["_base", "teams/x"],
    )
    assert calls["n"] == 1  # one network fetch covers every layer
    assert r.layers["_base"]["config.toml"] == b"a = 1\n"
    assert r.layers["teams/x"]["config.toml"] == b"a = 2\n"


def test_list_tags_newest_first(repo):
    assert gitsync.list_tags(repo, gitsync.GitAuth(scheme="local")) == [
        "v2.0.0",
        "v1.0.0",
    ]


def test_fetch_reads_files_at_the_pinned_tag(repo):
    auth = gitsync.GitAuth(scheme="local")
    r1 = gitsync.fetch_files(
        repo, "v1.0.0", ["config.toml", "runspec_hosts.toml"], auth
    )
    assert r1.files["config.toml"] == b"a = 1\n"
    assert r1.files["runspec_hosts.toml"] == b'[[host]]\nname = "h1"\n'
    assert len(r1.commit) == 40

    r2 = gitsync.fetch_files(repo, "v2.0.0", ["config.toml"], auth)
    assert r2.files["config.toml"] == b"a = 2\n"
    assert r2.commit != r1.commit  # different tags → different commits


def test_missing_file_is_skipped_not_fatal(repo):
    r = gitsync.fetch_files(
        repo,
        "v1.0.0",
        ["config.toml", "does_not_exist.toml"],
        gitsync.GitAuth(scheme="local"),
    )
    assert "config.toml" in r.files
    assert "does_not_exist.toml" not in r.files


def test_unknown_ref_raises(repo):
    with pytest.raises(gitsync.GitSyncError):
        gitsync.fetch_files(
            repo, "v9.9.9", ["config.toml"], gitsync.GitAuth(scheme="local")
        )


def test_ssh_url_detection():
    assert gitsync._is_ssh_url("git@github.com:acme/cfg.git")
    assert gitsync._is_ssh_url("ssh://git@host/acme/cfg.git")
    assert not gitsync._is_ssh_url("https://github.com/acme/cfg.git")
    assert not gitsync._is_ssh_url("/local/path/to/repo")


def test_ssh_transport_kwargs_expand_key_and_user():
    auth = gitsync.GitAuth(
        scheme="ssh", username="git", key_path="~/.ssh/runspec_ed25519"
    )
    kwargs = gitsync._transport_kwargs("git@github.com:acme/cfg.git", auth)
    assert kwargs["username"] == "git"
    # ~ is expanded so paramiko gets an absolute identity file path.
    assert kwargs["key_filename"] == os.path.expanduser("~/.ssh/runspec_ed25519")
    assert "~" not in kwargs["key_filename"]


def test_ssh_vendor_factory_binds_passphrase():
    # _install_paramiko_vendor binds the passphrase into the vendor factory so a
    # later run_command can decrypt the key. Verify the factory is installed and
    # callable with a passphrase captured (no network/connection needed).
    pytest.importorskip("paramiko")
    import dulwich.client as client

    gitsync._install_paramiko_vendor("secret-pass")
    assert client.get_ssh_vendor is not gitsync._ParamikoSSHVendor
    vendor = client.get_ssh_vendor()  # builds the paramiko-backed vendor
    assert hasattr(vendor, "run_command")


def test_https_transport_kwargs_userpass():
    """A userpass credential sends its real username + secret as basic auth."""
    auth = gitsync.GitAuth(scheme="https", username="alice", secret="s3cret")
    kwargs = gitsync._transport_kwargs("https://gitlab.com/acme/app.git", auth)
    assert kwargs["username"] == "alice"
    assert kwargs["password"] == "s3cret"


def test_https_transport_kwargs_token_only_defaults_username():
    """A token-only credential (no username — the natural GitLab/GitHub PAT pick)
    must still send an Authorization header. dulwich only attaches basic auth when
    a username is present, so we default it so the token rides as the password."""
    auth = gitsync.GitAuth(scheme="https", username="", secret="glpat-xxxx")
    kwargs = gitsync._transport_kwargs("https://gitlab.com/acme/app.git", auth)
    assert kwargs["username"] == "oauth2"  # placeholder so auth is sent (was 401)
    assert kwargs["password"] == "glpat-xxxx"


def test_https_transport_kwargs_no_secret_no_username():
    """A public/anonymous repo (no secret) sends no credentials."""
    auth = gitsync.GitAuth(scheme="https")
    kwargs = gitsync._transport_kwargs("https://gitlab.com/acme/app.git", auth)
    assert "username" not in kwargs and "password" not in kwargs


def test_pool_manager_none_when_no_ca_or_proxy(monkeypatch):
    for var in (
        "SSL_CERT_FILE",
        "GIT_SSL_CAINFO",
        "REQUESTS_CA_BUNDLE",
        "https_proxy",
        "HTTPS_PROXY",
        "http_proxy",
        "HTTP_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    # No CA / proxy → None (dulwich uses its certifi default).
    assert gitsync._https_pool_manager(gitsync.GitAuth(scheme="https")) is None


def test_pool_manager_uses_ca_bundle(tmp_path, monkeypatch):
    for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    ca = tmp_path / "corp-ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    pm = gitsync._https_pool_manager(gitsync.GitAuth(scheme="https", ca_bundle=str(ca)))
    assert pm is not None
    assert pm.connection_pool_kw.get("ca_certs") == str(ca)


def test_pool_manager_builds_proxy_manager(monkeypatch):
    monkeypatch.delenv("https_proxy", raising=False)
    import urllib3

    pm = gitsync._https_pool_manager(
        gitsync.GitAuth(scheme="https", proxy="proxy.corp:8080")
    )
    assert isinstance(pm, urllib3.ProxyManager)  # scheme is added automatically


def test_pool_manager_verify_off(monkeypatch):
    for var in (
        "SSL_CERT_FILE",
        "GIT_SSL_CAINFO",
        "REQUESTS_CA_BUNDLE",
        "https_proxy",
        "HTTPS_PROXY",
        "http_proxy",
        "HTTP_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    # tls_verify=False must build a manager (not None) with verification disabled.
    pm = gitsync._https_pool_manager(gitsync.GitAuth(scheme="https", tls_verify=False))
    assert pm is not None
    assert pm.connection_pool_kw.get("cert_reqs") == "CERT_NONE"


# ── tag versions / newer-config detection ───────────────────────────────────────


def test_sort_tags_is_version_aware():
    assert gitsync.sort_tags(["v1.9.0", "v1.10.0", "v1.10.0-rc1", "notes", "v2.0"]) == [
        "v2.0",
        "v1.10.0",
        "v1.10.0-rc1",
        "v1.9.0",
        "notes",
    ]


def test_newer_tag_same_series_and_releases_only():
    tags = ["v1.0.0", "v1.2.0", "v2.0.0-rc1", "emea-v9.0.0"]
    assert gitsync.newer_tag("v1.0.0", tags) == "v1.2.0"
    assert gitsync.newer_tag("v1.2.0", tags) is None
    assert gitsync.newer_tag("emea-v1.0", ["emea-v1.0.1", "v5"]) == "emea-v1.0.1"
    assert gitsync.newer_tag("main", tags) is None


def test_check_update_tag_branch_and_unversioned(monkeypatch):
    refs = {
        b"refs/tags/v1.0.0": b"1" * 40,
        b"refs/tags/v1.1.0": b"2" * 40,
        b"refs/tags/golden": b"3" * 40,
        b"refs/heads/main": b"9" * 40,
    }
    monkeypatch.setattr(gitsync, "_ls_remote", lambda url, auth: refs)
    auth = gitsync.GitAuth()
    tag = gitsync.check_update("u", "v1.0.0", "", auth)
    assert (tag.available, tag.kind, tag.latest) == (True, "tag", "v1.1.0")
    assert gitsync.check_update("u", "v1.1.0", "", auth).available is False
    moved = gitsync.check_update("u", "main", "8" * 40, auth)
    assert (moved.available, moved.kind, moved.latest) == (True, "branch", "9" * 40)
    assert gitsync.check_update("u", "main", "9" * 40, auth).available is False
    assert gitsync.check_update("u", "main", "", auth).available is False
    assert gitsync.check_update("u", "golden", "", auth).kind == "none"
