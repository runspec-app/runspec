"""
test_builtin_search_tier.py — deferred built-ins in search mode.

With ``[llm] tool_mode = "search"`` the chat turn's built-in tools split like
the runnables did: a hot kernel (status/history/memory/workspace) stays inline,
and the long tail leaves the tool list — discoverable via a folded one-line
menu, described on demand by ``describe_tool``, and run through ``use_tool``,
which the autonomy gate unwraps to the INNER tool before any decision so the
inner tool's own confirm rules, Console card, and audit apply unchanged.
Headless (trigger/schedule) turns keep inline built-ins (``inline_builtins``)
so ``action_allow`` narrowing keeps working, and ``_filter_headless_tools``
strips the meta-tools defensively.
"""

from __future__ import annotations

import json
import threading

from runspec_console.bridge import Bridge
from runspec_console.bridge_const import _BUILTIN_META_NAMES, _HOT_BUILTIN_NAMES


def _tool(name, desc="Do a thing. Second sentence with detail."):
    return {
        "name": name,
        "description": desc,
        "input_schema": {"type": "object", "properties": {}},
    }


HOT = [
    "console_status",
    "search_history",
    "remember",
    "search_memory",
    "workspace_ls",
]
DEFERRED = [
    "manage_denylist",
    "outlook_inbox",
    "repo_grep",
    "create_document",
    "run_python_sandboxed",
    "manage_hosts",
]
META = {"search_runnables", "run_runnable", "describe_runnable"}


class _Stub:
    # Real methods under test, bound from the Bridge mixins.
    _tool_mode = Bridge._tool_mode
    _defer_builtins_enabled = Bridge._defer_builtins_enabled
    _runnables_to_tools = Bridge._runnables_to_tools
    _visible_runnables = Bridge._visible_runnables
    _search_mode_tools = Bridge._search_mode_tools
    _deferred_builtin_tools = Bridge._deferred_builtin_tools
    _builtin_meta_tools = Bridge._builtin_meta_tools
    _run_describe_builtin_tool = Bridge._run_describe_builtin_tool
    _unwrap_use_tool = Bridge._unwrap_use_tool
    _preflight_builtin_input = staticmethod(Bridge._preflight_builtin_input)
    _build_builtin_index = Bridge._build_builtin_index
    _chat_index_text = Bridge._chat_index_text
    _build_catalog_index = Bridge._build_catalog_index
    _catalog_index_enabled = Bridge._catalog_index_enabled
    _first_sentence = staticmethod(Bridge._first_sentence)
    _catalog_system_section = staticmethod(Bridge._catalog_system_section)
    _dedup_tool_name = Bridge._dedup_tool_name
    _dedup_tool_aliases = Bridge._dedup_tool_aliases
    _sanitize_tool_name = staticmethod(Bridge._sanitize_tool_name)
    _effective_tool_autonomy = Bridge._effective_tool_autonomy

    def __init__(self, mode="search", llm_extra=None):
        self._lock = threading.Lock()
        self._runnables_cache: list = []
        self._discovery_ready = True
        self._discovery_progress = (0, 0)
        self._mode = mode
        self._llm_extra = dict(llm_extra or {})
        self.sync_calls: list = []

    def get_config(self):
        return {"llm": {"tool_mode": self._mode, **self._llm_extra}}

    def _reload_credentials(self):
        pass

    def _custom_agent_tools(self):
        return [_tool(n) for n in (*HOT, *DEFERRED)]


# ── toolset layout ────────────────────────────────────────────────────────────


def test_search_mode_defers_long_tail_keeps_hot_kernel():
    names = [t["name"] for t in _Stub()._runnables_to_tools()]
    assert set(HOT) <= set(names)
    assert not (set(DEFERRED) & set(names))
    assert "describe_tool" in names and "use_tool" in names
    assert META <= set(names)


def test_use_tool_enum_lists_exactly_the_deferred_names():
    tools = {t["name"]: t for t in _Stub()._runnables_to_tools()}
    enum = tools["use_tool"]["input_schema"]["properties"]["name"]["enum"]
    assert enum == DEFERRED
    d_enum = tools["describe_tool"]["input_schema"]["properties"]["names"]["items"][
        "enum"
    ]
    assert d_enum == DEFERRED


def test_hot_kernel_matches_const():
    # The fixture's hot names must actually be classified hot by the const.
    assert set(HOT) <= _HOT_BUILTIN_NAMES
    assert not (set(DEFERRED) & _HOT_BUILTIN_NAMES)


def test_hot_kernel_is_pinned_exactly():
    """Pin the whole set, not a subset. The fixtures above only name tools they
    invented, so a real tool moving in or out of the kernel — a per-turn context
    cost, and for run_runbook the difference between zero hops and a
    describe_tool + use_tool round-trip — would otherwise change nothing here."""
    assert _HOT_BUILTIN_NAMES == {
        "console_status",
        "search_history",
        "list_files",
        # fetch_url: read-only web read, the entry point of "make a runbook from
        # this doc" tasks — kept zero-hop so the agent reads the page directly.
        "fetch_url",
        "remember",
        "search_memory",
        # list_memories/forget_memory: the review-and-prune pair. Hot for the same
        # reason as the rest of the memory kernel — the pinned-fact header references
        # memory, so managing it shouldn't need a discovery hop.
        "list_memories",
        "forget_memory",
        "save_runbook",
        # run_runbook renders a runbook's steps to follow; load_runbook reads its
        # raw definition to inspect/edit. Both are hot: the folded index advertises
        # every runbook by symptom, so deferring the tools that act on that menu
        # meant two discovery hops for a procedure already described. load_runbook
        # is chat-only (stripped from headless turns) — that, not deferral, is what
        # keeps the old action_allow bypass from returning.
        "run_runbook",
        "load_runbook",
        "workspace_read",
        "workspace_write",
        "workspace_ls",
        "workspace_grep",
        "workspace_head",
        "workspace_tail",
        "workspace_info",
        # After-the-fact reshaping of a spilled file — read-only, hot beside the
        # rest of the workspace tools so the agent reshapes a spill in one hop
        # rather than reaching for confirm-gated run_python.
        "workspace_json_get",
        "workspace_materialize",
    }


def test_inline_builtins_keeps_everything_inline():
    names = [t["name"] for t in _Stub()._runnables_to_tools(inline_builtins=True)]
    assert set(HOT) | set(DEFERRED) <= set(names)
    assert not (_BUILTIN_META_NAMES & set(names))


def test_defer_builtins_escape_hatch():
    stub = _Stub(llm_extra={"defer_builtins": False})
    names = [t["name"] for t in stub._runnables_to_tools()]
    assert set(DEFERRED) <= set(names)
    assert not (_BUILTIN_META_NAMES & set(names))


def test_dedup_mode_unchanged():
    names = [t["name"] for t in _Stub(mode="dedup")._runnables_to_tools()]
    assert set(HOT) | set(DEFERRED) <= set(names)
    assert not (_BUILTIN_META_NAMES & set(names))
    assert not (META & set(names))


# ── describe_tool ─────────────────────────────────────────────────────────────


def test_describe_tool_returns_schemas_and_flags_unknown():
    out = json.loads(
        _Stub()._run_describe_builtin_tool({"names": ["repo_grep", "nope"]})
    )
    by_name = {t["name"]: t for t in out["tools"]}
    assert by_name["repo_grep"]["input_schema"] == {
        "type": "object",
        "properties": {},
    }
    assert "error" in by_name["nope"]


def test_describe_tool_requires_names():
    assert "Error" in _Stub()._run_describe_builtin_tool({})


# ── use_tool unwrap ───────────────────────────────────────────────────────────


def test_unwrap_resolves_deferred_tool():
    got = _Stub()._unwrap_use_tool(
        {"name": "manage_denylist", "input": {"action": "list"}}
    )
    assert got == ("manage_denylist", {"action": "list"})


def test_unwrap_refuses_hot_unknown_nested_and_missing():
    stub = _Stub()
    assert "Error" in stub._unwrap_use_tool({"name": "console_status"})
    assert "Error" in stub._unwrap_use_tool({"name": "nope"})
    assert "Error" in stub._unwrap_use_tool({"name": "use_tool"})
    assert "Error" in stub._unwrap_use_tool({})


# ── use_tool inner-input preflight ────────────────────────────────────────────
#
# Search mode surfaces no per-tool JSON schema up front, so a model that skips
# describe_tool used to get opaque handler errors (or silently ignored fields).
# The unwrap now validates the inner input against the deferred tool's real
# schema — the run_runnable _preflight_validate parallel.

_RICH_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "dest_dir": {"type": "string"},
        "mode": {"type": "string", "enum": ["fast", "safe"]},
        "names": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}},
    },
    "required": ["path", "dest_dir"],
}


class _RichStub(_Stub):
    def _custom_agent_tools(self):
        tools = super()._custom_agent_tools()
        for t in tools:
            if t["name"] == "outlook_inbox":
                t["input_schema"] = dict(_RICH_SCHEMA)
        return tools


def test_preflight_unknown_and_missing_and_enum():
    pf = Bridge._preflight_builtin_input
    ok = {"path": "/a", "dest_dir": "/b"}
    assert pf("t", _RICH_SCHEMA, ok) is None
    err = pf("t", _RICH_SCHEMA, {**ok, "bogus": 1})
    assert "unknown argument" in err and "bogus" in err and "path" in err
    err = pf("t", _RICH_SCHEMA, {"path": "/a"})
    assert "missing required" in err and "dest_dir" in err
    err = pf("t", _RICH_SCHEMA, {**ok, "mode": "wrong"})
    assert "must be one of" in err and "fast" in err
    err = pf("t", _RICH_SCHEMA, {**ok, "names": ["a", "zz"]})
    assert "invalid value" in err and "zz" in err


def test_preflight_respects_open_and_empty_schemas():
    pf = Bridge._preflight_builtin_input
    open_schema = {**_RICH_SCHEMA, "additionalProperties": True}
    assert pf("t", open_schema, {"path": "/a", "dest_dir": "/b", "extra": 1}) is None
    assert pf("t", {}, {"anything": 1}) is None  # schemaless — nothing to check


def test_unwrap_preflights_inner_input():
    stub = _RichStub()
    got = stub._unwrap_use_tool(
        {"name": "outlook_inbox", "input": {"path": "/a", "dest_dir": "/b"}}
    )
    assert got == ("outlook_inbox", {"path": "/a", "dest_dir": "/b"})
    err = stub._unwrap_use_tool({"name": "outlook_inbox", "input": {"path": "/a"}})
    assert "missing required" in err and "describe_tool" in err
    err = stub._unwrap_use_tool(
        {"name": "outlook_inbox", "input": {"path": "/a", "dest_dir": "/b", "x": 1}}
    )
    assert "unknown argument" in err


# ── gate integration ──────────────────────────────────────────────────────────
#
# The contract (the run_runnable precedent, re-broken and re-fixed in 0.190.1):
# the gate DECIDES on the inner tool, the confirm prompt keeps the RAW wrapper
# name (the frontend matches the prompt to the running tool segment by exact
# name — a renamed prompt never renders Approve/Deny and times out as a deny),
# the card DISPLAYS the inner tool, and EXECUTION runs the inner tool.


class _GateStub(_Stub):
    _gated_run_tool = Bridge._gated_run_tool
    _confirm_decision = Bridge._confirm_decision
    _confirm_card_view = Bridge._confirm_card_view
    _confirm_runs_as = Bridge._confirm_runs_as

    def __init__(self, approve=True, edited=None):
        super().__init__()
        self.await_calls: list = []
        self._approve = approve
        self._edited = edited

    def _await_confirmation(self, tool_name, tool_input, level, chat_id):
        self.await_calls.append((tool_name, tool_input, level))
        return self._approve, self._edited

    def _run_tool_sync(self, tool_name, tool_input, chat_id=""):
        self.sync_calls.append((tool_name, tool_input))
        return "OK"


def test_autonomy_resolves_through_the_wrapper():
    stub = _Stub()
    wrap = {"name": "manage_denylist", "input": {"action": "list"}}
    assert stub._effective_tool_autonomy("use_tool", wrap) == "autonomous"
    wrap = {"name": "manage_denylist", "input": {"action": "all"}}
    assert stub._effective_tool_autonomy("use_tool", wrap) == "confirm"


def test_confirm_card_displays_the_inner_tool():
    stub = _GateStub()
    view = stub._confirm_card_view(
        "use_tool", {"name": "create_document", "input": {"path": "x.md"}}
    )
    assert view == ("create_document", {"path": "x.md"})
    # Unresolved wrapper: show the raw call rather than nothing.
    raw = {"name": "nope"}
    assert stub._confirm_card_view("use_tool", raw) == ("use_tool", raw)


def test_gate_autonomous_inner_runs_without_prompt():
    stub = _GateStub()
    out = stub._gated_run_tool(
        "use_tool", {"name": "manage_denylist", "input": {"action": "list"}}, "chat1"
    )
    assert out == "OK"
    assert stub.await_calls == []  # autonomous inner → no prompt
    assert stub.sync_calls == [("manage_denylist", {"action": "list"})]


def test_gate_confirms_under_raw_name_and_runs_inner():
    stub = _GateStub()
    wrap = {"name": "create_document", "input": {"path": "x.md"}}
    out = stub._gated_run_tool("use_tool", wrap, "chat1")
    assert out == "OK"
    # The prompt registered the RAW wrapper call — the frontend matches the
    # running `use_tool` segment by this name to render Approve/Deny.
    assert stub.await_calls == [("use_tool", wrap, "confirm")]
    # ...but what ran is the inner tool the card displayed.
    assert stub.sync_calls == [("create_document", {"path": "x.md"})]


def test_gate_denied_wrapper_runs_nothing():
    stub = _GateStub(approve=False)
    out = stub._gated_run_tool(
        "use_tool", {"name": "create_document", "input": {}}, "chat1"
    )
    # The refusal names the inner tool the operator saw on the card.
    assert "declined" in out and "create_document" in out
    assert stub.sync_calls == []


def test_gate_edited_input_replaces_inner_input():
    stub = _GateStub(edited={"path": "edited.md"})
    out = stub._gated_run_tool(
        "use_tool", {"name": "create_document", "input": {"path": "x.md"}}, "chat1"
    )
    assert out == "OK"
    assert stub.sync_calls == [("create_document", {"path": "edited.md"})]


def test_gate_returns_unwrap_error_without_running():
    stub = _GateStub()
    out = stub._gated_run_tool("use_tool", {"name": "nope"}, "chat1")
    assert "Error" in out
    assert stub.await_calls == [] and stub.sync_calls == []


class _RegisterStub(_GateStub):
    _register_confirm = Bridge._register_confirm

    def __init__(self):
        super().__init__()
        self.events: list = []
        self._pending_confirms: dict = {}
        self._pending_confirm_meta: dict = {}

    def _dispatch(self, event, detail):
        self.events.append((event, detail))

    def _notify_confirm(self, tool_name, tool_input, level):
        pass


def test_register_confirm_keeps_raw_name_displays_inner():
    """The regression test for the stuck confirm card: the prompt event must
    carry the raw wrapper as tool_name (segment matching) and the inner tool as
    display_name/display_input (what the operator approves)."""
    stub = _RegisterStub()
    stub._register_confirm(
        "chat1",
        "use_tool",
        {"name": "create_document", "input": {"path": "x.md"}},
        "confirm",
    )
    (event, detail) = stub.events[0]
    assert event == "runspec:tool_confirm"
    assert detail["tool_name"] == "use_tool"
    assert detail["display_name"] == "create_document"
    assert detail["display_input"] == {"path": "x.md"}


def test_register_confirm_carries_the_current_call_id():
    """The confirm names the exact tool call it gates (set per call by the agent
    loop), so the UI puts Approve/Deny on that card — not on every running card
    that shares the tool name (every search-mode runnable call is run_runnable)."""
    from runspec_console.bridge_const import CURRENT_CALL_ID

    stub = _RegisterStub()
    token = CURRENT_CALL_ID.set("toolu_b")
    try:
        rid, _ev, _h = stub._register_confirm(
            "chat1", "use_tool", {"name": "create_document", "input": {}}, "confirm"
        )
    finally:
        CURRENT_CALL_ID.reset(token)
    (_event, detail) = stub.events[0]
    assert detail["call_id"] == "toolu_b"
    assert stub._pending_confirm_meta[rid]["call_id"] == "toolu_b"
    # Outside a tool call (rule runs, gates with no agent call) it's empty.
    stub._register_confirm(
        "chat1", "use_tool", {"name": "create_document", "input": {}}, "confirm"
    )
    assert stub.events[1][1]["call_id"] == ""


# ── fail-closed dispatch + autonomy registration ──────────────────────────────


class _SyncStub(_Stub):
    _run_tool_sync = Bridge._run_tool_sync


def test_run_tool_sync_fails_closed_on_wrapped_use_tool():
    out = _SyncStub()._run_tool_sync("use_tool", {"name": "repo_grep"})
    assert "Error" in out and "autonomy gate" in out


def test_run_tool_sync_dispatches_describe_tool():
    out = json.loads(
        _SyncStub()._run_tool_sync("describe_tool", {"names": ["run_python_sandboxed"]})
    )
    assert out["tools"][0]["name"] == "run_python_sandboxed"


def test_describe_autonomous_use_tool_gated_when_unwrapped_is_skipped():
    stub = _Stub()
    assert stub._effective_tool_autonomy("describe_tool", {}) == "autonomous"
    assert stub._effective_tool_autonomy("use_tool", {}) == "confirm"


# ── headless: inline built-ins + defensive strip ──────────────────────────────


def test_filter_headless_strips_meta_tools():
    tools = [_tool("use_tool"), _tool("describe_tool"), _tool("workspace_ls")]
    out = Bridge._filter_headless_tools(tools, None)
    assert [t["name"] for t in out] == ["workspace_ls"]


# ── folded menu ───────────────────────────────────────────────────────────────


def test_builtin_index_lists_deferred_with_first_sentence():
    lines = _Stub()._build_builtin_index().splitlines()
    assert len(lines) == len(DEFERRED)
    assert lines[0] == "- manage_denylist — Do a thing."


def test_builtin_index_empty_when_inline_or_disabled():
    assert _Stub(llm_extra={"defer_builtins": False})._build_builtin_index() == ""
    assert _Stub(llm_extra={"tool_index": False})._build_builtin_index() == ""


def test_chat_index_includes_deferred_menu_and_rides_the_system_section():
    stub = _Stub()
    text = stub._chat_index_text()
    assert "use_tool" in text and "- repo_grep" in text
    # The index composes into the system prompt as a trailing section (not the
    # first user message) — so the deferred-tools menu still reaches the model.
    section = stub._catalog_system_section(text)
    assert "repo_grep" in section
    assert section.startswith("What you can reach right now, by category:")


def test_chat_index_empty_outside_search_mode():
    assert _Stub(mode="dedup")._chat_index_text() == ""
