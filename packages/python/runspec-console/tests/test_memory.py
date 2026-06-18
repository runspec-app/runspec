"""Unit tests for the persistent memory + runbook stores (pure, temp-dir backed)."""

import json

import pytest


@pytest.fixture
def mem(tmp_path, monkeypatch):
    # app_dir() keys off APPDATA — point it at a temp dir so the stores are isolated.
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from runspec_console import memory

    return memory


def test_add_and_search(mem):
    mem.add_memory("OrderSvc NPEs on null discount", app="ordersvc", tags=["npe"])
    mem.add_memory("Payments timeout points at the gateway", app="payments")

    hits = mem.search_memory("discount npe")
    assert len(hits) == 1
    assert hits[0]["app"] == "ordersvc"
    assert "discount" in hits[0]["fact"]


def test_search_scoped_by_app(mem):
    mem.add_memory("log lives at /var/log/a.log", app="a", tags=["log"])
    mem.add_memory("log lives at /var/log/b.log", app="b", tags=["log"])
    hits = mem.search_memory("log", app="b")
    assert len(hits) == 1 and hits[0]["app"] == "b"


def test_search_increments_hits(mem):
    mem.add_memory("cache flush fixes the stale read", app="x", tags=["cache"])
    mem.search_memory("cache")
    mem.search_memory("cache")
    row = mem.read_memory()[0]
    assert row["hits"] == 2


def test_build_header_empty_when_no_pins(mem):
    mem.add_memory("unpinned fact", app="x")
    assert mem.build_header() == ""


def test_build_header_lists_pinned_capped(mem):
    for i in range(10):
        mem.add_memory(f"pinned fact number {i}", app="x", pin=True)
    header = mem.build_header(max_facts=3, max_chars=700)
    assert header.startswith("## Known facts (memory)")
    # Header lists at most max_facts bullets and stays under the char cap.
    assert header.count("\n- ") <= 3
    assert len(header) <= 700


def test_evict_keeps_pinned_and_caps_app(mem):
    mem.add_memory("keep me", app="x", pin=True)
    for i in range(60):
        mem.add_memory(f"fact {i}", app="x")
    rows = [r for r in mem.read_memory() if r["app"] == "x"]
    assert len(rows) <= 50
    assert any(r["pinned"] and r["fact"] == "keep me" for r in rows)


def test_blank_fact_rejected(mem):
    assert mem.add_memory("   ") == ""
    assert mem.read_memory() == []


def test_skill_roundtrip(mem):
    sid = mem.save_skill(
        "",
        title="Stale cache triage",
        symptom="users see old data",
        steps="1. summarize-log\n2. restart-service cache",
        tags=["cache"],
    )
    assert sid == "stale-cache-triage"
    listed = mem.list_skills()
    assert listed == [
        {"id": sid, "title": "Stale cache triage", "symptom": "users see old data"}
    ]
    full = mem.load_skill(sid)
    assert "restart-service" in full["steps"]


def test_skill_replace_by_id(mem):
    mem.save_skill("s1", title="v1", steps="a")
    mem.save_skill("s1", title="v2", steps="b")
    skills = mem.load_skills()
    assert len(skills) == 1 and skills[0]["title"] == "v2"


def test_search_no_match_returns_empty(mem):
    mem.add_memory("totally unrelated", app="x")
    assert mem.search_memory("zzz qqq") == []


def test_memory_persists_as_valid_json_shape(mem):
    mem.add_memory("a fact", app="x", tags=["t"])
    hits = mem.search_memory("fact")
    # The bridge serialises search results as JSON — make sure they're JSON-safe.
    json.dumps({"memories": hits})


def test_bridge_registers_routes_and_gates_memory_tools(tmp_path, monkeypatch):
    """The bridge surfaces the four tools, runs them without a confirm gate, and
    only offers load_skill once a runbook exists."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from runspec_console.bridge import Bridge

    class _Stub:
        _memory_tools = Bridge._memory_tools
        _run_memory_tool = Bridge._run_memory_tool
        _effective_tool_autonomy = Bridge._effective_tool_autonomy

    b = _Stub()

    # Autonomy: local + reversible → run without prompting.
    for name in ("remember", "search_memory", "save_skill", "load_skill"):
        assert b._effective_tool_autonomy(name, {}) == "autonomous"

    # Registration: write tools present; load_skill omitted until a skill exists.
    names = {t["name"] for t in b._memory_tools()}
    assert {"remember", "search_memory", "save_skill"} <= names
    assert "load_skill" not in names

    # Routing: remember → search round-trips; saving a skill reveals load_skill.
    b._run_memory_tool(
        "remember", {"fact": "the log is at /var/log/app.log", "app": "a"}
    )
    b._run_memory_tool(
        "save_skill", {"title": "Restart flow", "steps": "1. restart-service"}
    )
    assert "load_skill" in {t["name"] for t in b._memory_tools()}
    out = b._run_memory_tool("search_memory", {"query": "log"})
    assert "/var/log/app.log" in out
