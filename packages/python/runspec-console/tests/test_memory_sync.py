"""Memory Sync: sharing the knowledge wiki through git — the pure helpers in
knowledge.py, gitsync.push_file / fetch_dir against a local bare repo, and the
bridge push / pull (bridge_memory_sync.py)."""

from __future__ import annotations

import tomllib

import tomli_w

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


def _approved(text, about=("app:orders",)):
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


def test_machine_local_facts_are_never_shared(tmp_path):
    shared = _approved("orders logs live in /var/log/orders")
    mine = _approved("the console venv is C:\\venvs\\ops", about=("host:local",))
    both = _approved("cron runs nightly here", about=("Host: local", "runnable:cron"))
    data, count = kn.contribution([shared, mine, both])
    assert count == 1
    assert [r["id"] for r in tomllib.loads(data.decode())["fact"]] == [shared["id"]]

    # A teammate's old push that still carries a fact about their `local` is
    # not read as a fact about this machine.
    old = tomli_w.dumps({"fact": [shared, mine]}).encode()
    store = kn.KnowledgeStore(tmp_path)
    store.write_team({"alice.toml": old})
    assert [f["id"] for f in store.team_facts()] == [shared["id"]]


def test_this_console_facts_are_never_shared():
    team = _approved("tail-log after http-check finds a 502 fastest")
    host = _approved("web-01 is slow on Mondays", about=("host:web-01",))
    named = _approved("restart orders-web-a before the batch", about=("app:orders",))
    home = _approved("the key lives in ~/.ssh/ops", about=("app:orders",))
    account = _approved("ask jason@desk-01 for access", about=("app:orders",))
    observed = dict(_approved("B often follows A"), source="capture")
    data, count = kn.contribution(
        [team, host, named, home, account, observed],
        ["orders-web-a", "prod"],
        lane="ops-jason",
    )
    rows = tomllib.loads(data.decode())["fact"]
    assert count == 1 and [r["id"] for r in rows] == [team["id"]]
    assert rows[0]["author"] == "ops-jason"  # signed by the lane, not user@host

    preview = kn.share_preview([team, host, named], ["orders-web-a"])
    assert [r["id"] for r in preview["share"]] == [team["id"]]
    kept = {r["id"]: r["reason"] for r in preview["kept"]}
    assert "host:web-01" in kept[host["id"]] and "orders-web-a" in kept[named["id"]]


def test_fact_scope_keeps_generic_tool_facts_on_the_team():
    f = _approved("orders logs live in /var/log/orders/app.log")
    assert kn.fact_scope(f) == ("team", "")
    assert kn.fact_scope(f, ["prod"])[0] == "team"  # common names don't count
    assert kn.with_scope(f, [])["scope"] == "team"


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
        [
            _approved(
                "http-check needs --insecure on the lab CA", ["runnable:http-check"]
            )
        ]
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
    assert res["pages"]["pages"] == 0


def test_pull_brings_in_the_wiki_pages(bridge, bare):
    page = b"---\ntype: Runnable\n---\n\n# cron\n\n## Works with\n\n- run list first\n"
    gitsync.push_file(bare, "main", "runnables/cron.md", page, LOCAL)
    gitsync.push_file(bare, "main", "runnables/index.md", b"# Runnables\n", LOCAL)
    gitsync.push_file(bare, "main", "runspec/index.md", b"# generated\n", LOCAL)
    res = bridge.memory_sync_pull()
    assert res["ok"] and res["pages"] == {"pages": 1, "added": 1, "removed": 0}
    store = bridge._knowledge_store()
    assert (store.root / "pages" / "runnables" / "cron.md").read_bytes() == page


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


def test_push_uses_the_commit_email(bridge, bare):
    from dulwich.repo import Repo

    assert bridge.save_memory_sync_config(
        {"url": bare, "user": "Jason F", "email": "jason@acme.com"}
    )["ok"]
    bridge.memory_sync_push()
    repo = Repo(bare)
    head = repo[repo.refs[b"refs/heads/contrib/jason-f"]]
    assert head.author == b"Jason F <jason@acme.com>"
    assert head.committer == head.author
    assert bridge.get_memory_sync_config()["config"]["email"] == "jason@acme.com"
    assert not bridge.save_memory_sync_config({"url": bare, "email": "nope"})["ok"]


def test_author_rejection_says_what_to_fix(bridge, monkeypatch):
    def refuse(*a, **k):
        raise gitsync.GitSyncError(
            "push to contrib/jason rejected: GitLab: Author 'j@pc' is not a member of team"
        )

    monkeypatch.setattr(gitsync, "push_file", refuse)
    res = bridge.memory_sync_push()
    assert not res["ok"] and "Commit email" in res["error"]
    assert "not a member of team" in res["error"]


# ── pulling a release tag (pull_from) ────────────────────────────────────────


def _tag(bare, name, branch="main"):
    from dulwich.repo import Repo

    repo = Repo(bare)
    repo.refs[b"refs/tags/" + name.encode()] = repo.refs[
        b"refs/heads/" + branch.encode()
    ]


def _team_file(bare, lane, text):
    data, _ = kn.contribution([_approved(text, [f"host:{lane}"])])
    gitsync.push_file(bare, "main", f"facts/{lane}.toml", data, LOCAL)


def test_latest_release_picks_the_newest_in_the_series():
    tags = ["v1.9.0", "v1.10.0", "v2.0.0-rc1", "emea-v9.0", "notes"]
    assert gitsync.latest_release(tags) == "v1.10.0"
    assert gitsync.latest_release(tags, "emea-v") == "emea-v9.0"
    assert gitsync.latest_release(["notes"]) is None


def test_save_validates_the_pull_source(bridge):
    assert not bridge.save_memory_sync_config({"pull_from": "sideways"})["ok"]
    assert not bridge.save_memory_sync_config({"pull_from": "tag"})["ok"]
    got = bridge.save_memory_sync_config(
        {"url": "x", "pull_from": "tag", "pull_tag": "v1.0.0"}
    )
    assert got["ok"] and got["config"]["pull_from"] == "tag"
    assert cfg.read_memory_sync()["pull_tag"] == "v1.0.0"
    bridge.save_memory_sync_config({"url": "x", "pull_from": "branch"})
    assert "pull_from" not in cfg.read_memory_sync()  # the default isn't written


def test_pull_latest_tag_reads_the_release_not_the_branch(bridge, bare):
    cfg.write_memory_sync({"url": bare, "user": "Jason", "pull_from": "latest-tag"})
    _team_file(bare, "alice", "released fact")
    res = bridge.memory_sync_pull()
    assert not res["ok"] and "no release tag" in res["error"]

    _tag(bare, "v1.0.0")
    _team_file(bare, "bob", "merged but not released")
    assert bridge.memory_sync_check_update()["available"]  # never pulled
    res = bridge.memory_sync_pull()
    assert res["ok"] and res["ref"] == "v1.0.0" and res["files"] == 1
    status = bridge.memory_sync_status()
    assert status["last_pull_ref"] == "v1.0.0"
    assert status["pull_source"] == "the newest v* tag"
    assert not bridge.memory_sync_check_update()["available"]

    _tag(bare, "v1.1.0")
    chk = bridge.memory_sync_check_update()
    assert chk["available"] and chk["latest"] == "v1.1.0" and not chk["pinned"]
    assert bridge.memory_sync_pull()["files"] == 2


def test_pull_pinned_tag_stays_put(bridge, bare):
    _team_file(bare, "alice", "one")
    _tag(bare, "v1.0.0")
    _team_file(bare, "bob", "two")
    _tag(bare, "v1.1.0")
    cfg.write_memory_sync(
        {"url": bare, "user": "Jason", "pull_from": "tag", "pull_tag": "v1.0.0"}
    )
    res = bridge.memory_sync_pull()
    assert res["ok"] and res["ref"] == "v1.0.0" and res["files"] == 1
    chk = bridge.memory_sync_check_update()
    assert chk["available"] and chk["pinned"] and chk["latest"] == "v1.1.0"

    cfg.write_memory_sync(
        {"url": bare, "user": "Jason", "pull_from": "tag", "pull_tag": "v9.9.9"}
    )
    res = bridge.memory_sync_pull()
    assert not res["ok"] and "v9.9.9" in res["error"]


def test_branch_check_update_sees_new_commits(bridge, bare):
    _team_file(bare, "alice", "one")
    bridge.memory_sync_pull()
    assert not bridge.memory_sync_check_update()["available"]
    _team_file(bare, "bob", "two")
    assert bridge.memory_sync_check_update()["available"]


def test_facts_become_page_edits_and_propose_opens_a_merge_request(bridge, bare):
    from runspec_console import wiki_drafts, wiki_pages

    page = b"---\ntype: Runnable\n---\n\n# cron\n\n## Works with\n\n- run list first\n"
    gitsync.push_file(bare, "main", "runnables/cron.md", page, LOCAL)
    assert bridge.memory_sync_pull()["pages"]["pages"] == 1
    store = bridge._knowledge_store()

    # A pending fact from the inbox: Add to page approves it and edits the page.
    p = kn.make_fact(
        kind="pairing",
        about=["runnable:cron", "runnable:journal-vacuum"],
        text="run journal-vacuum after clearing old cron output",
        status="pending",
        source="agent",
    )
    store.propose(p)
    res = bridge.knowledge_draft_fact(p["id"])
    assert res == {"ok": True, "path": "runnables/cron.md", "heading": "Works with"}
    assert store.inbox() == []
    drafts = bridge.knowledge_drafts()
    assert [d["path"] for d in drafts["drafts"]] == ["runnables/cron.md"]
    assert p["id"] in drafts["drafted"]

    # A this-console fact is refused; conversion adds the rest, once.
    local = _approved("web-01 runs cron at 02:00", ["host:web-01", "runnable:cron"])
    term = _approved("BAU means business as usual", ["term:BAU"])
    kn._save(store.root / kn.FACTS_FILE, [*store.own_facts(), local, term])
    assert bridge.knowledge_draft_fact(local["id"])["ok"] is False
    got = bridge.knowledge_convert_facts()
    assert got["ok"] and got["kept_local"] == 1 and got["pages"] >= 1
    assert "terms/bau.md" in [d["path"] for d in bridge.knowledge_drafts()["drafts"]]
    assert bridge.knowledge_convert_facts()["facts"] == 0  # nothing twice

    # Propose pushes every draft to contrib/<lane> and links the merge request.
    out = bridge.memory_sync_propose()
    assert out["ok"] and out["pages"] == 2 and out["branch"] == "contrib/jason"
    assert out["merge_request_url"] == ""  # a local path has no web UI
    # The edits now show as proposed, until one changes again.
    after = bridge.knowledge_drafts()
    assert after["proposed_branch"] == "contrib/jason"
    assert all(d["proposed"] for d in after["drafts"])
    text = bridge.knowledge_draft_text("terms/bau.md")["text"]
    bridge.knowledge_draft_save("terms/bau.md", text + "\n- one more line\n")
    states = {d["path"]: d["proposed"] for d in bridge.knowledge_drafts()["drafts"]}
    assert states == {"runnables/cron.md": True, "terms/bau.md": False}
    bridge.knowledge_draft_save("terms/bau.md", text)
    pushed = gitsync.fetch_dir(
        bare, "contrib/jason", LOCAL, "", suffix=".md", recursive=True
    ).files
    assert b"journal-vacuum" in pushed["runnables/cron.md"]
    assert "terms/bau.md" in pushed

    # Once the team merges it, the next Pull clears the drafts.
    for path, data in pushed.items():
        if wiki_pages.is_page_path(path):
            gitsync.push_file(bare, "main", path, data, LOCAL)
    pulled = bridge.memory_sync_pull()
    assert pulled["drafts_landed"] == 2
    assert wiki_drafts.draft_paths(store.root) == []


def test_propose_with_nothing_to_propose(bridge):
    assert bridge.memory_sync_propose() == {
        "ok": False,
        "error": "No page edits to propose.",
    }


def test_draft_text_save_and_discard(bridge):
    got = bridge.knowledge_draft_text("", "runnable:df")
    assert got["ok"] and got["path"] == "runnables/df.md" and got["base"] is None
    assert "## Works with" in got["text"]
    assert bridge.knowledge_draft_save("runnables/df.md", got["text"] + "\n- x\n")[
        "kept"
    ]
    assert (
        bridge.knowledge_draft_save("runnables/df.md", "password = hunter2abc")["ok"]
        is False
    )
    assert bridge.knowledge_draft_discard("runnables/df.md")["discarded"]
    assert bridge.knowledge_draft_text("../x.md")["ok"] is False
