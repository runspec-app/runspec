"""
test_repos.py — the git-repo agent tools (repos.py + bridge wiring).

Pure helpers (path-safety, registry, diff) run anywhere. The clone/sync test uses
a real local dulwich repo (no network), and the bridge dispatch/gating tests bind
the real Bridge methods onto a light stub with repos.* monkeypatched.
"""

from __future__ import annotations

import json

import pytest

from runspec_console import config, gitsync, repos
from runspec_console.bridge import Bridge
from runspec_console.bridge_const import _REPO_READ_TOOLS, _REPO_WRITE_TOOLS


# ── pure helpers ──────────────────────────────────────────────────────────────


def test_safe_path_allows_inside(tmp_path):
    assert repos._safe_path(tmp_path, "a/b.txt") == (tmp_path / "a/b.txt").resolve()
    assert repos._safe_path(tmp_path, "") == tmp_path.resolve()
    assert (
        repos._safe_path(tmp_path, "/leading/slash")
        == (tmp_path / "leading/slash").resolve()
    )


@pytest.mark.parametrize("bad", ["../escape", "a/../../escape", "../../etc/passwd"])
def test_safe_path_rejects_escape(tmp_path, bad):
    with pytest.raises(ValueError):
        repos._safe_path(tmp_path, bad)


def test_normalise_repos_defaults_and_dedup():
    out = repos.normalise_repos(
        [
            {"name": "App", "url": "https://x/app.git"},  # ref defaults to main
            {"name": "app", "url": "https://x/dup.git"},  # dup name (ci) dropped
            {"name": "", "url": "https://x/none.git"},  # no name dropped
            {"name": "noUrl"},  # no url dropped
            {"name": "B", "url": "ssh://x/b.git", "ref": "v2", "credential_id": "c1"},
        ]
    )
    assert [r["name"] for r in out] == ["App", "B"]
    assert out[0]["ref"] == "main"
    assert out[1]["ref"] == "v2" and out[1]["credential_id"] == "c1"


def test_is_binary():
    assert repos._is_binary(b"text\nmore") is False
    assert repos._is_binary(b"a\x00b") is True


def test_unified_diff_text_and_binary():
    d = repos._unified_diff("f.txt", b"a\nb\n", b"a\nc\n")
    assert any(line.startswith("-b") for line in d)
    assert any(line.startswith("+c") for line in d)
    assert repos._unified_diff("x.bin", b"\x00", b"\x01") == [
        "Binary file x.bin differs"
    ]


def test_registry_round_trip(tmp_path):
    p = tmp_path / "runspec_repos.toml"
    repos.save_repos(
        p,
        [
            {
                "name": "App",
                "url": "https://x/app.git",
                "ref": "main",
                "credential_id": "c1",
            }
        ],
    )
    loaded = repos.normalise_repos(repos.load_repos(p))
    assert loaded[0]["name"] == "App" and loaded[0]["credential_id"] == "c1"


# ── real clone / read / edit / diff against a local repo (no network) ──────────


def _make_origin(tmp_path):
    """A tiny local git repo with one commit and tag 'v1' (the clone source).

    Built with dulwich plumbing (not porcelain.commit) so it never touches the
    machine's git config / commit-signing — deterministic on any box."""
    from dulwich.objects import Blob, Commit, Tree
    from dulwich.repo import Repo

    src = tmp_path / "origin"
    src.mkdir()
    repo = Repo.init(str(src))
    blob_a = Blob.from_string(b"hello\nworld\n")
    blob_b = Blob.from_string(b"def price():\n    return 42\n")
    sub = Tree()
    sub.add(b"b.py", 0o100644, blob_b.id)
    root = Tree()
    root.add(b"a.txt", 0o100644, blob_a.id)
    root.add(b"sub", 0o040000, sub.id)
    commit = Commit()
    commit.tree = root.id
    commit.author = commit.committer = b"T <t@t>"
    commit.author_time = commit.commit_time = 1_700_000_000
    commit.author_timezone = commit.commit_timezone = 0
    commit.encoding = b"UTF-8"
    commit.message = b"init"
    for obj in (blob_a, blob_b, sub, root, commit):
        repo.object_store.add_object(obj)
    repo.refs[b"refs/heads/master"] = commit.id
    repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/master")
    repo.refs[b"refs/tags/v1"] = commit.id
    repo.close()
    return src


def test_sync_read_grep_edit_diff_roundtrip(tmp_path):
    src = _make_origin(tmp_path)
    dest = tmp_path / "checkout"
    auth = gitsync.GitAuth(scheme="local")

    res = repos.sync_repo(str(src), "v1", auth, dest)
    assert res["action"] == "cloned"
    assert (dest / "a.txt").exists() and (dest / "sub" / "b.py").exists()

    # list + read
    names = {e["name"] for e in repos.list_dir(dest, "")}
    assert {"a.txt", "sub"} <= names
    read = repos.read_file(dest, "a.txt")
    assert read["content"] == "hello\nworld" and read["total_lines"] == 2
    ranged = repos.read_file(dest, "a.txt", 2, 2)
    assert ranged["content"] == "world"

    # grep
    hits = repos.grep_repo(dest, r"def price")
    assert hits and hits[0]["path"] == "sub/b.py" and hits[0]["line"] == 1

    # edit (local) → diff shows it
    repos.write_file(dest, "a.txt", "hello\nthere\n")
    diff = repos.diff_repo(dest)
    paths = {f["path"]: f for f in diff["files"]}
    assert paths["a.txt"]["status"] == "modified"
    assert "+there" in paths["a.txt"]["diff"]

    # new file → added
    repos.write_file(dest, "new.md", "# hi\n")
    diff2 = {f["path"]: f["status"] for f in repos.diff_repo(dest)["files"]}
    assert diff2.get("new.md") == "added"

    # re-sync discards local edits (remote is source of truth)
    res2 = repos.sync_repo(str(src), "v1", auth, dest)
    assert res2["action"] == "updated"
    assert repos.read_file(dest, "a.txt")["content"] == "hello\nworld"


def test_sync_unknown_ref_raises(tmp_path):
    src = _make_origin(tmp_path)
    with pytest.raises(gitsync.GitSyncError):
        repos.sync_repo(
            str(src), "nope", gitsync.GitAuth(scheme="local"), tmp_path / "co"
        )


def test_read_rejects_binary(tmp_path):
    src = _make_origin(tmp_path)
    dest = tmp_path / "co"
    repos.sync_repo(str(src), "v1", gitsync.GitAuth(scheme="local"), dest)
    (dest / "blob.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(ValueError):
        repos.read_file(dest, "blob.bin")


# ── bridge dispatch + gating ──────────────────────────────────────────────────


class _StubBridge:
    _repo_tools = Bridge._repo_tools
    _run_repo_tool = Bridge._run_repo_tool
    _effective_tool_autonomy = Bridge._effective_tool_autonomy

    def _sync_auth(self, url, cred_id):  # what _run_repo_tool calls for repo_sync
        return gitsync.GitAuth(scheme="local")


def test_reads_autonomous_writes_confirm():
    b = _StubBridge()
    for name in _REPO_READ_TOOLS:
        assert b._effective_tool_autonomy(name, {}) == "autonomous"
    for name in _REPO_WRITE_TOOLS:
        assert b._effective_tool_autonomy(name, {}) == "confirm"


def test_no_repo_tools_when_registry_empty(monkeypatch):
    monkeypatch.setattr(repos, "available", lambda: True)
    monkeypatch.setattr(repos, "read_repos", lambda: [])
    assert _StubBridge()._repo_tools() == []


def test_repo_tools_offered_when_registered(monkeypatch):
    monkeypatch.setattr(repos, "available", lambda: True)
    monkeypatch.setattr(
        repos, "read_repos", lambda: [{"name": "app", "url": "u", "ref": "main"}]
    )
    names = [t["name"] for t in _StubBridge()._repo_tools()]
    assert names == [
        "repo_status",
        "repo_sync",
        "repo_list",
        "repo_grep",
        "repo_read",
        "repo_diff",
        "repo_write",
    ]


def test_repo_status_lists(monkeypatch):
    monkeypatch.setattr(
        repos,
        "read_repos",
        lambda: [{"name": "app", "url": "u", "ref": "main", "description": "d"}],
    )
    monkeypatch.setattr(repos, "is_checked_out", lambda n: False)
    out = _StubBridge()._run_repo_tool("repo_status", {})
    data = json.loads(out)
    assert data["count"] == 1 and data["repos"][0]["checked_out"] is False


def test_repo_unknown_name_errors(monkeypatch):
    monkeypatch.setattr(repos, "find_repo", lambda n: None)
    out = _StubBridge()._run_repo_tool("repo_read", {"name": "ghost", "path": "x"})
    assert out.startswith("Error:") and "registered" in out


def test_repo_read_requires_sync_first(monkeypatch):
    monkeypatch.setattr(
        repos, "find_repo", lambda n: {"name": "app", "url": "u", "ref": "main"}
    )
    monkeypatch.setattr(repos, "is_checked_out", lambda n: False)
    out = _StubBridge()._run_repo_tool("repo_read", {"name": "app", "path": "x"})
    assert out.startswith("Error:") and "repo_sync first" in out


def test_repo_sync_dispatch(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        repos,
        "find_repo",
        lambda n: {"name": "app", "url": "u", "ref": "main", "credential_id": ""},
    )
    monkeypatch.setattr(repos, "checkout_dir", lambda n: "/co/app")

    def fake_sync(url, ref, auth, dest):
        seen.update(url=url, ref=ref, dest=dest)
        return {
            "action": "cloned",
            "ref": ref,
            "commit": "abcdef0123",
            "path": str(dest),
        }

    monkeypatch.setattr(repos, "sync_repo", fake_sync)
    out = _StubBridge()._run_repo_tool("repo_sync", {"name": "app"})
    assert seen["url"] == "u" and seen["ref"] == "main"
    assert out.startswith("✓ cloned app @ main")


def test_repo_write_reports(monkeypatch):
    monkeypatch.setattr(
        repos, "find_repo", lambda n: {"name": "app", "url": "u", "ref": "main"}
    )
    monkeypatch.setattr(repos, "is_checked_out", lambda n: True)
    monkeypatch.setattr(repos, "checkout_dir", lambda n: "/co/app")
    monkeypatch.setattr(
        repos,
        "write_file",
        lambda dest, path, content: {
            "path": path,
            "bytes": len(content),
            "created": True,
        },
    )
    out = _StubBridge()._run_repo_tool(
        "repo_write", {"name": "app", "path": "a.txt", "content": "hi"}
    )
    assert out.startswith("✓ Created app/a.txt") and "not committed" in out


def test_repos_registered_for_config_sync():
    assert "runspec_repos.toml" in gitsync.KNOWN_FILES
    from runspec_console import configmerge

    assert configmerge.COLLECTION_FILES["runspec_repos.toml"] == ("repo", "name")


def test_repos_path_helpers():
    assert config.repos_path().name == "runspec_repos.toml"
    assert config.repos_root().name == "repos"
