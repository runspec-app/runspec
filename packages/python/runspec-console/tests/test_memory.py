"""Unit tests for the persistent memory + runbook stores (pure, temp-dir backed)."""

import json
import threading

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


def test_pinned_count(mem):
    assert mem.pinned_count() == 0
    mem.add_memory("unpinned", app="x")
    mem.add_memory("pin a", app="x", pin=True)
    mem.add_memory("pin b", app="x", pin=True)
    # Counts every pinned fact, including ones beyond the header display cap.
    assert mem.pinned_count() == 2


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


def test_list_memories_returns_all_with_fields(mem):
    mem.add_memory("first fact", app="a", tags=["t1"])
    mem.add_memory("second fact", app="b", pin=True)
    rows = mem.list_memories()
    assert len(rows) == 2
    # Newest first, and every prune-relevant field is present.
    assert rows[0]["fact"] == "second fact"
    assert rows[0]["pinned"] is True
    assert {"id", "app", "fact", "tags", "pinned", "hits", "created"} <= set(rows[0])


def test_list_memories_scoped_by_app(mem):
    mem.add_memory("a fact", app="a")
    mem.add_memory("b fact", app="b")
    rows = mem.list_memories(app="b")
    assert [r["app"] for r in rows] == ["b"]


def test_list_memories_does_not_increment_hits(mem):
    mem.add_memory("a fact", app="a")
    mem.list_memories()
    mem.list_memories(app="a")
    # Pure read — unlike search_memory, listing never inflates recall stats.
    assert mem.read_memory()[0]["hits"] == 0


def test_forget_memory_removes_only_that_entry(mem):
    mid = mem.add_memory("goner", app="a")
    mem.add_memory("keeper", app="a")
    assert mem.forget_memory(mid) is True
    facts = [r["fact"] for r in mem.read_memory()]
    assert facts == ["keeper"]
    # Deleting a missing / blank id is a no-op that reports False.
    assert mem.forget_memory(mid) is False
    assert mem.forget_memory("") is False


def test_forget_a_pinned_memory_frees_a_header_slot(mem):
    """The prune the operator asked for: dropping a pinned fact lowers the pin count
    that gates the header (only HEADER_MAX_FACTS pins fold into the prompt)."""
    ids = [mem.add_memory(f"pin {i}", app="x", pin=True) for i in range(3)]
    assert mem.pinned_count() == 3
    assert mem.forget_memory(ids[0]) is True
    assert mem.pinned_count() == 2


def test_runbook_roundtrip(mem):
    sid = mem.save_runbook(
        "",
        title="Stale cache triage",
        symptom="users see old data",
        steps="1. summarize-log\n2. restart-service cache",
        tags=["cache"],
    )
    assert sid == "stale-cache-triage"
    listed = mem.list_runbooks()
    assert listed == [
        {"id": sid, "title": "Stale cache triage", "symptom": "users see old data"}
    ]
    full = mem.load_runbook(sid)
    assert "restart-service" in full["steps"]


def test_runbook_replace_by_id(mem):
    mem.save_runbook("s1", title="v1", steps="a")
    mem.save_runbook("s1", title="v2", steps="b")
    runbooks = mem.load_runbooks()
    assert len(runbooks) == 1 and runbooks[0]["title"] == "v2"


def test_save_runbook_entry_preserves_other_entries_and_discoverable(mem):
    """The editor's single-entry save must not disturb other runbooks — including ones
    changed on disk since the console loaded — nor drop their publish audience. This is
    the data-loss fix: the old whole-collection bulk save rebuilt every other runbook
    from a stale snapshot and omitted `discoverable`."""
    from runspec_console.bridge_invoke import InvokeMixin

    class _Stub(InvokeMixin):
        pass

    bridge = _Stub()
    mem.save_runbook("a", title="A", steps=[{"text": "aa"}], discoverable=["operator"])
    mem.save_runbook("b", title="B", steps=[{"text": "bb"}])
    # 'a' is edited on disk in a way the console never saw (external editor / Config Sync).
    rows = mem.load_runbooks()
    for r in rows:
        if r["id"] == "a":
            r["title"] = "A edited externally"
    mem.save_runbooks(rows)

    # The editor saves only 'b' (its payload carries no 'discoverable').
    res = bridge.save_runbook_entry(
        {"id": "b", "title": "B2", "steps": [{"text": "bb2"}]}
    )
    assert res["ok"] and res["id"] == "b"
    a = mem.load_runbook("a")
    assert a["title"] == "A edited externally"  # external edit survives
    assert a["discoverable"] == ["operator"]  # audience not dropped
    assert mem.load_runbook("b")["title"] == "B2"

    # delete_runbook removes only its target.
    assert bridge.delete_runbook("a") == {"ok": True, "removed": True}
    assert [r["id"] for r in mem.load_runbooks()] == ["b"]


def test_save_runbook_entry_normalises_type_aliases(mem):
    """A JSON type spelling from an editor round-trip is normalised before validation."""
    from runspec_console.bridge_invoke import InvokeMixin

    class _Stub(InvokeMixin):
        pass

    res = _Stub().save_runbook_entry(
        {
            "id": "r",
            "title": "R",
            "steps": [{"text": "go {{n}}"}],
            "argsRaw": {"n": {"type": "integer"}},
        }
    )
    assert res["ok"]
    assert mem.load_runbook("r")["args"]["n"]["type"] == "int"


def test_multiline_step_text_serialises_as_triple_quoted(mem):
    """A step `text` with newlines must land as a readable triple-quoted TOML literal,
    not one escaped line — so runbooks are editable in an external editor / git."""
    mem.save_runbook(
        "ml", title="ML", steps=[{"text": "line one\nline two\nline three"}]
    )
    raw = mem.runbooks_path().read_text(encoding="utf-8")
    assert '"""' in raw
    assert "\\n" not in raw  # not the escaped single-line form
    # And it round-trips back to the same text on read.
    assert mem.load_runbook("ml")["step"][0]["text"] == "line one\nline two\nline three"


def test_delete_runbook_removes_only_that_entry(mem):
    mem.save_runbook("a", title="A", steps="x")
    mem.save_runbook("b", title="B", steps="y")
    assert mem.delete_runbook("a") is True
    assert [r["id"] for r in mem.load_runbooks()] == ["b"]
    # Deleting a missing id is a no-op that reports False.
    assert mem.delete_runbook("nope") is False
    assert mem.delete_runbook("") is False


def test_runbook_args_roundtrip(mem):
    """A runbook's [runbook.args] table survives save → load verbatim."""
    args = {
        "app": {"type": "str", "required": True},
        "filter": {"options": ["a", "b"], "default": "a"},
    }
    sid = mem.save_runbook(
        "fetch-log", title="Fetch log", steps="grab {{app}} via {{filter}}", args=args
    )
    full = mem.load_runbook(sid)
    assert full["args"] == args


def test_runbook_without_args_has_no_args_key(mem):
    """Param-less runbooks stay clean — no empty `args` table is written."""
    sid = mem.save_runbook("plain", title="Plain", steps="do a thing")
    assert "args" not in mem.load_runbook(sid)


def test_render_runbook_body(mem):
    body = "get {{app}} on {{host}} x{{count}} on={{flag}} miss={{nope}}"
    out = mem.render_runbook_body(
        body, {"app": "web", "host": "h1", "count": 5, "flag": True}
    )
    # Known placeholders substituted (bool → true/false, int → str); unknown left intact.
    assert out == "get web on h1 x5 on=true miss={{nope}}"


def test_render_runbook_structured_array(mem):
    """A [[runbook.step]] array renders to numbered prose with {{placeholders}} filled;
    a title becomes an inline `N. Title: text` prefix."""
    entry = {
        "id": "x",
        "step": [
            {"text": "probe {{host}}"},
            {"title": "Restart", "text": "restart {{host}}"},
        ],
    }
    out = mem.render_runbook(entry, {"host": "web-01"})
    assert out == "1. probe web-01\n2. Restart: restart web-01"


def test_render_runbook_legacy_string_is_byte_identical(mem):
    """The legacy `steps` string path must render exactly as render_runbook_body did —
    no renumbering, no reshaping."""
    entry = {"id": "x", "steps": "1. do a on {{host}}\n2. do b"}
    vals = {"host": "h1"}
    assert mem.render_runbook(entry, vals) == mem.render_runbook_body(
        entry["steps"], vals
    )


def test_render_runbook_prefers_array_over_legacy_string(mem):
    """When both keys somehow exist, the structured array wins."""
    entry = {"id": "x", "step": [{"text": "new"}], "steps": "old"}
    assert mem.render_runbook(entry, {}) == "1. new"


def test_normalize_runbook_steps_splits_and_falls_back(mem):
    # Structured array → cleaned rows verbatim.
    assert mem.normalize_runbook_steps(
        {"step": [{"text": "a"}, {"title": "T", "text": "b"}]}
    ) == [{"text": "a"}, {"text": "b", "title": "T"}]
    # Legacy numbered string → one row per numbered step.
    assert mem.normalize_runbook_steps({"steps": "1. a\n2. b"}) == [
        {"text": "a"},
        {"text": "b"},
    ]
    # Unnumbered string → a single row (never one row per line).
    assert mem.normalize_runbook_steps({"steps": "blob\nmore"}) == [
        {"text": "blob\nmore"}
    ]
    # Empty → no rows.
    assert mem.normalize_runbook_steps({}) == []


def test_save_runbook_list_writes_step_array(mem):
    """Saving a list of rows persists a [[runbook.step]] array and drops the legacy
    `steps` string key."""
    sid = mem.save_runbook(
        "web-triage",
        title="Web triage",
        steps=[{"text": "probe"}, {"title": "Fix", "text": "restart"}, {"text": "  "}],
    )
    full = mem.load_runbook(sid)
    assert "steps" not in full
    # The blank row is dropped; title kept only where present.
    assert full["step"] == [
        {"text": "probe"},
        {"text": "restart", "title": "Fix"},
    ]


def test_save_runbook_string_keeps_legacy_key(mem):
    """A string save stays a legacy `steps` string (no lossy auto-split)."""
    sid = mem.save_runbook("legacy", title="Legacy", steps="1. a\n2. b")
    full = mem.load_runbook(sid)
    assert full["steps"] == "1. a\n2. b"
    assert "step" not in full


def test_structured_steps_survive_config_sync_merge(mem):
    """A runbook's nested [[runbook.step]] array rides the by-id merge unchanged."""
    from runspec_console import configmerge

    incoming = [{"id": "team", "title": "Team", "step": [{"text": "do a"}]}]
    merged = {e["id"]: e for e in configmerge.merge_collection([], incoming, "id", [])}
    assert merged["team"]["step"] == [{"text": "do a"}]


def test_runbook_args_survive_config_sync_merge(mem):
    """Runbooks merge by id through configmerge — the nested args table travels with
    the entry (adding from the repo, keeping the operator's own runbooks)."""
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
    """The bridge surfaces the memory tools and runs the note/recall ones without a
    confirm gate. save_runbook is the exception — see below. Reading a runbook is
    not a memory tool at all: it is run_runbook (see test_action_tools.py)."""
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

    # Autonomy: local + reversible → run without prompting. list_memories is a pure
    # read (no query, no hit bump), so it's autonomous too.
    for name in ("remember", "search_memory", "list_memories"):
        assert b._effective_tool_autonomy(name, {}) == "autonomous"
    # save_runbook is the exception: a runbook is a procedure the agent (and every
    # console it syncs to) will later follow, so the operator approves each save.
    # forget_memory is the other: it deletes a stored fact, so the operator agrees to
    # each prune. Both must stay *in* _MEMORY_TOOL_NAMES though — that frozenset is
    # also the dispatch key and the search-mode hot-kernel key.
    assert b._effective_tool_autonomy("save_runbook", {}) == "confirm"
    assert b._effective_tool_autonomy("forget_memory", {}) == "confirm"
    from runspec_console.bridge_const import _HOT_BUILTIN_NAMES, _MEMORY_TOOL_NAMES

    for n in ("save_runbook", "forget_memory", "list_memories"):
        assert n in _MEMORY_TOOL_NAMES
        assert n in _HOT_BUILTIN_NAMES

    # Registration: the five always-on memory tools — load_runbook only appears
    # once a runbook exists to read (its runbook_id enum would otherwise be empty).
    assert {t["name"] for t in b._memory_tools()} == {
        "remember",
        "search_memory",
        "list_memories",
        "forget_memory",
        "save_runbook",
    }

    # Routing: remember → search round-trips; saving a runbook dispatches the
    # UI-refresh event.
    b._run_memory_tool(
        "remember", {"fact": "the log is at /var/log/app.log", "app": "a"}
    )
    b._run_memory_tool(
        "save_runbook", {"title": "Restart flow", "steps": "1. restart-service"}
    )
    # Now that a runbook exists, load_runbook is offered (the chat-only reader).
    assert "load_runbook" in {t["name"] for t in b._memory_tools()}
    # Saving a runbook notifies the UI so the `$` menu / Forms / Runbooks editor
    # refresh live (no restart needed) — mirrors the Settings editor's dispatch.
    assert any(ev == "runspec:runbooks_changed" for ev, _ in b.dispatched)
    out = b._run_memory_tool("search_memory", {"query": "log"})
    assert "/var/log/app.log" in out


def test_bridge_list_and_forget_memory_roundtrip(tmp_path, monkeypatch):
    """The agent's prune flow: list_memories enumerates everything (id included), then
    forget_memory deletes one by that id — and a bad id reports rather than raises."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from runspec_console.bridge import Bridge

    class _Stub:
        _run_memory_tool = Bridge._run_memory_tool

        def _dispatch(self, ev, payload):
            pass

    b = _Stub()
    b._run_memory_tool(
        "remember", {"fact": "OrderSvc log at /var/log/o.log", "app": "o"}
    )
    b._run_memory_tool(
        "remember", {"fact": "Payments token rotates monthly", "app": "p"}
    )

    listed = json.loads(b._run_memory_tool("list_memories", {}))
    assert listed["count"] == 2
    ids = {m["id"] for m in listed["memories"]}
    # Scoped list narrows to one app.
    scoped = json.loads(b._run_memory_tool("list_memories", {"app": "o"}))
    assert scoped["count"] == 1 and scoped["memories"][0]["app"] == "o"

    target = scoped["memories"][0]["id"]
    assert b._run_memory_tool("forget_memory", {"memory_id": target}) == (
        f"Forgot memory {target}."
    )
    after = json.loads(b._run_memory_tool("list_memories", {}))
    assert after["count"] == 1 and target not in {m["id"] for m in after["memories"]}
    assert ids  # (silence unused in case the set assertion above changes)

    # A blank / unknown id reports an error string instead of raising.
    assert b._run_memory_tool("forget_memory", {}).startswith("Error:")
    assert "already gone" in b._run_memory_tool("forget_memory", {"memory_id": "m_zzz"})

    # Empty store lists cleanly (no crash, human-readable).
    b._run_memory_tool("forget_memory", {"memory_id": after["memories"][0]["id"]})
    assert "No memories stored" in b._run_memory_tool("list_memories", {})


def _runbook_tool_stub(tmp_path, monkeypatch):
    """A minimal bridge stub exposing just the runbook agent-tool surface."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from runspec_console.bridge import Bridge

    class _Stub:
        _memory_tools = Bridge._memory_tools
        _run_memory_tool = Bridge._run_memory_tool
        _action_tools = Bridge._action_tools

        def __init__(self):
            self.dispatched: list[tuple[str, object]] = []
            # _action_tools reads the fleet to shape its host/runnable enums.
            self._hosts: list = []
            self._runnables_cache: list = []
            self._filters: list = []
            self._credentials: list = []
            self._lock = threading.Lock()

        def _reload_credentials(self):
            pass

        def _dispatch(self, ev, payload):
            self.dispatched.append((ev, payload))

    return _Stub()


def test_agent_edit_preserves_args_and_discoverable(tmp_path, monkeypatch):
    """Editing a runbook through the agent must not silently drop its parameters or
    its publish audience.

    ``memory.save_runbook`` *replaces* the entry, so a field the handler omits is
    deleted. The agent's schema carries neither ``args`` nor ``discoverable``, so
    every agent edit used to wipe both — the runbook stayed, its ``[runbook.args]``
    table and catalog visibility did not."""
    b = _runbook_tool_stub(tmp_path, monkeypatch)
    from runspec_console import memory

    memory.save_runbook(
        "restart-web",
        title="Restart web",
        symptom="web tier wedged",
        steps="1. restart {{host}}",
        args={"host": {"type": "choice", "options": ["web-01", "web-02"]}},
        discoverable=["operator"],
    )

    # The agent rewrites only the steps, as it would after a correction.
    b._run_memory_tool(
        "save_runbook",
        {"id": "restart-web", "title": "Restart web", "steps": "1. drain\n2. restart"},
    )

    after = memory.load_runbook("restart-web")
    assert after["steps"].startswith("1. drain")
    assert after["args"] == {
        "host": {"type": "choice", "options": ["web-01", "web-02"]}
    }
    assert after["discoverable"] == ["operator"]


def test_agent_can_author_and_replace_runbook_args(tmp_path, monkeypatch):
    """`args` is in the agent's schema, validated before write, and an explicitly
    supplied table replaces the old one (only *omission* preserves)."""
    b = _runbook_tool_stub(tmp_path, monkeypatch)
    from runspec_console import memory

    schema = next(t for t in b._memory_tools() if t["name"] == "save_runbook")
    assert "args" in schema["input_schema"]["properties"]
    assert "discoverable" not in schema["input_schema"]["properties"]

    b._run_memory_tool(
        "save_runbook",
        {
            "title": "Fetch logs",
            "steps": "1. download {{path}}",
            "args": {"path": {"type": "str", "description": "Remote log path"}},
        },
    )
    assert memory.load_runbook("fetch-logs")["args"] == {
        "path": {"type": "str", "description": "Remote log path"}
    }

    b._run_memory_tool(
        "save_runbook",
        {
            "id": "fetch-logs",
            "title": "Fetch logs",
            "steps": "1. download {{path}}",
            "args": {"path": {"type": "path"}},
        },
    )
    assert memory.load_runbook("fetch-logs")["args"] == {"path": {"type": "path"}}


def test_agent_can_author_structured_step_array(tmp_path, monkeypatch):
    """The agent's save_runbook accepts the new [[runbook.step]] array form and
    persists it as an array (not a legacy string) — proving the model can write the
    readable form its schema tells it to prefer."""
    b = _runbook_tool_stub(tmp_path, monkeypatch)
    from runspec_console import memory

    # The schema offers steps as string-or-array of {text, title?}.
    schema = next(t for t in b._memory_tools() if t["name"] == "save_runbook")
    steps_schema = schema["input_schema"]["properties"]["steps"]
    assert "anyOf" in steps_schema
    array_variant = next(v for v in steps_schema["anyOf"] if v["type"] == "array")
    assert array_variant["items"]["required"] == ["text"]

    out = b._run_memory_tool(
        "save_runbook",
        {
            "title": "Web triage",
            "steps": [
                {"text": "Probe web-01__http-check."},
                {"title": "Restart", "text": "Run {{host}}__restart-service."},
            ],
        },
    )
    assert out.startswith("Saved runbook")
    saved = memory.load_runbook("web-triage")
    # Persisted as the structured array, no legacy string key.
    assert "steps" not in saved
    assert saved["step"] == [
        {"text": "Probe web-01__http-check."},
        {"text": "Run {{host}}__restart-service.", "title": "Restart"},
    ]
    # And it renders to the numbered prose the agent would follow.
    assert memory.render_runbook(saved, {"host": "web-01"}) == (
        "1. Probe web-01__http-check.\n2. Restart: Run web-01__restart-service."
    )


def test_agent_steps_omitted_edit_preserves_step_array(tmp_path, monkeypatch):
    """Editing another field (steps omitted) keeps the structured step array — the
    same carry-forward that protects args/discoverable."""
    b = _runbook_tool_stub(tmp_path, monkeypatch)
    from runspec_console import memory

    memory.save_runbook(
        "web-triage",
        title="Web triage",
        steps=[{"text": "probe"}, {"title": "Fix", "text": "restart"}],
    )
    # Agent edits only the symptom; it omits steps.
    b._run_memory_tool(
        "save_runbook",
        {"id": "web-triage", "title": "Web triage", "symptom": "502s after deploy"},
    )
    after = memory.load_runbook("web-triage")
    assert after["symptom"] == "502s after deploy"
    assert after["step"] == [{"text": "probe"}, {"text": "restart", "title": "Fix"}]
    assert "steps" not in after


def test_agent_rejects_step_with_machine_control_key(tmp_path, monkeypatch):
    """The cliff guard reaches the agent path: a step carrying a condition/goto-style
    key bounces with a readable reason and writes nothing."""
    b = _runbook_tool_stub(tmp_path, monkeypatch)
    from runspec_console import memory

    out = b._run_memory_tool(
        "save_runbook",
        {"title": "Bad", "steps": [{"text": "go", "condition": "exit != 0"}]},
    )
    assert out.startswith("Error: invalid runbook steps")
    assert memory.load_runbook("bad") is None


def test_agent_runbook_args_validation_errors_are_returned(tmp_path, monkeypatch):
    """A bad args table bounces with a readable reason and writes nothing, so the
    model can correct itself in-turn rather than persisting a broken runbook."""
    b = _runbook_tool_stub(tmp_path, monkeypatch)
    from runspec_console import memory

    out = b._run_memory_tool(
        "save_runbook",
        {"title": "Bad", "steps": "1. go", "args": {"n": {"type": "nonesuch"}}},
    )
    assert out.startswith("Error: invalid runbook args")
    assert memory.load_runbook("bad") is None

    out = b._run_memory_tool(
        "save_runbook", {"title": "Bad", "steps": "1. go", "args": "not-a-table"}
    )
    assert out.startswith("Error: 'args' must be a table")
    assert memory.load_runbook("bad") is None


def test_runbook_menu_carries_the_symptom(tmp_path, monkeypatch):
    """The embedded menu names the symptom, not just the title — the symptom is what
    lets the agent pick a runbook without a round-trip per candidate."""
    b = _runbook_tool_stub(tmp_path, monkeypatch)
    from runspec_console import memory

    memory.save_runbook(
        "web-502",
        title="Triage 502s",
        symptom="web nodes returning 502s",
        steps="1. probe",
    )

    run = next(t for t in b._action_tools() if t["name"] == "run_runbook")
    assert "Triage 502s" in run["description"]
    assert "web nodes returning 502s" in run["description"]


def test_runbook_tool_schemas_are_byte_stable_across_a_steps_only_edit(
    tmp_path, monkeypatch
):
    """Editing a runbook's steps must not change the agent's tool schemas at all.

    This is a prompt-cache invariant, not tidiness. The schemas sit before the cache
    breakpoint, so any byte change invalidates the whole cached tools+system prefix
    for the rest of the session — and ``save_runbook`` replaces an entry by
    delete-then-append, which moved the edited runbook to the end of the file and
    therefore to the end of every enum and menu. A steps-only edit — the most common
    kind, and one the agent is now nudged to propose — used to bust the cache while
    changing nothing the model can see. ``list_runbooks`` sorts by id to prevent it.

    Asserted over the *whole* tool list rather than just the enum, so the next thing
    that leaks a mutable value into a schema fails here too."""
    b = _runbook_tool_stub(tmp_path, monkeypatch)
    from runspec_console import memory

    for rid in ("zebra", "alpha", "middle"):
        memory.save_runbook(rid, title=f"T {rid}", symptom=f"S {rid}", steps="v1")

    def snapshot():
        tools = b._memory_tools() + b._action_tools()
        return json.dumps(tools, sort_keys=True)

    before = snapshot()
    memory.save_runbook("alpha", title="T alpha", symptom="S alpha", steps="v2 rewrite")
    assert snapshot() == before

    # Sanity: the ids really are sorted, not merely unchanged.
    run = next(t for t in b._action_tools() if t["name"] == "run_runbook")
    assert run["input_schema"]["properties"]["runbook_id"]["enum"] == [
        "alpha",
        "middle",
        "zebra",
    ]
    # ...and adding one *does* change the schemas — the cache invalidation we accept.
    memory.save_runbook("beta", title="T beta", symptom="S beta", steps="v1")
    assert snapshot() != before


def test_runbook_menu_is_capped(tmp_path, monkeypatch):
    """The menu lives in the cached tools block, so it must not grow without bound
    as runbooks accumulate. Past the cap the agent is pointed at search_runnables,
    which ranks on the same symptom text and has no cap pressure."""
    from runspec_console.bridge_agent_tools import _RUNBOOK_MENU_CAP, _runbook_menu

    rows = [
        {"id": f"rb-{i:03d}", "title": f"T{i}", "symptom": f"S{i}"}
        for i in range(_RUNBOOK_MENU_CAP + 7)
    ]
    menu = _runbook_menu(rows)
    assert menu.count("'rb-") == _RUNBOOK_MENU_CAP
    assert "…and 7 more — use search_runnables to find them" in menu
    # Under the cap: everything listed, no overflow note.
    assert "…and" not in _runbook_menu(rows[:3])


def test_bridge_get_save_runbooks_roundtrip(tmp_path, monkeypatch):
    """save_runbooks persists the collection (validating args); get_runbooks returns each
    runbook with its args parsed to the ArgDef shape the UI consumes."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from runspec_console.bridge import Bridge

    class _Stub:
        get_runbooks = Bridge.get_runbooks
        save_runbooks = Bridge.save_runbooks

    b = _Stub()
    res = b.save_runbooks(
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

    runbooks = b.get_runbooks()
    assert len(runbooks) == 1
    s = runbooks[0]
    assert s["id"] == "fetch-log" and s["argsRaw"]["app"]["required"] is True
    # args is the parsed ArgDef[] (same shape the Forms tab renders).
    assert s["args"] == [{"name": "app", "type": "str", "required": True}]


def test_bridge_save_runbooks_rejects_bad_args(tmp_path, monkeypatch):
    """A malformed args table (choice without options) is rejected before it lands."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from runspec_console.bridge import Bridge

    class _Stub:
        save_runbooks = Bridge.save_runbooks

    res = _Stub().save_runbooks(
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


def _run_runbook_stub(tmp_path, monkeypatch):
    """A stub exposing the runbook *action* tool (run_runbook) surface."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from runspec_console.bridge import Bridge

    class _Stub:
        _action_run_runbook = Bridge._action_run_runbook

        def _tag_run_origin_runbook(self, chat_id, rid):
            pass

    return _Stub()


def test_run_runbook_reads_without_required_values(tmp_path, monkeypatch):
    """The agent can consult a runbook that has a required-by-inference arg (no
    default) *without* supplying it — the steps come back with {{placeholders}} intact,
    the parameters are described, and the unfilled required ones are flagged."""
    b = _run_runbook_stub(tmp_path, monkeypatch)
    from runspec_console import memory

    memory.save_runbook(
        "web-triage",
        title="Web triage",
        steps=[{"text": "probe {{host}}__http-check"}],
        args={"host": {"type": "choice", "options": ["web-01", "web-02"]}},
    )
    out = b._action_run_runbook({"runbook_id": "web-triage"})
    assert not out.startswith("Error")  # no "Missing required argument"
    assert "{{host}}" in out  # unfilled placeholder left visible
    assert "Parameters: host (choice" in out
    assert "Unfilled: host" in out


def test_run_runbook_fills_and_omits_unfilled_note_when_complete(tmp_path, monkeypatch):
    b = _run_runbook_stub(tmp_path, monkeypatch)
    from runspec_console import memory

    memory.save_runbook(
        "web-triage",
        title="Web triage",
        steps=[{"text": "probe {{host}}__http-check"}],
        args={"host": {"type": "choice", "options": ["web-01", "web-02"]}},
    )
    out = b._action_run_runbook(
        {"runbook_id": "web-triage", "values": {"host": "web-01"}}
    )
    assert "probe web-01__http-check" in out
    assert "Unfilled" not in out


def test_run_runbook_still_rejects_a_bad_supplied_value(tmp_path, monkeypatch):
    b = _run_runbook_stub(tmp_path, monkeypatch)
    from runspec_console import memory

    memory.save_runbook(
        "web-triage",
        title="Web triage",
        steps=[{"text": "probe {{host}}"}],
        args={"host": {"type": "choice", "options": ["web-01", "web-02"]}},
    )
    out = b._action_run_runbook(
        {"runbook_id": "web-triage", "values": {"host": "nope"}}
    )
    assert out.startswith("Error")


def test_agent_save_runbook_accepts_json_type_spelling(tmp_path, monkeypatch):
    """`type = "string"` (and other JSON spellings) are accepted and stored as the
    runspec type name, instead of bouncing as an unknown type."""
    b = _runbook_tool_stub(tmp_path, monkeypatch)
    from runspec_console import memory

    out = b._run_memory_tool(
        "save_runbook",
        {
            "title": "Fetch logs",
            "steps": "1. download {{path}}",
            "args": {"path": {"type": "string", "description": "Remote path"}},
        },
    )
    assert out.startswith("Saved runbook")
    assert memory.load_runbook("fetch-logs")["args"]["path"]["type"] == "str"


def test_load_runbook_returns_raw_definition(tmp_path, monkeypatch):
    """load_runbook returns the editable source — raw step array (placeholders
    intact) + parsed args — not the rendered 'follow now' body run_runbook gives."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    import json as _json
    import threading as _threading
    from runspec_console.bridge import Bridge
    from runspec_console import memory

    memory.save_runbook(
        "web-triage",
        title="Web triage",
        symptom="502s",
        tags=["web"],
        steps=[
            {"text": "probe {{host}}__http-check"},
            {"title": "Fix", "text": "restart {{host}}"},
        ],
        args={"host": {"type": "choice", "options": ["web-01", "web-02"]}},
    )

    class _Stub:
        _run_load_runbook = Bridge._run_load_runbook

        def __init__(self):
            self._run_origin = {}
            self._lock = _threading.Lock()

    b = _Stub()
    out = _json.loads(b._run_load_runbook({"runbook_id": "web-triage"}, "chat"))
    rb = out["runbook"]
    assert rb["id"] == "web-triage" and rb["symptom"] == "502s"
    # Raw steps, placeholders intact (not rendered/numbered).
    assert rb["steps"] == [
        {"text": "probe {{host}}__http-check"},
        {"text": "restart {{host}}", "title": "Fix"},
    ]
    # Args parsed to the editable shape (type/options/required).
    assert rb["args"][0]["name"] == "host"
    assert rb["args"][0]["options"] == ["web-01", "web-02"]


def test_load_runbook_is_chat_only_origin_guard(tmp_path, monkeypatch):
    """Layer 2 of the chat-only guarantee: a trigger-originated turn is refused."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    import threading as _threading
    from runspec_console.bridge import Bridge
    from runspec_console import memory

    memory.save_runbook("web-triage", title="Web triage", steps="1. probe")

    class _Stub:
        _run_load_runbook = Bridge._run_load_runbook

        def __init__(self):
            self._run_origin = {"t1": {"trigger": "nightly"}}
            self._lock = _threading.Lock()

    out = _Stub()._run_load_runbook({"runbook_id": "web-triage"}, "t1")
    assert out.startswith("✗") and "chat-only" in out


def test_load_runbook_stripped_from_headless_toolset():
    """Layer 1: _filter_headless_tools removes load_runbook regardless of action_allow,
    while run_runbook / save_runbook pass through."""
    from runspec_console.bridge import Bridge

    tools = [
        {"name": n} for n in ("load_runbook", "run_runbook", "save_runbook", "remember")
    ]
    # Even an action_allow naming load_runbook can't re-admit it.
    kept = {
        t["name"]
        for t in Bridge._filter_headless_tools(tools, ["load_runbook", "run_runbook"])
    }
    assert "load_runbook" not in kept
    assert {"run_runbook", "save_runbook", "remember"} <= kept


def test_agent_partial_update_preserves_omitted_fields(tmp_path, monkeypatch):
    """A partial edit (only the fields being changed) must not wipe the rest — the
    regression that made a steps-only edit blank a runbook's symptom and tags."""
    b = _runbook_tool_stub(tmp_path, monkeypatch)
    from runspec_console import memory

    memory.save_runbook(
        "web-triage",
        title="Web triage",
        symptom="502s after deploy",
        tags=["web", "incident"],
        steps="1. probe\n2. restart",
        args={"host": {"type": "choice", "options": ["web-01", "web-02"]}},
    )
    # Change only the steps (id + steps).
    out = b._run_memory_tool(
        "save_runbook",
        {
            "id": "web-triage",
            "steps": [{"text": "probe"}, {"text": "drain"}, {"text": "restart"}],
        },
    )
    assert out.startswith("Updated runbook")
    a = memory.load_runbook("web-triage")
    assert a["symptom"] == "502s after deploy"  # not wiped
    assert a["tags"] == ["web", "incident"]  # not wiped
    assert a["args"] == {"host": {"type": "choice", "options": ["web-01", "web-02"]}}
    assert a["step"] == [{"text": "probe"}, {"text": "drain"}, {"text": "restart"}]

    # Change only the symptom (id + symptom) → steps/tags/args preserved.
    b._run_memory_tool(
        "save_runbook", {"id": "web-triage", "symptom": "502s after a restart"}
    )
    a = memory.load_runbook("web-triage")
    assert a["symptom"] == "502s after a restart"
    assert a["step"] == [{"text": "probe"}, {"text": "drain"}, {"text": "restart"}]
    assert a["tags"] == ["web", "incident"]


def test_agent_update_can_explicitly_clear_tags(tmp_path, monkeypatch):
    """Presence-keyed merge: sending tags=[] explicitly clears them, while omitting
    tags preserves them (distinguishes 'clear' from 'leave alone')."""
    b = _runbook_tool_stub(tmp_path, monkeypatch)
    from runspec_console import memory

    memory.save_runbook("rb", title="RB", tags=["a", "b"], steps="1. x")
    b._run_memory_tool("save_runbook", {"id": "rb", "tags": []})
    assert memory.load_runbook("rb")["tags"] == []


def test_agent_create_requires_title_and_steps(tmp_path, monkeypatch):
    """Creating a new runbook still needs a title (→ id) and a body, with distinct
    messages — only omission on an *existing* runbook is a preserve."""
    b = _runbook_tool_stub(tmp_path, monkeypatch)
    from runspec_console import memory

    assert b._run_memory_tool("save_runbook", {"steps": "1. x"}).startswith(
        "Error: a new runbook needs a title"
    )
    assert b._run_memory_tool("save_runbook", {"title": "New"}).startswith(
        "Error: a new runbook needs steps"
    )
    assert memory.load_runbook("new") is None
    # A complete create works and reports "Saved".
    assert b._run_memory_tool(
        "save_runbook", {"title": "Fresh", "steps": "1. do it"}
    ).startswith("Saved runbook")
