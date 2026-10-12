"""The wiki repo template (okf.py, wiki_template/): the generated runspec/ pages,
the OKF lint shared with the template's CI, the multi-file git push, and the
bridge's Create wiki repo / Refresh runspec pages."""

from __future__ import annotations

import datetime as dt

import pytest

from runspec_console import okf

SEED = [
    {
        "kind": "glossary",
        "about": ["app:runspec", "topic:autonomy"],
        "text": "Autonomy says who may run a tool. See https://runspec.app/autonomy/.",
    },
    {
        "kind": "procedure",
        "about": ["app:runspec", "topic:autonomy"],
        "text": "Set autonomy on the runnable section.",
    },
    {
        "kind": "note",
        "about": ["app:runspec-console", "feature:forms"],
        "text": "Forms run a runnable from a form.",
    },
]


# ── generated pages ─────────────────────────────────────────────────────────


def test_committed_pages_match_the_seed():
    """wiki_template/runspec/ is regenerated with `python -m runspec_console.okf --write`."""
    assert okf.refresh_changes(okf.committed_pages()) == {}


def test_the_template_passes_its_own_lint():
    files = okf.template_files()
    md = {p: d.decode() for p, d in files.items() if p.endswith(".md")}
    issues = okf.lint_files(md)
    assert issues == []
    assert "tools/okf_lint.py" in files and ".gitlab-ci.yml" in files
    assert ".github/workflows/wiki.yml" in files and "README.md" in files
    assert not any("__pycache__" in p or p.endswith("__init__.py") for p in files)
    assert files["index.md"].startswith(b'---\nokf_version: "0.2"\n---')


def test_pages_one_per_entity_with_facts_on_their_main_entity():
    pages = okf.seed_pages(SEED, today="2026-10-01")
    page = pages["runspec/topics/autonomy.md"]
    assert 'type: "Topic"' in page and "## What it is" in page and "## How to" in page
    assert 'resource: "https://runspec.app/autonomy/"' in page
    assert "- [runspec](/runspec/apps/runspec.md)" in page
    # The app page has no facts of its own but links back.
    assert "[autonomy](/runspec/topics/autonomy.md)" in pages["runspec/apps/runspec.md"]
    assert 'type: "Feature"' in pages["runspec/features/forms.md"]
    assert "- [forms](/runspec/features/forms.md)" in pages["runspec/index.md"]


def test_unchanged_pages_keep_their_date():
    first = okf.seed_pages(SEED, today="2026-10-01")
    again = okf.seed_pages(SEED, existing=first, today="2026-10-09")
    assert again == first
    edited = [dict(SEED[0], text="Autonomy decides who may run it.")] + SEED[1:]
    later = okf.seed_pages(edited, existing=first, today="2026-10-09")
    assert 'at: "2026-10-09"' in later["runspec/topics/autonomy.md"]
    assert later["runspec/features/forms.md"] == first["runspec/features/forms.md"]


def test_refresh_changes_add_change_and_remove():
    current = okf.seed_pages(SEED, today="2026-10-01")
    current["runspec/topics/gone.md"] = "---\ntype: Topic\n---\n# gone\n"
    changes = okf.refresh_changes(current, SEED[:2], today="2026-10-09")
    assert changes["runspec/topics/gone.md"] is None
    assert changes["runspec/features/forms.md"] is None
    assert "runspec/topics/autonomy.md" not in changes  # unchanged
    assert b"forms" not in changes["runspec/index.md"]


# ── lint ────────────────────────────────────────────────────────────────────


def _lint(files, today=dt.date(2026, 10, 9)):
    lint = okf.lint_module()
    return [(i.level, i.path, i.message) for i in lint.lint_files(files, today)]


def test_lint_requires_type_frontmatter():
    issues = _lint({"a.md": "# no frontmatter\n", "b.md": "---\ntitle: b\n---\n# b\n"})
    assert ("error", "a.md", "no YAML frontmatter (--- ... ---)") in issues
    assert ("error", "b.md", "frontmatter has no type") in issues


def test_lint_reserved_files_and_root_version():
    issues = _lint({"index.md": "# home\n", "x/log.md": "---\ntype: Log\n---\n"})
    assert (
        "warning",
        "index.md",
        'add okf_version: "0.2" to the root index.md',
    ) in issues
    assert ("error", "x/log.md", "log.md must not have frontmatter") in issues


def test_lint_secrets_links_and_staleness():
    page = (
        "---\ntype: Topic\nstale_after: 2026-01-01\n---\n"
        "See [cron](/runnables/cron.md) and [here](./other.md).\n"
        "db password = hunter2abc\n"
        "Pass --password $DB_PASSWORD on the command line.\n"
    )
    issues = _lint({"topics/a.md": page, "topics/other.md": "---\ntype: Topic\n---\n"})
    levels = {(lvl, msg.split(" —")[0]) for lvl, _p, msg in issues}
    assert ("warning", "link to missing page /runnables/cron.md") in levels
    assert ("warning", "stale since 2026-01-01") in levels
    assert ("error", "looks like a password") in levels
    assert sum(1 for lvl, *_ in issues if lvl == "error") == 1  # the $VAR line is fine


def test_lint_bundle_on_disk(tmp_path):
    lint = okf.lint_module()
    (tmp_path / "index.md").write_text('---\nokf_version: "0.2"\n---\n# x\n')
    (tmp_path / "README.md").write_text("# no frontmatter, but README is skipped\n")
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "p.md").write_text("---\ntype: Topic\n---\n[home](/index.md)\n")
    assert lint.lint_bundle(str(tmp_path)) == []
    assert lint.main([str(tmp_path)]) == 0
    (tmp_path / "t" / "q.md").write_text("# bad\n")
    assert lint.main([str(tmp_path)]) == 1


def test_site_links_become_relative():
    import importlib.util

    path = okf.TEMPLATE_DIR / "tools" / "build_site.py"
    spec = importlib.util.spec_from_file_location("okf_build_site", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.relative_links(
        "[a](/runspec/topics/x.md#y) [b](https://e.com/z.md)", "runspec/features/f.md"
    )
    assert out == "[a](../topics/x.md#y) [b](https://e.com/z.md)"


# ── git + bridge ────────────────────────────────────────────────────────────

pytest.importorskip("dulwich")

import runspec_console.config as cfg  # noqa: E402
from runspec_console import gitsync  # noqa: E402
from runspec_console import knowledge as kn  # noqa: E402
from runspec_console.bridge_memory_sync import MemorySyncMixin  # noqa: E402

LOCAL = gitsync.GitAuth(scheme="local")


@pytest.fixture
def bare(tmp_path):
    from dulwich.repo import Repo

    path = tmp_path / "wiki.git"
    path.mkdir()
    Repo.init_bare(str(path))
    return str(path)


def test_push_files_writes_deletes_and_refuses_existing(bare):
    r = gitsync.push_files(
        bare, "main", {"a/x.md": b"x", "a/b/y.md": b"y"}, LOCAL, require_new=True
    )
    assert r.created
    got = gitsync.fetch_dir(bare, "main", LOCAL, "a", ".md", recursive=True)
    assert got.files == {"x.md": b"x", "b/y.md": b"y"}
    assert gitsync.fetch_dir(bare, "main", LOCAL, "a", ".md").files == {"x.md": b"x"}
    gitsync.push_files(bare, "main", {"a/b/y.md": None, "a/x.md": b"x2"}, LOCAL)
    assert gitsync.fetch_dir(bare, "main", LOCAL, "a", ".md", recursive=True).files == {
        "x.md": b"x2"
    }
    same = gitsync.push_files(bare, "main", {"a/x.md": b"x2", "gone.md": None}, LOCAL)
    assert not same.changed
    with pytest.raises(gitsync.GitSyncError, match="already exists"):
        gitsync.push_files(bare, "main", {"z.md": b"z"}, LOCAL, require_new=True)


class _Bridge(MemorySyncMixin):
    def __init__(self, root):
        self.root = root

    def _knowledge_store(self):
        return kn.KnowledgeStore(self.root)

    def _knowledge_changed(self):
        pass

    def _sync_auth(self, url, credential_id, tls=None):
        return LOCAL


@pytest.fixture
def bridge(monkeypatch, tmp_path, bare):
    monkeypatch.setattr(cfg, "config_path", lambda: tmp_path / "config.toml")
    cfg.write_memory_sync({"url": bare, "user": "Jason"})
    return _Bridge(tmp_path / "knowledge")


def test_create_repo_then_refresh(bridge, bare, monkeypatch):
    assert "doesn't exist yet" in bridge.memory_sync_refresh_docs()["error"]
    res = bridge.memory_sync_create_repo()
    assert res["ok"] and res["branch"] == "main" and res["files"] > 40
    tree = gitsync.fetch_dir(bare, "main", LOCAL, "", ".md", recursive=True).files
    assert "README.md" in tree and "runspec/index.md" in tree

    again = bridge.memory_sync_create_repo()
    assert not again["ok"] and "already a wiki" in again["error"]

    # Nothing changed since the template: nothing to push.
    assert bridge.memory_sync_refresh_docs() == {
        "ok": True,
        "changed": False,
        "pages": 0,
        "branch": "main",
    }

    # A newer console's summaries change one page and drop the others.
    monkeypatch.setattr(okf, "load_seed", lambda path=okf.SEED_PATH: SEED)
    res = bridge.memory_sync_refresh_docs()
    assert res["ok"] and res["branch"] == okf.REFRESH_BRANCH and res["pages"] > 1
    refreshed = gitsync.fetch_dir(
        bare, okf.REFRESH_BRANCH, LOCAL, "runspec", ".md", recursive=True
    ).files
    assert set(refreshed) == {
        "index.md",
        "apps/runspec.md",
        "apps/runspec-console.md",
        "topics/autonomy.md",
        "features/forms.md",
    }
    # main is untouched until someone merges the branch.
    assert (
        "topics/runnable.md"
        in gitsync.fetch_dir(
            bare, "main", LOCAL, "runspec", ".md", recursive=True
        ).files
    )


def test_template_files_are_lf_even_from_a_crlf_checkout(tmp_path, monkeypatch):
    (tmp_path / "index.md").write_bytes(b'---\r\nokf_version: "0.2"\r\n---\r\n# x\r\n')
    monkeypatch.setattr(okf, "TEMPLATE_DIR", tmp_path)
    assert okf.template_static() == {"index.md": b'---\nokf_version: "0.2"\n---\n# x\n'}


def test_create_into_an_existing_branch_goes_to_a_setup_branch(
    bridge, bare, monkeypatch, tmp_path
):
    """A GitLab repo created with a README already has its default branch."""
    cfg.write_memory_sync({"url": bare, "user": "Jason", "branch": "master"})
    gitsync.push_file(bare, "master", "README.md", b"# wiki\n", LOCAL)
    res = bridge.memory_sync_create_repo()
    assert res["ok"] and res["review"] and res["branch"] == okf.SETUP_BRANCH
    assert res["base"] == "master"
    master = gitsync.fetch_dir(bare, "master", LOCAL, "", ".md").files
    assert master == {"README.md": b"# wiki\n"}  # untouched
    setup = gitsync.fetch_dir(
        bare, okf.SETUP_BRANCH, LOCAL, "", ".md", recursive=True
    ).files
    assert "index.md" in setup and "runspec/index.md" in setup
    from dulwich.repo import Repo

    repo = Repo(bare)
    head = repo[repo.refs[b"refs/heads/" + okf.SETUP_BRANCH.encode()]]
    assert head.parents == [repo.refs[b"refs/heads/master"]]  # mergeable as an MR


def test_is_wiki_root():
    assert okf.is_wiki_root(b'---\nokf_version: "0.2"\n---\n# x\n')
    assert not okf.is_wiki_root(b"# readme\n")
    assert not okf.is_wiki_root(None)
