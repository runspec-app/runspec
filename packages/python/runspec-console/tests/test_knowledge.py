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
    return kn.KnowledgeStore(tmp_path / "knowledge", seed=False)


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


def test_observe_mines_habits_and_pairings(kn):
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
    # Where a runnable runs is History's job, not a fact (it swamped the wiki).
    assert not any(" is used on " in t for t in texts)
    assert not any(f["kind"] == "host" for f in facts)
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


def test_observe_skips_per_person_args(kn):
    """A username, email or run-as value differs per person, so it never becomes
    a habit fact — by arg name, or because the value is the operator's account."""
    hist = [
        _run(
            "tool-one",
            f"2026-10-0{day}T09:00:00+00:00",
            args={
                "username": "jason",
                "user-email": "jason@corp.com",
                "run_as": "svc-app",
                "target": "CORP\\Jason",  # the operator, by value
                "space": "OPS",
            },
        )
        for day in range(1, 5)
    ]
    texts = [f["text"] for f in kn.observe(hist)]
    habits = [t for t in texts if "usually run with" in t]
    assert habits == ["tool-one is usually run with space = 'OPS' (4 of 4 runs)."]


def test_observe_skips_unchanged_defaults(kn):
    """An arg left at the runnable's declared default is not a habit — only a
    value the operator actually chose is. Subcommands inherit parent defaults."""
    records = [
        {
            "name": "report",
            "args": [
                {"name": "format", "default": "json"},
                {"name": "verbose", "default": False},
                {"name": "space"},
            ],
            "commands": {
                "send": {"args": [{"name": "--max-rows", "default": 100}]},
            },
        }
    ]
    defaults = kn.arg_defaults(records)
    assert defaults["report"] == {"format": {"json"}, "verbose": {"False"}}
    assert defaults["report send"]["max_rows"] == {"100"}
    assert defaults["report send"]["format"] == {"json"}

    hist = []
    for day in range(1, 5):
        hist.append(
            _run(
                "report",
                f"2026-10-0{day}T09:00:00+00:00",
                args={"format": "json", "space": "OPS"},
            )
        )
        r = _run(
            "report",
            f"2026-10-0{day}T10:00:00+00:00",
            args={"format": "csv", "max-rows": 100},
        )
        r["commandPath"] = ["send"]
        hist.append(r)
    texts = [f["text"] for f in kn.observe(hist, defaults=defaults)]
    habits = sorted(t for t in texts if "usually run with" in t)
    assert habits == [
        "report is usually run with space = 'OPS' (4 of 4 runs).",
        "report send is usually run with format = 'csv' (4 of 4 runs).",
    ]
    # Without the defaults the unchanged ones would show up.
    assert any("format = 'json'" in f["text"] for f in kn.observe(hist))


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
        and "RESULT: SECRET" in prompt  # a short output excerpt rides along
    )


# ── bridge tools ─────────────────────────────────────────────────────────────


@pytest.fixture
def bridge_stub(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from runspec_console import knowledge

    # The shipped seed would match most searches; its own tests turn it back on.
    monkeypatch.setattr(knowledge, "SEED_PATH", None)
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
        _knowledge_autocapture = Bridge._knowledge_autocapture
        knowledge_capture = Bridge.knowledge_capture
        knowledge_distill = Bridge.knowledge_distill
        _knowledge_installed = Bridge._knowledge_installed
        _knowledge_config = Bridge._knowledge_config
        _knowledge_catalog = Bridge._knowledge_catalog
        knowledge_save_fact = Bridge.knowledge_save_fact
        knowledge_review = Bridge.knowledge_review
        knowledge_pin = Bridge.knowledge_pin
        _migrate_memory = Bridge._migrate_memory
        _knowledge_header = Bridge._knowledge_header
        _knowledge_pin_count = Bridge._knowledge_pin_count

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
    assert ov["capturing"] is True  # in the background, so the tab paints at once
    bridge_stub._knowledge_capture_thread.join(10)
    assert "runspec:knowledge_updated" in bridge_stub.events
    ov = bridge_stub.knowledge_overview()
    assert ov["capturing"] is False  # fresh now — no second capture
    assert ov["stats"]["observed"] >= 1
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


def test_overview_lists_installed_runnables(bridge_stub):
    assert bridge_stub.knowledge_overview()["installed_runnables"] == []
    bridge_stub._runnables_cache = [
        {"name": "disk-usage", "host": "local"},
        {"name": "disk-usage", "host": "web-01"},
        {"name": "cron", "host": "web-01"},
    ]
    assert bridge_stub.knowledge_overview()["installed_runnables"] == [
        "cron",
        "disk-usage",
    ]


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


def test_link_about_resolves_guesses_and_mentions(kn):
    cat = kn.entity_catalog(
        [
            "group:Linux Servers",
            "host:little-boy",
            "host:little-girl",
            "host:local",
            "runnable:disk-usage",
            "file:/var/log/x",  # not linkable
        ]
    )
    # A term guess for a group name joins the group; prose mentions are added.
    about = kn.link_about(
        ["term:linux-servers"],
        "Linux Servers is five Debian hosts: little-boy, little-girl and more.",
        cat,
    )
    assert about[0] == "group:Linux Servers"
    assert sorted(about) == [
        "group:Linux Servers",
        "host:little-boy",
        "host:little-girl",
    ]
    # Same type, other spelling → canonical; files and unknowns are kept as-is.
    assert kn.link_about(["host:Little-Boy", "file:/etc/x", "app:orders"], "", cat) == [
        "host:little-boy",
        "file:/etc/x",
        "app:orders",
    ]
    # A common word in prose is not a reference ("local time"), but it is
    # kept when the fact names it as a subject.
    assert kn.link_about(["topic:clock"], "uses local time", cat) == ["topic:clock"]
    assert kn.link_about(["host:local"], "local time", cat) == ["host:local"]
    # The longer name wins over its prefix.
    cat2 = kn.entity_catalog(["host:web-01", "host:web-01-backup"])
    assert kn.link_about(["topic:x"], "copy to web-01-backup nightly", cat2) == [
        "topic:x",
        "host:web-01-backup",
    ]


def test_observe_config(kn):
    facts = kn.observe_config(
        hosts=[
            {"name": "a", "group": "Linux Servers", "profiles": ["Homelab"]},
            {"name": "b", "group": "Linux Servers", "profiles": ["Homelab", "Work"]},
            {"name": "local", "group": None, "profiles": []},
        ],
        triggers=[
            {
                "name": "disk-alert",
                "source": "outlook",
                "event": "new_mail",
                "mode": "rule",
                "run": "group:Linux Servers__disk-usage",
            },
            {
                "name": "triage",
                "source": "chat",
                "mode": "agent",
                "runnables": ["summarize-log"],
                "enabled": False,
            },
        ],
        schedules=[
            {
                "id": "nightly",
                "action": "runnable",
                "runnable": "cron",
                "host": "a",
                "when": {"cron": "0 2 * * *"},
            },
            {
                "id": "weekly-rb",
                "action": "runbook",
                "runbook": "patch",
                "when": {"every": "7d"},
            },
        ],
        runbooks=[
            {
                "id": "patch",
                "title": "Patch the Linux boxes",
                "steps": [{"text": "run disk-usage first, then upgrade"}],
            },
        ],
        filters=[
            {"name": "errors", "pipeline": "grep ERROR", "description": "Only errors."}
        ],
        catalog=kn.entity_catalog(["runnable:disk-usage", "group:Linux Servers"]),
    )
    texts = {f["text"]: f for f in facts}
    assert all(f["status"] == "observed" and f["source"] == "config" for f in facts)
    # Group / profile membership is visible elsewhere — never a fact.
    assert not any(t.startswith(("Group ", "Profile ")) for t in texts)
    t = texts[
        "Trigger disk-alert fires on outlook new_mail and runs disk-usage on Linux Servers (rule mode)."
    ]
    assert set(t["about"]) == {
        "trigger:disk-alert",
        "group:Linux Servers",
        "runnable:disk-usage",
    }
    assert (
        "Trigger triage fires on chat and starts an agent turn (agent mode). It is disabled."
        in texts
    )
    assert "Schedule nightly runs cron on a (cron 0 2 * * *)." in texts
    assert "Schedule weekly-rb runs runbook patch (every 7d)." in texts
    rb = texts["Runbook patch: Patch the Linux boxes. It uses disk-usage."]
    assert set(rb["about"]) == {
        "runbook:patch",
        "runnable:disk-usage",
    }
    assert any(
        f["text"].startswith("File filter errors applies `grep ERROR`") for f in facts
    )


def test_capture_reads_config_and_relinks(bridge_stub):
    from runspec_console import knowledge

    store = bridge_stub._knowledge_store()
    # A fact written before the group existed: a lonely term.
    fact = knowledge.make_fact(
        kind="glossary",
        about=["term:Linux Servers"],
        text="Linux Servers is a group of Debian hosts: web-01 and web-02.",
        status="approved",
        source="operator",
    )
    old = store.add_approved(fact)
    bridge_stub._all_host_entries = lambda: [
        {"name": "web-01", "group": "Linux Servers"},
        {"name": "web-02", "group": "Linux Servers"},
    ]
    bridge_stub._read_schedules = lambda: []
    r = bridge_stub.knowledge_capture()
    assert r["ok"] and r["relinked"] == 1
    facts = {f["text"]: f for f in store.all_facts() if f.get("status") != "retired"}
    linked = facts[fact["text"]]
    assert linked["about"] == ["group:Linux Servers", "host:web-01", "host:web-02"]
    assert linked["id"] != old and linked["source"] == "operator"
    # Idempotent: a second capture changes nothing more.
    assert bridge_stub.knowledge_capture()["relinked"] == 0


def test_agent_proposals_are_linked(bridge_stub):
    bridge_stub._all_host_entries = lambda: [{"name": "web-01", "group": "Web"}]
    bridge_stub._run_knowledge_tool(
        "propose_fact",
        {
            "kind": "note",
            "about": ["term:web"],
            "text": "The web group serves orders from web-01.",
        },
    )
    pending = bridge_stub._knowledge_store().inbox()
    assert pending[0]["about"] == ["group:Web", "host:web-01"]


# ── pins (the replacement for the old memory header) ─────────────────────────


def test_pins_cap_follow_edits_and_drop_on_retire(store, kn):
    a = _fact(kn, "orders logs live in /var/log/orders", status="approved")
    store.add_approved(a)
    assert store.header() == ""
    ok, _ = store.set_pinned(a["id"], True)
    assert ok and store.pinned_ids() == [a["id"]]
    head = store.header()
    assert head.startswith("## Pinned facts (team wiki)")
    assert "orders logs live in /var/log/orders (host:web-01)" in head
    # an edit gives the fact a new id; the pin follows it
    edited = store.edit(a["id"], {"text": "orders logs live in /srv/orders"})
    assert store.pinned_ids() == [edited["id"]]
    assert "/srv/orders" in store.header()
    # retiring drops the pin
    store.retire(edited["id"])
    assert store.pinned_ids() == [] and store.header() == ""
    # unknown fact, and the cap
    assert store.set_pinned("k_nope", True)[0] is False
    for i in range(kn.PIN_MAX):
        f = _fact(kn, f"fact number {i} about web", status="approved")
        store.add_approved(f)
        assert store.set_pinned(f["id"], True)[0]
    extra = _fact(kn, "one fact too many", status="approved")
    store.add_approved(extra)
    ok, msg = store.set_pinned(extra["id"], True)
    assert not ok and "unpin one first" in msg


def test_pinned_pending_fact_follows_review(store, kn):
    p = _fact(kn, "web-01 runs the orders api")
    store.propose(p)
    assert store.set_pinned(p["id"], True)[0]  # a pin may sit on a pending fact
    assert "orders api" in store.header()
    approved = store.review(p["id"], True, {"text": "web-01 runs the orders API v2"})
    assert store.pinned_ids() == [approved["id"]]
    q = _fact(kn, "web-01 is in rack 7")
    store.propose(q)
    store.set_pinned(q["id"], True)
    store.review(q["id"], False)
    assert q["id"] not in store.pinned_ids()


def test_memory_to_facts(kn):
    rows = [
        {
            "id": "m_1",
            "app": "orders",
            "fact": "orders logs live in /var/log/o",
            "tags": ["logs"],
            "pinned": True,
            "hits": 2,
            "created": "2026-01-02T00:00:00+00:00",
        },
        {
            "id": "m_2",
            "app": "general",
            "fact": "the freeze starts Friday",
            "pinned": True,
            "hits": 9,
        },
        {"id": "m_3", "app": "x", "fact": "password: hunter2hunter2"},
        {"id": "m_4", "app": "general", "fact": ""},
    ]
    facts, pins, skipped = kn.memory_to_facts(rows, author="me@box")
    assert skipped == ["m_3"]
    by_text = {f["text"]: f for f in facts}
    o = by_text["orders logs live in /var/log/o"]
    assert o["about"] == ["app:orders", "topic:logs"]
    assert o["status"] == "pending" and o["source"] == "memory"
    assert o["evidence"] == ["m_1"] and o["first_seen"] == "2026-01-02"
    assert by_text["the freeze starts Friday"]["about"] == ["topic:general"]
    # most-recalled pin first
    assert pins == [by_text["the freeze starts Friday"]["id"], o["id"]]


def test_bridge_migrates_old_memory_once_and_pins_carry_over(bridge_stub, tmp_path):
    import json

    from runspec_console import config as config_mod

    path = config_mod.memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[[memory]]\nid = "m_1"\napp = "orders"\nfact = "orders logs live in /var/log/o"\n'
        "pinned = true\nhits = 3\n\n"
        '[[memory]]\nid = "m_2"\napp = "general"\nfact = "the change freeze is Friday"\n',
        encoding="utf-8",
    )
    store = bridge_stub._knowledge_store()
    assert not path.exists()
    assert path.with_name(path.name + ".migrated").exists()
    assert {f["text"] for f in store.inbox()} == {
        "orders logs live in /var/log/o",
        "the change freeze is Friday",
    }
    assert "orders logs live in /var/log/o" in bridge_stub._knowledge_header()
    assert bridge_stub._knowledge_pin_count() == 1
    # moved facts wait in the inbox (search shows approved facts only); once the
    # operator approves one, the agent finds it, marked pinned, and can unpin it
    assert "No wiki facts match" in bridge_stub._run_knowledge_tool(
        "search_wiki", {"query": "orders logs"}
    )
    pid = store.pinned_ids()[0]
    assert bridge_stub.knowledge_review(pid, True)["ok"]
    hits = json.loads(
        bridge_stub._run_knowledge_tool("search_wiki", {"query": "orders logs"})
    )["facts"]
    assert hits and hits[0].get("pinned") is True
    out = bridge_stub._run_knowledge_tool(
        "pin_wiki_fact", {"fact_id": hits[0]["id"], "pinned": False}
    )
    assert out.startswith("Unpinned")
    assert bridge_stub._knowledge_header() == ""
    # pinning is autonomous; the Knowledge tab can pin too
    assert bridge_stub._effective_tool_autonomy("pin_wiki_fact", {}) == "autonomous"
    assert bridge_stub.knowledge_pin(hits[0]["id"], True)["ok"] is True
    assert bridge_stub.knowledge_overview()["pinned"][0]["id"] == hits[0]["id"]


# ── the shipped seed ─────────────────────────────────────────────────────────


def test_seed_loads_and_names_every_builtin_tool(kn):
    from runspec_console.bridge_const import _BUILTIN_META_NAMES, _BUILTIN_TOOL_NAMES

    facts = kn.seed_facts()
    assert len(facts) >= 30
    assert all(f["status"] == "approved" and f["source"] == "seed" for f in facts)
    assert len({f["id"] for f in facts}) == len(facts)
    text = " ".join(f["text"] for f in facts)
    # A built-in tool added without a seed mention fails here: name it in
    # knowledge_seed.toml so the wiki (and an MCP client) can find it.
    missing = sorted(
        n
        for n in _BUILTIN_TOOL_NAMES
        | _BUILTIN_META_NAMES
        | {"search_runnables", "describe_runnable", "run_runnable"}
        if n not in text
    )
    assert missing == []
    assert any("add-runnable" in f["text"] for f in facts)


def test_seed_is_searchable_overridable_and_never_pushed(kn, tmp_path):
    store = kn.KnowledgeStore(tmp_path / "knowledge")
    idx = store.index()
    try:
        hits = idx.search("add a new runnable")
        assert hits and hits[0]["source"] == "seed"
        assert idx.page("feature:knowledge")["facts"]
    finally:
        idx.close()
    seed = store.seed_facts()[0]
    assert store.retire(seed["id"])  # an own-lane copy overrides the shipped one
    by_id = {f["id"]: f for f in store.all_facts()}
    assert by_id[seed["id"]]["status"] == "retired"
    assert store.set_pinned(store.seed_facts()[1]["id"], True)[0]
    data, count = kn.contribution(store.own_facts())
    assert count == 0  # a seed fact is never pushed by Memory Sync


def test_render_turns_carries_redacted_output_excerpts(kn):
    text = kn.render_turns(
        [
            {
                "user": "fetch the orders log",
                "tools": [
                    {
                        "name": "list_files",
                        "input": {"path": "/var/log"},
                        "output": "orders/app.log\nother.log " + "x" * 600,
                    },
                    {
                        "name": "relay_file",
                        "input": {"path": "/var/log/orders/app.log"},
                        "output": "copied; token=abcd1234efgh",
                    },
                ],
                "assistant": "Fetched it.",
            }
        ]
    )
    assert "RESULT: orders/app.log other.log" in text
    assert "x" * 301 not in text and "…" in text  # capped
    assert "abcd1234efgh" not in text and "«redacted»" in text


def test_notes_for_runnable(store, kn):
    store.add_approved(
        _fact(
            kn,
            "Use the space overview page as the entry point for team docs.",
            about=["runnable:confluence"],
            kind="note",
            status="approved",
        )
    )
    store.write_observed(
        [
            _fact(
                kn,
                "confluence is usually run with --space OPS.",
                about=["runnable:confluence"],
                kind="arg-habit",
                status="observed",
            )
        ]
    )
    store.propose(_fact(kn, "unreviewed tip", about=["runnable:confluence"]))
    store.add_approved(
        _fact(kn, "x" * 500, about=["runnable:ping"], kind="note", status="approved")
    )
    idx = store.index()
    try:
        notes = idx.notes_for(["runnable:confluence", "runnable:ping", "runnable:none"])
        assert notes["runnable:confluence"] == [
            "Use the space overview page as the entry point for team docs.",
            "confluence is usually run with --space OPS.",
        ]  # approved before observed; pending left out
        assert len(notes["runnable:ping"][0]) == kn.NOTE_MAX_CHARS
        assert "runnable:none" not in notes
        assert (
            len(
                idx.notes_for(["runnable:confluence"], per_entity=1)[
                    "runnable:confluence"
                ]
            )
            == 1
        )
    finally:
        idx.close()


def test_bridge_wiki_notes_by_runnable_name(kn, tmp_path, monkeypatch):
    from runspec_console import bridge_knowledge as bk
    from runspec_console import config as config_mod

    monkeypatch.setattr(config_mod, "knowledge_dir", lambda: tmp_path / "kd")
    st = kn.KnowledgeStore(tmp_path / "kd", seed=False)
    st.add_approved(
        _fact(
            kn,
            "keep macros",
            about=["runnable:confluence"],
            kind="note",
            status="approved",
        )
    )

    class B(bk.KnowledgeMixin):
        pass

    b = B()
    b._memory_migration_checked = True
    assert b._wiki_notes(["confluence", "ping"]) == {"confluence": ["keep macros"]}
    assert b._wiki_notes([]) == {}
