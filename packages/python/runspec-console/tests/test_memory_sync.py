"""Memory Sync: sharing the knowledge wiki through git — the pure helpers in
knowledge.py, gitsync.push_file / fetch_dir against a local bare repo, and the
bridge push / pull (bridge_memory_sync.py)."""

from __future__ import annotations

import tomllib

import pytest

pytest.importorskip("dulwich")

import runspec_console.config as cfg  # noqa: E402
from runspec_console import gitsync  # noqa: E402
from runspec_console import knowledge as kn  # noqa: E402
from runspec_console.bridge_knowledge import KnowledgeMixin  # noqa: E402
from runspec_console.bridge_memory_sync import MemorySyncMixin  # noqa: E402

LOCAL = gitsync.GitAuth(scheme="local")


@pytest.fixture
def bare(tmp_path):
    from dulwich.repo import Repo

    path = tmp_path / "wiki.git"
    path.mkdir()
    Repo.init_bare(str(path))
    return str(path)


def _approved(text, about=("host:web-01",)):
    f = kn.make_fact(
        kind="location", about=list(about), text=text, status="approved", source="agent"
    )
    f["evidence"] = ["run:abc"]
    return f


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_lane_name():
    assert kn.lane_name("Jason.Finestone") == "jason.finestone"
    assert kn.lane_name("DOMAIN\\jason") == "domain-jason"
    assert kn.lane_name("  ") == "user"
    assert kn.is_team_file("alice.toml") and not kn.is_team_file("../x.toml")


def test_contribution_shares_approved_facts_only():
    keep = _approved("orders logs live in /var/log/orders")
    pending = dict(_approved("pending one"), status="pending")
    retired = dict(_approved("retired one"), status="retired")
    leaked = dict(_approved("fine"), text="the db password = hunter2abc")
    data, count = kn.contribution([pending, keep, retired, leaked])
    assert count == 1
    rows = tomllib.loads(data.decode())["fact"]
    assert [r["id"] for r in rows] == [keep["id"]]
    assert "evidence" not in rows[0]  # local run ids stay home


def test_write_team_replaces_and_validates(tmp_path):
    store = kn.KnowledgeStore(tmp_path)
    a, _ = kn.contribution([_approved("a fact")])
    b, _ = kn.contribution([_approved("b fact")])
    got = store.write_team({"alice.toml": a, "bob.toml": b})
    assert got == {"files": 2, "facts": 2, "added": 2, "removed": 0}
    assert len(store.team_facts()) == 2
    got = store.write_team({"alice.toml": a})
    assert got["removed"] == 1 and not (tmp_path / "team" / "bob.toml").exists()
    with pytest.raises(ValueError, match="TOML"):
        store.write_team({"carol.toml": b"[[fact]\n"})
    assert (tmp_path / "team" / "alice.toml").exists()  # a bad pull writes nothing


def test_memory_sync_config_round_trip(monkeypatch, tmp_path):
    from runspec_console import configmerge

    monkeypatch.setattr(cfg, "config_path", lambda: tmp_path / "config.toml")
    cfg.write_memory_sync({"url": "https://x/wiki.git", "branch": "main", "user": "j"})
    raw = cfg.read_config()["memory_sync"]
    assert raw == {"url": "https://x/wiki.git", "user": "j"}  # default branch dropped
    assert "config.toml:memory_sync.user" in configmerge.DEFAULT_PRESERVE


# ── git layer ────────────────────────────────────────────────────────────────


def test_push_file_creates_updates_and_noops(bare):
    r = gitsync.push_file(bare, "contrib/j", "facts/j.toml", b"one\n", LOCAL)
    assert r.changed and r.created
    again = gitsync.push_file(bare, "contrib/j", "facts/j.toml", b"one\n", LOCAL)
    assert not again.changed and again.commit == r.commit
    r2 = gitsync.push_file(bare, "contrib/j", "facts/j.toml", b"two\n", LOCAL)
    assert r2.changed and not r2.created
    got = gitsync.fetch_dir(bare, "contrib/j", LOCAL, "facts")
    assert got.files == {"j.toml": b"two\n"} and got.commit == r2.commit


def test_push_file_refuses_odd_paths(bare):
    with pytest.raises(gitsync.GitSyncError):
        gitsync.push_file(bare, "contrib/j", "../config.toml", b"x", LOCAL)


def test_push_file_starts_from_the_team_branch(bare):
    gitsync.push_file(bare, "main", "README.md", b"wiki\n", LOCAL)
    gitsync.push_file(bare, "contrib/j", "facts/j.toml", b"x\n", LOCAL)
    from dulwich.repo import Repo

    repo = Repo(bare)
    head = repo[repo.refs[b"refs/heads/contrib/j"]]
    assert head.parents == [repo.refs[b"refs/heads/main"]]


def test_push_file_retries_a_moved_branch(monkeypatch, bare):
    calls = []
    real = gitsync._push_once

    def flaky(*args, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise gitsync.PushConflictError("moved")
        return real(*args, **kw)

    monkeypatch.setattr(gitsync, "_push_once", flaky)
    assert gitsync.push_file(bare, "contrib/j", "facts/j.toml", b"x", LOCAL).changed
    assert len(calls) == 2
    calls.clear()
    monkeypatch.setattr(
        gitsync,
        "_push_once",
        lambda *a, **k: (_ for _ in ()).throw(gitsync.PushConflictError("moved")),
    )
    with pytest.raises(gitsync.PushConflictError):
        gitsync.push_file(bare, "contrib/j", "facts/j.toml", b"y", LOCAL)


def test_fetch_dir_on_an_empty_repo(bare):
    got = gitsync.fetch_dir(bare, "main", LOCAL, "facts")
    assert got.files == {} and got.commit == ""


# ── bridge ───────────────────────────────────────────────────────────────────


class _Bridge(MemorySyncMixin, KnowledgeMixin):
    def __init__(self, root):
        self.root = root
        self.events = []

    def _knowledge_store(self):
        return kn.KnowledgeStore(self.root)

    def _sync_auth(self, url, credential_id, tls=None):
        return LOCAL

    def _dispatch(self, name, payload):
        self.events.append(name)


@pytest.fixture
def bridge(monkeypatch, tmp_path, bare):
    monkeypatch.setattr(cfg, "config_path", lambda: tmp_path / "config.toml")
    cfg.write_memory_sync({"url": bare, "user": "Jason"})
    return _Bridge(tmp_path / "knowledge")


def test_push_and_pull_round_trip(bridge, bare):
    store = bridge._knowledge_store()
    assert bridge.memory_sync_push()["facts"] == 0  # nothing approved yet: empty file
    mine = _approved("orders logs live in /var/log/orders")
    kn._save(store.root / kn.FACTS_FILE, [mine, dict(mine, id="k_x", status="retired")])

    status = bridge.memory_sync_status()
    assert status["shareable"] == 1 and status["unpushed"]
    assert status["contrib_branch"] == "contrib/jason"

    res = bridge.memory_sync_push()
    assert res["ok"] and res["changed"] and res["facts"] == 1
    assert not bridge.memory_sync_status()["unpushed"]
    assert bridge.memory_sync_push()["changed"] is False

    # The repo merges contributions into main: mine plus a teammate's.
    alice, _ = kn.contribution(
        [_approved("web-02 reboots on Sundays", ["host:web-02"])]
    )
    shared = gitsync.fetch_dir(bare, "contrib/jason", LOCAL, "facts").files
    gitsync.push_file(bare, "main", "facts/jason.toml", shared["jason.toml"], LOCAL)
    gitsync.push_file(bare, "main", "facts/alice.toml", alice, LOCAL)
    gitsync.push_file(bare, "main", "facts/Bad Name.toml", alice, LOCAL)

    pulled = bridge.memory_sync_pull()
    assert pulled["ok"] and pulled["files"] == 1 and pulled["added"] == 1
    assert pulled["skipped"] == ["Bad Name.toml"]
    assert sorted(p.name for p in (store.root / "team").iterdir()) == ["alice.toml"]
    status = bridge.memory_sync_status()
    assert status["team_facts"] == 1 and status["last_pull"]
    assert "runspec:knowledge_updated" in bridge.events


def test_pull_from_an_empty_repo_is_fine(bridge):
    res = bridge.memory_sync_pull()
    assert res["ok"] and res["files"] == 0


def test_memory_sync_test_and_unconfigured(bridge, monkeypatch, tmp_path):
    got = bridge.memory_sync_test()
    assert got["ok"] and got["empty"]
    bridge.memory_sync_push()
    got = bridge.memory_sync_test()
    assert got["contrib_exists"] and not got["branch_exists"]
    cfg.write_memory_sync({})
    assert not bridge.memory_sync_push()["ok"]
    assert not bridge.memory_sync_status()["configured"]


def test_save_rejects_a_contrib_team_branch(bridge):
    assert not bridge.save_memory_sync_config({"url": "x", "branch": "contrib/a"})["ok"]
    got = bridge.save_memory_sync_config({"url": "https://x/w.git", "user": "Al"})
    assert got["ok"] and got["lane"] == "al" and got["path"] == "facts/al.toml"
