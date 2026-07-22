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
    "run_python",
    "manage_hosts",
]
META = {"search_runnables", "run_runnable", "describe_runnable"}


class _Stub:
    # Real methods under test, bound from the Bridge mixins.
    _tool_mode = Bridge._tool_mode
    _defer_builtins_enabled = Bridge._defer_builtins_enabled
    _runnables_to_tools = Bridge._runnables_to_tools
    _search_mode_tools = Bridge._search_mode_tools
    _deferred_builtin_tools = Bridge._deferred_builtin_tools
    _builtin_meta_tools = Bridge._builtin_meta_tools
    _run_describe_builtin_tool = Bridge._run_describe_builtin_tool
    _unwrap_use_tool = Bridge._unwrap_use_tool
    _build_builtin_index = Bridge._build_builtin_index
    _chat_index_text = Bridge._chat_index_text
    _build_catalog_index = Bridge._build_catalog_index
    _catalog_index_enabled = Bridge._catalog_index_enabled
    _first_sentence = staticmethod(Bridge._first_sentence)
    _fold_catalog_index = Bridge._fold_catalog_index
    _dedup_tool_name = Bridge._dedup_tool_name
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


# ── gate integration: decisions apply to the INNER tool ───────────────────────


class _GateStub(_Stub):
    _gated_run_tool = Bridge._gated_run_tool

    def __init__(self):
        super().__init__()
        self.decisions: list = []

    def _confirm_decision(self, tool_name, tool_input):
        self.decisions.append(tool_name)
        return "run"

    def _confirm_card_view(self, tool_name, tool_input):
        return tool_name, {}

    def _run_tool_sync(self, tool_name, tool_input, chat_id=""):
        self.sync_calls.append((tool_name, tool_input))
        return "OK"


def test_gate_unwraps_before_deciding_and_dispatching():
    stub = _GateStub()
    out = stub._gated_run_tool(
        "use_tool", {"name": "repo_grep", "input": {"pattern": "x"}}, "chat1"
    )
    assert out == "OK"
    assert stub.decisions == ["repo_grep"]  # the gate judged the inner tool
    assert stub.sync_calls == [("repo_grep", {"pattern": "x"})]


def test_gate_returns_unwrap_error_without_running():
    stub = _GateStub()
    out = stub._gated_run_tool("use_tool", {"name": "nope"}, "chat1")
    assert "Error" in out
    assert stub.decisions == [] and stub.sync_calls == []


# ── fail-closed dispatch + autonomy registration ──────────────────────────────


class _SyncStub(_Stub):
    _run_tool_sync = Bridge._run_tool_sync


def test_run_tool_sync_fails_closed_on_wrapped_use_tool():
    out = _SyncStub()._run_tool_sync("use_tool", {"name": "repo_grep"})
    assert "Error" in out and "autonomy gate" in out


def test_run_tool_sync_dispatches_describe_tool():
    out = json.loads(
        _SyncStub()._run_tool_sync("describe_tool", {"names": ["run_python"]})
    )
    assert out["tools"][0]["name"] == "run_python"


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


def test_chat_index_folds_deferred_menu_into_first_user_message():
    stub = _Stub()
    text = stub._chat_index_text()
    assert "use_tool" in text and "- repo_grep" in text
    history = [{"role": "user", "content": "hi"}]
    folded = stub._fold_catalog_index(history, text)
    assert "repo_grep" in folded[0]["content"]
    assert history[0]["content"] == "hi"  # stored history untouched


def test_chat_index_empty_outside_search_mode():
    assert _Stub(mode="dedup")._chat_index_text() == ""
