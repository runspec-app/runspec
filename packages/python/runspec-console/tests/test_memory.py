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

    # Autonomy: local + reversible → run without prompting.
    for name in ("remember", "search_memory"):
        assert b._effective_tool_autonomy(name, {}) == "autonomous"
    # save_runbook is the exception: a runbook is a procedure the agent (and every
    # console it syncs to) will later follow, so the operator approves each save.
    # It must stay *in* _MEMORY_TOOL_NAMES though — that frozenset is also the
    # dispatch key and the search-mode hot-kernel key.
    assert b._effective_tool_autonomy("save_runbook", {}) == "confirm"
    from runspec_console.bridge_const import _HOT_BUILTIN_NAMES, _MEMORY_TOOL_NAMES

    assert "save_runbook" in _MEMORY_TOOL_NAMES
    assert "save_runbook" in _HOT_BUILTIN_NAMES

    # Registration: exactly the three memory tools — reading a runbook lives in
    # run_runbook, so no load_runbook here however many runbooks are saved.
    assert {t["name"] for t in b._memory_tools()} == {
        "remember",
        "search_memory",
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
    assert "load_runbook" not in {t["name"] for t in b._memory_tools()}
    # Saving a runbook notifies the UI so the `$` menu / Forms / Runbooks editor
    # refresh live (no restart needed) — mirrors the Settings editor's dispatch.
    assert any(ev == "runspec:runbooks_changed" for ev, _ in b.dispatched)
    out = b._run_memory_tool("search_memory", {"query": "log"})
    assert "/var/log/app.log" in out


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
