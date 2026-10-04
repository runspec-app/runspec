"""Team knowledge wiki: the pure store/index/capture (knowledge.py) and the bridge
tools (bridge_knowledge.py), temp-dir backed."""

import json

import pytest


@pytest.fixture
def kn(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from runspec_console import knowledge

    return knowledge


@pytest.fixture
def store(kn, tmp_path):
    return kn.KnowledgeStore(tmp_path / "knowledge")


def _fact(kn, text, about=("host:web-01",), kind="location", status="pending"):
    return kn.make_fact(
        kind=kind, about=list(about), text=text, status=status, source="agent"
    )


def test_normalize_entity(kn):
    assert kn.normalize_entity(" Host: web-01 ") == "host:web-01"
    assert kn.normalize_entity("blue-green") == "topic:blue-green"
    assert kn.normalize_entity("C:\\logs\\app.log") == "topic:C:\\logs\\app.log"
    assert kn.normalize_entity("file:/var/log/a.log") == "file:/var/log/a.log"
    assert kn.normalize_entity("") == ""


def test_fact_id_is_content_hash(kn):
    a = _fact(kn, "Logs live in /var/log/app.log")
    b = _fact(kn, "logs live in  /var/log/app.log.")
    assert a["id"] == b["id"]  # case/punctuation/whitespace-insensitive
    assert a["id"] != _fact(kn, "Logs live in /srv/app.log")["id"]


@pytest.mark.parametrize(
    "text",
    [
        "the db password = hunter2abc",
        "use token: ghp_abcdefghijklmnopqrstuvwxyz123456",
        "-----BEGIN OPENSSH PRIVATE KEY----- …",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123",
    ],
)
def test_secrets_are_refused(kn, text):
    with pytest.raises(ValueError, match="secret"):
        _fact(kn, text)


def test_fact_needs_an_entity(kn):
    with pytest.raises(ValueError):
        kn.make_fact(kind="note", about=[], text="x", status="pending", source="agent")


def test_propose_review_and_dedupe(store, kn):
    f = _fact(
        kn,
        "orders logs are in /var/log/orders/app.log",
        about=["host:web-01", "app:orders"],
    )
    assert store.propose(f) == (f["id"], "proposed")
    assert store.propose(f)[1] == "already-pending"
    assert len(store.inbox()) == 1 and store.inbox()[0]["confirmations"] == 2

    approved = store.review(f["id"], True)
    assert approved["status"] == "approved"
    assert store.inbox() == []
    assert store.propose(f)[1] == "confirmed"
    assert store.own_facts()[0]["confirmations"] == 3

    g = _fact(kn, "web-01 reboots on Sundays", kind="host")
    store.propose(g)
    assert store.review(g["id"], False) == {}
    assert store.review("k_nope", True) is None


def test_review_with_edits_reids(store, kn):
    f = _fact(kn, "typo'd fact")
    store.propose(f)
    res = store.review(f["id"], True, {"text": "the fixed fact"})
    assert res["text"] == "the fixed fact" and res["id"] != f["id"]


def test_edit_and_retire(store, kn):
    f = _fact(kn, "deploys go through jenkins", status="approved", kind="procedure")
    store.add_approved(f)
    new = store.edit(f["id"], {"text": "deploys go through GitLab CI"})
    ids = {r["id"]: r["status"] for r in store.own_facts()}
    assert ids[f["id"]] == "retired" and ids[new["id"]] == "approved"
    assert store.retire(new["id"])
    idx = store.index()
    try:
        assert idx.search("deploys") == []
    finally:
        idx.close()


def test_team_facts_merge_and_retire_overrides(store, kn):
    f = _fact(kn, "web-01 is the orders primary", status="approved", kind="host")
    team = store.root / "team"
    team.mkdir(parents=True)
    kn._save(team / "alice.toml", [dict(f, confirmations=4)])
    assert store.all_facts()[0]["confirmations"] == 4
    store.add_approved(f)  # my copy of the same fact
    assert store.all_facts()[0]["confirmations"] == 5
    store.retire(f["id"])
    assert store.all_facts()[0]["status"] == "retired"


def test_index_search_page_graph(store, kn):
    store.add_approved(
        _fact(
            kn,
            "orders logs are in /var/log/orders/app.log",
            about=["host:web-01", "app:orders", "file:/var/log/orders/app.log"],
            status="approved",
        )
    )
    store.add_approved(
        _fact(
            kn,
            "a blue-green deploy swaps the LB pool",
            kind="glossary",
            about=["term:blue-green", "service:lb"],
            status="approved",
        )
    )
    store.propose(_fact(kn, "web-01 has 64GB RAM", kind="host"))
    idx = store.index()
    try:
        hits = idx.search("orders log")
        assert hits and "orders" in hits[0]["text"]
        assert idx.search("RAM") == []  # pending excluded by default
        assert idx.search("RAM", include_pending=True)
        page = idx.page("host:web-01")
        assert len(page["facts"]) == 2  # pending included on the page
        assert {lnk["entity"] for lnk in page["links"]} >= {"app:orders"}
        g = idx.graph()
        assert {"host:web-01", "term:blue-green"} <= {n["id"] for n in g["nodes"]}
        assert any(
            {e["source"], e["target"]} == {"app:orders", "host:web-01"}
            for e in g["edges"]
        )
        sub = idx.graph(entity="term:blue-green")
        assert {n["id"] for n in sub["nodes"]} == {"term:blue-green", "service:lb"}
        stats = idx.stats()
        assert stats["approved"] == 2 and stats["pending"] == 1
    finally:
        idx.close()
    # The index rebuilds when a source file changes.
    store.add_approved(
        _fact(
            kn,
            "web-02 is the standby",
            kind="host",
            about=["host:web-02"],
            status="approved",
        )
    )
    idx = store.index()
    try:
        assert idx.search("standby")
    finally:
        idx.close()


def test_search_without_fts(store, kn, monkeypatch):
    store.add_approved(
        _fact(kn, "orders logs are in /var/log/orders", status="approved")
    )
    idx = store.index()
    idx.fts = False
    try:
        assert idx.search("orders")[0]["text"].startswith("orders")
        assert idx.search("")  # empty query lists by confirmations
    finally:
        idx.close()


def _run(runnable, ts, host="web-01", op="jason", args=None, rid=None):
    return {
        "runnable": runnable,
        "ts": ts,
        "host": host,
        "operator": op,
        "args": args or {},
        "runId": rid or ts,
        "kind": "runnable",
    }


def test_observe_mines_habits_hosts_and_pairings(kn):
    hist = []
    for day in range(1, 5):
        hist.append(
            _run(
                "tail-log",
                f"2026-10-0{day}T09:00:00+00:00",
                args={"path": "/var/log/app.log", "lines": 100 + day},
            )
        )
        hist.append(
            _run(
                "restart-service",
                f"2026-10-0{day}T09:05:00+00:00",
                args={"name": "orders"},
            )
        )
    hist.append(_run("chat", "2026-10-01T10:00:00+00:00"))
    facts = kn.observe(hist)
    texts = [f["text"] for f in facts]
    assert any(
        "tail-log is usually run with path = '/var/log/app.log'" in t for t in texts
    )
    assert not any("lines" in t for t in texts)  # varies every run
    assert any("tail-log is used on web-01 (4 runs)" in t for t in texts)
    assert any(
        "restart-service is often run after tail-log (4 times)" in t for t in texts
    )
    habit = next(f for f in facts if "path =" in f["text"])
    assert "file:/var/log/app.log" in habit["about"]
    assert habit["status"] == "observed" and habit["confirmations"] == 4
    assert (
        habit["first_seen"] == "2026-10-01" and habit["last_confirmed"] == "2026-10-04"
    )
    assert kn.observe(hist) == facts  # deterministic
    assert kn.observe(hist[:2]) == []  # below threshold


def test_parse_distill(kn):
    reply = 'Here you go:\n```json\n[{"kind":"glossary","about":"term:BAU","text":"BAU means business as usual"}, {"bad":1}]\n```'
    out = kn.parse_distill(reply)
    assert out == [
        {
            "kind": "glossary",
            "about": ["term:BAU"],
            "text": "BAU means business as usual",
        }
    ]
    assert kn.parse_distill("no json") == []
    prompt = kn.build_distill_prompt(
        [
            {
                "user": "where are the logs",
                "assistant": "in /var/log",
                "tools": [
                    {"name": "run_runnable", "input": {"id": "x"}, "output": "SECRET"}
                ],
            }
        ],
        known=["old fact"],
    )
    assert (
        "old fact" in prompt
        and "where are the logs" in prompt
        and "SECRET" not in prompt
    )


# ── bridge tools ─────────────────────────────────────────────────────────────


@pytest.fixture
def bridge_stub(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    import threading

    from runspec_console.bridge import Bridge

    class _Stub:
        _knowledge_tools = Bridge._knowledge_tools
        _run_knowledge_tool = Bridge._run_knowledge_tool
        _run_knowledge_tool_inner = Bridge._run_knowledge_tool_inner
        _knowledge_store = Bridge._knowledge_store
        _knowledge_changed = Bridge._knowledge_changed
        _knowledge_query = Bridge._knowledge_query
        _knowledge_source = Bridge._knowledge_source
        _effective_tool_autonomy = Bridge._effective_tool_autonomy
        knowledge_overview = Bridge.knowledge_overview
        knowledge_capture = Bridge.knowledge_capture
        knowledge_distill = Bridge.knowledge_distill
        knowledge_save_fact = Bridge.knowledge_save_fact
        knowledge_review = Bridge.knowledge_review

        def __init__(self):
            self._lock = threading.Lock()
            self._run_origin = {"trig": {"trigger": "mail", "agent": True}}
            self.events = []
            self.history = []
            self.history_days = []
            self.adapter = None

        def _utility_adapter(self):
            return self.adapter

        def _dispatch(self, ev, payload):
            self.events.append(ev)

        def get_history_all(self, since_days=30):
            self.history_days.append(since_days)
            return self.history

    return _Stub()


def test_tool_gates_and_exposure(bridge_stub):
    from runspec_console.bridge_const import (
        _BUILTIN_TOOL_NAMES,
        _HOT_BUILTIN_NAMES,
        _KNOWLEDGE_TOOL_NAMES,
        _MCP_SERVER_BUILTIN_NAMES,
    )

    names = {t["name"] for t in bridge_stub._knowledge_tools()}
    assert names == set(_KNOWLEDGE_TOOL_NAMES)
    assert _KNOWLEDGE_TOOL_NAMES <= _MCP_SERVER_BUILTIN_NAMES  # Claude Code gets them
    assert _KNOWLEDGE_TOOL_NAMES <= _BUILTIN_TOOL_NAMES
    assert {"search_wiki", "wiki_page", "propose_fact"} <= _HOT_BUILTIN_NAMES
    for n in (
        "search_wiki",
        "wiki_page",
        "wiki_graph",
        "propose_fact",
        "list_wiki_inbox",
    ):
        assert bridge_stub._effective_tool_autonomy(n, {}) == "autonomous"
    for n in ("review_wiki_fact", "edit_wiki_fact", "retire_wiki_fact"):
        assert bridge_stub._effective_tool_autonomy(n, {}) == "confirm"


def test_tool_roundtrip_and_sources(bridge_stub):
    run = bridge_stub._run_knowledge_tool
    out = run(
        "propose_fact",
        {
            "kind": "location",
            "about": ["host:web-01"],
            "text": "app logs are in /srv/logs",
        },
        "__mcp_server__",
    )
    assert out.startswith("Proposed k_")
    run(
        "propose_fact",
        {"kind": "glossary", "about": ["term:BAU"], "text": "BAU is business as usual"},
        "trig",
    )
    inbox = json.loads(run("list_wiki_inbox", {}))["inbox"]
    assert {r["source"] for r in inbox} == {"mcp", "trigger:mail"}
    assert run("search_wiki", {"query": "logs"}).startswith(
        "No wiki facts"
    )  # still pending
    fid = next(r["id"] for r in inbox if r["source"] == "mcp")
    assert run("review_wiki_fact", {"fact_id": fid, "approve": True}).startswith(
        "Approved"
    )
    assert "/srv/logs" in run("search_wiki", {"query": "logs"})
    page = json.loads(run("wiki_page", {"entity": "host:web-01"}))
    assert page["facts"][0]["text"] == "app logs are in /srv/logs"
    assert "host:web-01" in run("wiki_graph", {})
    assert run(
        "propose_fact",
        {"kind": "note", "about": ["host:x"], "text": "password=hunter22"},
    ).startswith("Error:")
    assert run("edit_wiki_fact", {"fact_id": fid}).startswith("Error:")
    assert run("retire_wiki_fact", {"fact_id": fid}).startswith("Retired")
    assert "runspec:knowledge_updated" in bridge_stub.events


def test_overview_captures_and_operator_facts(bridge_stub):
    bridge_stub.history = [
        _run("tail-log", f"2026-10-0{d}T09:00:00+00:00", args={"path": "/var/log/a"})
        for d in range(1, 5)
    ]
    ov = bridge_stub.knowledge_overview()
    assert ov["stats"]["observed"] >= 2
    assert any(n["id"] == "runnable:tail-log" for n in ov["graph"]["nodes"])
    res = bridge_stub.knowledge_save_fact(
        {"kind": "glossary", "about": ["term:P1"], "text": "P1 means wake someone up"}
    )
    assert res["ok"] and res["fact"]["status"] == "approved"
    bad = bridge_stub.knowledge_save_fact({"kind": "note", "about": [], "text": "x"})
    assert bad["ok"] is False
    assert bridge_stub.knowledge_review("k_none", True)["ok"] is False


def test_chunk_transcripts_marks_a_block_on_its_last_piece(kn):
    big = "\n".join(f"line {i} " + "x" * 90 for i in range(40))  # ~4k chars
    chunks = kn.chunk_transcripts(
        [("a", "short a"), ("b", big), ("c", ""), ("d", "short d")], max_chars=1500
    )
    assert all(len(text) <= 1500 for text, _ in chunks)
    finished = [k for _, keys in chunks for k in keys]
    assert finished == ["a", "b", "c", "d"]
    # b spans several chunks and is only finished on the last one
    b_chunks = [
        i
        for i, (text, _) in enumerate(chunks)
        if "line 0 " in text or "line 39 " in text
    ]
    assert "b" in chunks[b_chunks[-1]][1] and "b" not in chunks[b_chunks[0]][1]
    assert "line 0 " in "".join(t for t, _ in chunks)  # nothing dropped


def test_capture_range_is_remembered(bridge_stub):
    bridge_stub.knowledge_capture(0)
    assert bridge_stub.history_days[-1] is None  # 0 = all history
    bridge_stub.knowledge_capture()  # auto-capture keeps the operator's range
    assert bridge_stub.history_days[-1] is None
    assert bridge_stub.knowledge_overview()["capture_days"] == 0
    bridge_stub.knowledge_capture(90)
    assert bridge_stub.history_days[-1] == 90


class _FakeAdapter:
    def __init__(self):
        self.prompts = []

    async def chat(self, messages, tools):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        n = len(self.prompts)

        class _R:
            text = json.dumps(
                [{"kind": "note", "about": [f"topic:t{n}"], "text": f"fact number {n}"}]
            )

        return _R()


def _write_conv(tmp_path, cid, updated, turns):
    from runspec_console import config

    d = config.conversations_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{cid}.json").write_text(
        json.dumps(
            {
                "id": cid,
                "title": cid,
                "created": updated,
                "updated": updated,
                "turns": turns,
            }
        ),
        encoding="utf-8",
    )


def test_distill_reads_all_history_in_batches_and_resumes(bridge_stub, tmp_path):
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    new = datetime.now(timezone.utc).isoformat()
    turn = {
        "user": "where are the logs " + "y" * 700,
        "assistant": "in /var/log " + "z" * 700,
    }
    for i in range(30):  # ~45k chars of chat → more than one chunk
        _write_conv(tmp_path, f"old{i:02d}", old, [dict(turn)])
    _write_conv(
        tmp_path, "recent", new, [{"user": "recent question", "assistant": "ok"}]
    )
    bridge_stub.adapter = _FakeAdapter()

    # A 7-day window only reads the recent chat.
    r = bridge_stub.knowledge_distill(7)
    assert (
        r["ok"] and r["calls"] == 1 and r["conversations"] == 1 and r["remaining"] == 0
    )
    assert "recent question" in bridge_stub.adapter.prompts[0]

    # All history, one call per click: the backlog continues across clicks and
    # the already-read recent chat is not sent again.
    r = bridge_stub.knowledge_distill(0, max_calls=1)
    assert r["calls"] == 1 and r["remaining"] >= 1
    total = r["conversations"]
    while r["remaining"]:
        r = bridge_stub.knowledge_distill(0, max_calls=1)
        total += r["conversations"]
    assert total == 30
    assert not any("recent question" in p for p in bridge_stub.adapter.prompts[1:])
    assert bridge_stub.knowledge_distill(0)["note"]  # nothing new left

    # New turns appended to a chat are picked up on their own.
    _write_conv(
        tmp_path,
        "recent",
        new,
        [
            {"user": "recent question", "assistant": "ok"},
            {"user": "follow-up about /srv", "assistant": "ok"},
        ],
    )
    before = len(bridge_stub.adapter.prompts)
    r = bridge_stub.knowledge_distill(7)
    assert r["calls"] == 1
    last = bridge_stub.adapter.prompts[-1]
    assert len(bridge_stub.adapter.prompts) == before + 1
    assert "follow-up about /srv" in last and "recent question" not in last
    inbox = bridge_stub.knowledge_overview()["inbox"]
    assert len(inbox) == len(bridge_stub.adapter.prompts)  # one distinct fact per call
