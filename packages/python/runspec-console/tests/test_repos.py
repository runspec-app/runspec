"""
test_repos.py — the git-repo agent tools (repos.py + bridge wiring).

Pure helpers (path-safety, registry, diff) run anywhere. The clone/sync test uses
a real local dulwich repo (no network), and the bridge dispatch/gating tests bind
the real Bridge methods onto a light stub with repos.* monkeypatched.
"""

from __future__ import annotations

import json
import threading

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


def test_grep_options_case_literal_and_glob(tmp_path):
    """The repo_grep search options the console agent relies on: a match is found
    anywhere on a line (leading tabs/spaces are irrelevant), ignore_case folds
    case, fixed matches regex metacharacters literally, and glob narrows the
    scanned file set."""
    src = _make_origin(tmp_path)
    dest = tmp_path / "co"
    repos.sync_repo(str(src), "v1", gitsync.GitAuth(scheme="local"), dest)
    # craft files in the local checkout to exercise each option
    repos.write_file(dest, "svc.ts", "  \tresumeTask(id)\nfunction ResumeTask() {}\n")
    repos.write_file(dest, "note.md", "price[0] literal here\n")

    # a leading tab/spaces before the token never prevents a match
    hits = repos.grep_repo(dest, r"resumeTask", glob="*.ts")
    assert [h["line"] for h in hits] == [1]

    # ignore_case folds the CamelCase variant on line 2 in as well
    folded = repos.grep_repo(dest, r"resumetask", ignore_case=True, glob="*.ts")
    assert {h["line"] for h in folded} == {1, 2}

    # fixed=True matches the metacharacters literally; the same string as a regex
    # (a char class) does not match the literal text
    assert repos.grep_repo(dest, "price[0]", fixed=True, glob="*.md")[0]["path"] == (
        "note.md"
    )
    assert repos.grep_repo(dest, "price[0]", glob="*.md") == []

    # glob restricts the scan to the matching files (comma-separated list allowed)
    only_md = repos.grep_repo(dest, r".", glob="*.md,*.rst")
    assert {h["path"] for h in only_md} == {"note.md"}

    # max_results caps the rows returned (the truncation the handler flags)
    assert len(repos.grep_repo(dest, r".", max_results=1)) == 1


def test_is_checked_out_requires_working_tree(tmp_path, monkeypatch):
    """A bare .git with no working tree reads as not-checked-out (honest status)."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    d = repos.checkout_dir("payments")
    (d / ".git").mkdir(parents=True)
    assert repos.is_checked_out("payments") is False  # only .git → not ready
    (d / "README.md").write_text("hi", encoding="utf-8")
    assert repos.is_checked_out("payments") is True  # real working tree


def test_sync_empty_working_tree_raises(tmp_path, monkeypatch):
    """A non-empty commit that leaves only .git on disk is a silently-failed
    checkout — sync_repo surfaces it as an error instead of reporting success."""
    import dulwich.index

    src = _make_origin(tmp_path)
    # Simulate the silent-checkout failure: fetch + ref resolution succeed, but the
    # working tree never materialises (only .git ends up on disk).
    monkeypatch.setattr(dulwich.index, "build_index_from_tree", lambda *a, **k: None)
    with pytest.raises(gitsync.GitSyncError) as exc:
        repos.sync_repo(
            str(src), "v1", gitsync.GitAuth(scheme="local"), tmp_path / "co"
        )
    assert "no files were written" in str(exc.value)


def test_sync_unknown_ref_raises(tmp_path):
    src = _make_origin(tmp_path)
    dest = tmp_path / "co"
    with pytest.raises(gitsync.GitSyncError):
        repos.sync_repo(str(src), "nope", gitsync.GitAuth(scheme="local"), dest)
    # A failed *fresh* clone must not leave an orphan .git behind (it would read as
    # a misleading "synced" repo with no files). The whole dir is removed.
    assert not dest.exists()


def test_failed_clone_leaves_no_orphan(tmp_path):
    """A failed fetch on a fresh clone removes the half-made checkout — the root
    cause of the 'only .git, but a green synced chip' report."""
    dest = tmp_path / "co"
    with pytest.raises(gitsync.GitSyncError):
        repos.sync_repo(
            str(tmp_path / "does-not-exist"),
            "main",
            gitsync.GitAuth(scheme="local"),
            dest,
        )
    assert not dest.exists()


def test_read_rejects_binary(tmp_path):
    src = _make_origin(tmp_path)
    dest = tmp_path / "co"
    repos.sync_repo(str(src), "v1", gitsync.GitAuth(scheme="local"), dest)
    (dest / "blob.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(ValueError):
        repos.read_file(dest, "blob.bin")


# ── cooperative cancellation (the "stop" control for repo tools) ──────────────


def test_grep_cancels_when_event_set(tmp_path):
    """A set cancel_event aborts a scan between files with ToolCancelled — the
    seam the Console card Cancel / chat Stop pulls on a long repo_grep."""
    from runspec_console.bridge_const import ToolCancelled

    src = _make_origin(tmp_path)
    dest = tmp_path / "co"
    repos.sync_repo(str(src), "v1", gitsync.GitAuth(scheme="local"), dest)
    ev = threading.Event()
    ev.set()
    with pytest.raises(ToolCancelled):
        repos.grep_repo(dest, r".", cancel_event=ev)


def test_sync_cancel_before_checkout_raises_and_cleans_up(tmp_path):
    """A stop requested during a fresh clone surfaces as ToolCancelled (not a
    GitSyncError) and removes the half-made checkout — same cleanup as a failed
    fetch, so a cancelled sync never leaves a misleading 'synced' dir."""
    from runspec_console.bridge_const import ToolCancelled

    src = _make_origin(tmp_path)
    dest = tmp_path / "co"
    ev = threading.Event()
    ev.set()  # already cancelled before the checkout step runs
    with pytest.raises(ToolCancelled):
        repos.sync_repo(
            str(src), "v1", gitsync.GitAuth(scheme="local"), dest, cancel_event=ev
        )
    assert not dest.exists()


# ── bridge dispatch + gating ──────────────────────────────────────────────────


class _StubBridge:
    _repo_tools = Bridge._repo_tools
    _run_repo_tool = Bridge._run_repo_tool
    _effective_tool_autonomy = Bridge._effective_tool_autonomy
    # Settings → Repos bridge methods (FleetMixin) — used by the registry-UI tests.
    get_repos = Bridge.get_repos
    save_repos = Bridge.save_repos
    delete_repo = Bridge.delete_repo
    sync_repo = Bridge.sync_repo

    def _sync_auth(self, url, cred_id):  # what _run_repo_tool / sync_repo call
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

    def fake_sync(url, ref, auth, dest, cancel_event=None, on_progress=None):
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


def test_repo_grep_dispatch_passes_options_and_flags_truncation(monkeypatch):
    """The handler forwards the ignore_case/fixed/glob options to grep_repo and
    marks the payload 'truncated' once the row count reaches the effective cap,
    so the agent knows to narrow rather than assume it saw everything."""
    monkeypatch.setattr(
        repos, "find_repo", lambda n: {"name": "app", "url": "u", "ref": "main"}
    )
    monkeypatch.setattr(repos, "is_checked_out", lambda n: True)
    monkeypatch.setattr(repos, "checkout_dir", lambda n: "/co/app")
    seen = {}

    def fake_grep(dest, pattern, rel, mx, *, ignore_case, fixed, glob, **kw):
        seen.update(
            pattern=pattern, ignore_case=ignore_case, fixed=fixed, glob=glob, mx=mx
        )
        return [{"path": "f.py", "line": i, "text": "x"} for i in range(mx)]

    monkeypatch.setattr(repos, "grep_repo", fake_grep)
    out = _StubBridge()._run_repo_tool(
        "repo_grep",
        {
            "name": "app",
            "pattern": "price[0]",
            "ignore_case": True,
            "fixed": True,
            "glob": "*.py",
            "max_results": 3,
        },
    )
    data = json.loads(out)
    assert seen == {
        "pattern": "price[0]",
        "ignore_case": True,
        "fixed": True,
        "glob": "*.py",
        "mx": 3,
    }
    assert data["count"] == 3 and data["truncated"] is True and "Narrow" in data["hint"]


# ── Settings → Repos bridge methods ───────────────────────────────────────────


def test_get_repos_includes_status(monkeypatch):
    monkeypatch.setattr(
        repos,
        "read_repos",
        lambda: [{"name": "app", "url": "u", "ref": "main", "credential_id": ""}],
    )
    monkeypatch.setattr(repos, "is_checked_out", lambda n: True)
    monkeypatch.setattr(repos, "head_commit", lambda n: "abcdef0123")
    rows = _StubBridge().get_repos()
    assert rows[0]["name"] == "app"
    assert rows[0]["checked_out"] is True and rows[0]["commit"] == "abcdef0123"


def test_save_repos_writes_registry(monkeypatch, tmp_path):
    p = tmp_path / "runspec_repos.toml"
    monkeypatch.setattr(config, "repos_path", lambda: p)
    _StubBridge().save_repos([{"name": "App", "url": "https://x/app.git"}])
    assert repos.normalise_repos(repos.load_repos(p))[0]["name"] == "App"


def test_sync_repo_unknown(monkeypatch):
    monkeypatch.setattr(repos, "find_repo", lambda n: None)
    res = _StubBridge().sync_repo("ghost")
    assert res["ok"] is False and "registered" in res["error"]


def test_sync_repo_ok(monkeypatch):
    monkeypatch.setattr(
        repos, "find_repo", lambda n: {"name": "app", "url": "u", "ref": "main"}
    )
    monkeypatch.setattr(repos, "checkout_dir", lambda n: "/co/app")
    monkeypatch.setattr(
        repos,
        "sync_repo",
        lambda url, ref, auth, dest: {"action": "cloned", "commit": "abcdef0123456"},
    )
    res = _StubBridge().sync_repo("app")
    assert (
        res["ok"] is True
        and res["action"] == "cloned"
        and res["commit"] == "abcdef0123"
    )


def test_sync_repo_surfaces_error(monkeypatch):
    monkeypatch.setattr(
        repos, "find_repo", lambda n: {"name": "app", "url": "u", "ref": "main"}
    )
    monkeypatch.setattr(repos, "checkout_dir", lambda n: "/co/app")

    def boom(*a, **k):
        raise gitsync.GitSyncError("auth failed")

    monkeypatch.setattr(repos, "sync_repo", boom)
    res = _StubBridge().sync_repo("app")
    assert res["ok"] is False and "auth failed" in res["error"]


def test_repos_registered_for_config_sync():
    assert "runspec_repos.toml" in gitsync.KNOWN_FILES
    from runspec_console import configmerge

    assert configmerge.COLLECTION_FILES["runspec_repos.toml"] == ("repo", "name")


def test_repos_path_helpers():
    assert config.repos_path().name == "runspec_repos.toml"
    assert config.repos_root().name == "repos"
    assert config.repo_paths_path().name == "runspec_repo_paths.toml"


# ── machine-local custom checkout-path overrides ──────────────────────────────


def test_repo_paths_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    p = config.repo_paths_path()
    assert repos.read_repo_paths() == {}  # absent → empty, no file written
    repos.set_repo_path("payments", r"C:\dev\payments")
    assert p.exists()
    assert repos.read_repo_paths() == {"payments": r"C:\dev\payments"}
    # Round-trips through TOML (backslashes survive the basic-string escaping).
    assert repos.load_repo_paths(p) == {"payments": r"C:\dev\payments"}


def test_custom_path_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    repos.set_repo_path("Payments", "/home/me/dev/payments")
    assert repos.custom_path("payments") == "/home/me/dev/payments"
    assert repos.custom_path("PAYMENTS") == "/home/me/dev/payments"
    assert repos.custom_path("other") == ""


def test_set_repo_path_clear_removes_entry_and_file(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    p = config.repo_paths_path()
    repos.set_repo_path("a", "/x/a")
    repos.set_repo_path("a", "")  # clearing the only entry deletes the file
    assert not p.exists()
    assert repos.custom_path("a") == ""


def test_checkout_dir_honours_override(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    # Default location is under repos_root.
    assert repos.checkout_dir("app") == config.repos_root() / "app"
    repos.set_repo_path("app", str(tmp_path / "dev" / "app"))
    assert repos.checkout_dir("app") == (tmp_path / "dev" / "app")
    assert repos.default_checkout_dir("app") == config.repos_root() / "app"


def test_delete_checkout_removes_appdata_only(tmp_path, monkeypatch):
    """delete_checkout removes the AppData dir + drops the override, but never
    touches a custom dev-folder checkout."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    # AppData checkout exists and gets removed.
    appdata = repos.default_checkout_dir("app")
    appdata.mkdir(parents=True)
    (appdata / "f.txt").write_text("x", encoding="utf-8")
    assert repos.delete_checkout("app") is True
    assert not appdata.exists()

    # A repo with a custom dev folder: the dev folder survives, the override is
    # cleared, and no AppData dir is reported removed.
    dev = tmp_path / "mydev" / "infra"
    dev.mkdir(parents=True)
    (dev / "keep.txt").write_text("important", encoding="utf-8")
    repos.set_repo_path("infra", str(dev))
    assert repos.delete_checkout("infra") is False  # no AppData dir to remove
    assert dev.exists() and (dev / "keep.txt").exists()  # dev folder untouched
    assert repos.custom_path("infra") == ""  # override cleared


def test_get_repos_includes_custom_path(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(
        repos, "read_repos", lambda: [{"name": "infra", "url": "u", "ref": "main"}]
    )
    repos.set_repo_path("infra", str(tmp_path / "dev" / "infra"))
    monkeypatch.setattr(repos, "is_checked_out", lambda n: False)
    monkeypatch.setattr(repos, "head_commit", lambda n: "")
    row = _StubBridge().get_repos()[0]
    assert row["path"] == str(tmp_path / "dev" / "infra")
    assert row["checkout_dir"] == str(tmp_path / "dev" / "infra")


def test_save_repos_splits_path_to_local_file(monkeypatch, tmp_path):
    """A repo's `path` is written to the machine-local file, never the synced
    registry."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    reg = config.repos_path()
    _StubBridge().save_repos(
        [{"name": "infra", "url": "https://x/infra.git", "path": r"C:\dev\infra"}]
    )
    # Synced registry has no `path` field…
    saved = repos.normalise_repos(repos.load_repos(reg))[0]
    assert "path" not in saved
    assert reg.read_text(encoding="utf-8").find("path") == -1
    # …but the machine-local override file does.
    assert repos.custom_path("infra") == r"C:\dev\infra"


def test_save_repos_drops_stale_override(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    repos.set_repo_path("gone", "/old/path")
    _StubBridge().save_repos([{"name": "infra", "url": "https://x/infra.git"}])
    # The override for a repo no longer in the saved set is rebuilt away.
    assert repos.custom_path("gone") == ""


def test_delete_repo_removes_entry_and_appdata(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    reg = config.repos_path()
    repos.save_repos(
        reg,
        [
            {"name": "app", "url": "https://x/app.git"},
            {"name": "infra", "url": "https://x/infra.git"},
        ],
    )
    appdata = repos.default_checkout_dir("app")
    appdata.mkdir(parents=True)
    (appdata / "f.txt").write_text("x", encoding="utf-8")
    res = _StubBridge().delete_repo("app")
    assert res["ok"] is True and res["removed_checkout"] is True
    assert not appdata.exists()
    names = [r["name"] for r in repos.normalise_repos(repos.load_repos(reg))]
    assert names == ["infra"]


def test_delete_repo_keeps_custom_checkout(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    reg = config.repos_path()
    repos.save_repos(reg, [{"name": "infra", "url": "https://x/infra.git"}])
    dev = tmp_path / "mydev" / "infra"
    dev.mkdir(parents=True)
    (dev / "keep.txt").write_text("work in progress", encoding="utf-8")
    repos.set_repo_path("infra", str(dev))
    res = _StubBridge().delete_repo("infra")
    assert res["ok"] is True and res["removed_checkout"] is False
    assert dev.exists() and (dev / "keep.txt").exists()  # never deleted
    assert repos.normalise_repos(repos.load_repos(reg)) == []


def test_repo_paths_not_synced():
    """The machine-local override file must stay out of Config Sync."""
    assert "runspec_repo_paths.toml" not in gitsync.KNOWN_FILES
