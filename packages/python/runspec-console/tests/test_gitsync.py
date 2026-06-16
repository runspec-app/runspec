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
