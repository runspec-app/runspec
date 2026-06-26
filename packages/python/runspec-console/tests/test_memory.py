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


def test_skill_args_roundtrip(mem):
    """A skill's [skill.args] table survives save → load verbatim."""
    args = {
        "app": {"type": "str", "required": True},
        "filter": {"options": ["a", "b"], "default": "a"},
    }
    sid = mem.save_skill(
        "fetch-log", title="Fetch log", steps="grab {{app}} via {{filter}}", args=args
    )
    full = mem.load_skill(sid)
    assert full["args"] == args


def test_skill_without_args_has_no_args_key(mem):
    """Param-less skills stay clean — no empty `args` table is written."""
    sid = mem.save_skill("plain", title="Plain", steps="do a thing")
    assert "args" not in mem.load_skill(sid)


def test_render_skill_body(mem):
    body = "get {{app}} on {{host}} x{{count}} on={{flag}} miss={{nope}}"
    out = mem.render_skill_body(
        body, {"app": "web", "host": "h1", "count": 5, "flag": True}
    )
    # Known placeholders substituted (bool → true/false, int → str); unknown left intact.
    assert out == "get web on h1 x5 on=true miss={{nope}}"


def test_skill_args_survive_config_sync_merge(mem):
    """Skills merge by id through configmerge — the nested args table travels with
    the entry (adding from the repo, keeping the operator's own skills)."""
    from runspec_console import configmerge

    local = [{"id": "mine", "title": "Mine", "steps": "x"}]
    incoming = [
        {"id": "team", "title": "Team", "steps": "y", "args": {"app": {"type": "str"}}}
    ]
    merged = {
        e["id"]: e for e in configmerge.merge_collection(local, incoming, "id", [])
    }
    assert set(merged) == {"mine", "team"}
    assert merged["team"]["args"] == {"app": {"type": "str"}}


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

        def __init__(self):
            self.dispatched: list[tuple[str, object]] = []

        def _dispatch(self, ev, payload):
            self.dispatched.append((ev, payload))

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
    # Saving a skill notifies the UI so the `$` menu / Forms / Skills editor
    # refresh live (no restart needed) — mirrors the Settings editor's dispatch.
    assert any(ev == "runspec:skills_changed" for ev, _ in b.dispatched)
    out = b._run_memory_tool("search_memory", {"query": "log"})
    assert "/var/log/app.log" in out


def test_bridge_get_save_skills_roundtrip(tmp_path, monkeypatch):
    """save_skills persists the collection (validating args); get_skills returns each
    skill with its args parsed to the ArgDef shape the UI consumes."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from runspec_console.bridge import Bridge

    class _Stub:
        get_skills = Bridge.get_skills
        save_skills = Bridge.save_skills

    b = _Stub()
    res = b.save_skills(
        [
            {
                "id": "fetch-log",
                "title": "Fetch log",
                "symptom": "pull a log",
                "steps": "grab {{app}}",
                "tags": ["logs"],
                "argsRaw": {"app": {"type": "str", "required": True}},
            }
        ]
    )
    assert res["ok"] and res["count"] == 1

    skills = b.get_skills()
    assert len(skills) == 1
    s = skills[0]
    assert s["id"] == "fetch-log" and s["argsRaw"]["app"]["required"] is True
    # args is the parsed ArgDef[] (same shape the Forms tab renders).
    assert s["args"] == [{"name": "app", "type": "str", "required": True}]


def test_bridge_save_skills_rejects_bad_args(tmp_path, monkeypatch):
    """A malformed args table (choice without options) is rejected before it lands."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from runspec_console.bridge import Bridge

    class _Stub:
        save_skills = Bridge.save_skills

    res = _Stub().save_skills(
        [
            {
                "id": "bad",
                "title": "Bad",
                "steps": "x",
                "argsRaw": {"k": {"type": "choice"}},
            }
        ]
    )
    assert res["ok"] is False and "bad" in res["error"]
