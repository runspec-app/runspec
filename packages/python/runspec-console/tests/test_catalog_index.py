"""
test_catalog_index.py — the always-on catalog index for search mode.

Search mode hides runnables behind ``search_runnables``, so the agent can only
find what it already thinks to look for. The index folds a compact menu (id +
first sentence, no schemas) into the conversation so the agent always sees what
exists. These tests cover index construction, the first-sentence helper, the
config gate + cap, and the non-mutating fold into the message history.
"""

from __future__ import annotations

import threading

from runspec_console.bridge import Bridge


class _Bridge:
    def __init__(self, runnables, config=None, *, ready=True, progress=(1, 1)):
        self._lock = threading.Lock()
        self._runnables_cache = runnables
        self._config = config or {}
        # Discovery readiness — default ready so the index reads as a finished
        # fleet; the not-ready path appends a "still loading" note for the agent.
        self._discovery_ready = ready
        self._discovery_progress = progress

    def get_config(self):
        return self._config

    _first_sentence = staticmethod(Bridge._first_sentence)
    _sanitize_tool_name = staticmethod(Bridge._sanitize_tool_name)
    _dedup_tool_name = Bridge._dedup_tool_name
    _catalog_index_enabled = Bridge._catalog_index_enabled
    _build_catalog_index = Bridge._build_catalog_index
    _visible_runnables = Bridge._visible_runnables
    _fold_catalog_index = Bridge._fold_catalog_index
    _tool_mode = Bridge._tool_mode
    _defer_builtins_enabled = Bridge._defer_builtins_enabled
    _build_builtin_index = Bridge._build_builtin_index
    _chat_index_text = Bridge._chat_index_text


def _rn(host, name, group="fleet", description=""):
    return {"host": host, "name": name, "group": group, "description": description}


# ── first sentence ────────────────────────────────────────────────────────────


def test_first_sentence_splits_and_trims():
    assert Bridge._first_sentence("Raise a ticket. Then wait.") == "Raise a ticket."
    assert Bridge._first_sentence("  multi\n  line\ntext  ") == "multi line text"
    assert Bridge._first_sentence("") == ""


def test_first_sentence_caps_long_text():
    out = Bridge._first_sentence("x" * 300, maxlen=50)
    assert len(out) == 50 and out.endswith("…")


# ── index construction ────────────────────────────────────────────────────────


def test_index_one_line_per_runnable_deduped_and_sorted():
    runnables = [
        _rn("a", "jsm-raise", group="jsm", description="Raise a request. More."),
        _rn("b", "jsm-raise", group="jsm", description="Raise a request. More."),
        _rn("a", "list-procs", group="linux", description="List processes."),
    ]
    out = _Bridge(runnables)._build_catalog_index()
    lines = out.splitlines()
    # deduped across hosts → 2 lines; sorted by (group, name)
    assert lines == [
        "- jsm__jsm-raise — Raise a request.",
        "- linux__list-procs — List processes.",
    ]


def test_index_handles_missing_description():
    out = _Bridge([_rn("a", "ping", description="")])._build_catalog_index()
    assert out == "- fleet__ping"


def test_index_disabled_returns_empty():
    b = _Bridge([_rn("a", "ping")], config={"llm": {"tool_index": False}})
    assert b._build_catalog_index() == ""
    b2 = _Bridge([_rn("a", "ping")], config={"llm": {"tool_index": "off"}})
    assert b2._build_catalog_index() == ""


def test_index_enabled_by_default():
    assert _Bridge([_rn("a", "ping")])._catalog_index_enabled() is True


def test_index_caps_with_overflow_note():
    runnables = [_rn("a", f"task-{i:03d}") for i in range(10)]
    b = _Bridge(runnables, config={"llm": {"tool_index_limit": 4}})
    out = b._build_catalog_index()
    lines = out.splitlines()
    assert len(lines) == 5  # 4 + overflow note
    assert "and 6 more" in lines[-1]
    # The overflow count is recorded for the context indicator ("N beyond menu").
    assert b._catalog_overflow == 6


def test_index_overflow_zero_when_under_cap():
    """No truncation ⇒ overflow is 0, so the indicator shows no 'beyond menu'."""
    b = _Bridge([_rn("a", f"task-{i}") for i in range(3)], config={"llm": {}})
    b._build_catalog_index()
    assert b._catalog_overflow == 0


def test_index_empty_when_no_runnables():
    assert _Bridge([])._build_catalog_index() == ""


# ── discovery-readiness hint (the first-turn "blind" fix) ─────────────────────


def test_index_empty_but_not_ready_tells_agent_discovery_in_progress():
    # First turn after launch: nothing discovered yet. Instead of "" (which folds
    # nothing and leaves the agent blind), the index is a "still loading" note so
    # the agent won't conclude a runnable doesn't exist.
    out = _Bridge([], ready=False, progress=(0, 3))._build_catalog_index()
    assert out != ""
    assert "discovery in progress" in out.lower()
    assert "0 of 3 hosts" in out


def test_index_partial_and_not_ready_appends_loading_note():
    out = _Bridge(
        [_rn("a", "ping", description="Ping a host.")], ready=False, progress=(1, 3)
    )._build_catalog_index()
    lines = out.splitlines()
    assert lines[0] == "- fleet__ping — Ping a host."  # the real entry still leads
    assert "1 of 3 hosts" in lines[-1]  # trailing readiness note


def test_index_when_ready_has_no_loading_note():
    out = _Bridge(
        [_rn("a", "ping", description="Ping a host.")], ready=True
    )._build_catalog_index()
    assert "discovery in progress" not in out.lower()


# ── folding into history (non-mutating) ───────────────────────────────────────


def test_fold_prepends_to_first_user_message():
    history = [
        {"role": "user", "content": "raise a ticket"},
        {"role": "assistant", "content": "ok"},
    ]
    out = _Bridge([])._fold_catalog_index(history, "- jsm__raise — Raise.")
    assert out[0]["content"].endswith("raise a ticket")
    assert "jsm__raise" in out[0]["content"]
    assert out[1] == {"role": "assistant", "content": "ok"}
    # original history untouched
    assert history[0]["content"] == "raise a ticket"


def test_fold_noop_when_index_empty():
    history = [{"role": "user", "content": "hi"}]
    out = _Bridge([])._fold_catalog_index(history, "")
    assert out is history  # same object, no copy


def test_fold_only_first_user_message():
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "second"},
    ]
    out = _Bridge([])._fold_catalog_index(history, "- x — y")
    assert "x — y" in out[0]["content"]
    assert out[2]["content"] == "second"  # untouched


def test_fold_preamble_is_a_generic_header():
    """The preamble must not describe the blob's contents — the index carries more
    than one kind of section (runnables, plus the deferred built-ins menu), and each
    labels itself. It also used to claim "This is the full menu", which is false
    whenever _build_catalog_index truncated at tool_index_limit."""
    out = _Bridge([])._fold_catalog_index(
        [{"role": "user", "content": "hi"}], "- x — y"
    )
    preamble = out[0]["content"].split("- x — y")[0]
    assert "run_runnable" not in preamble
    assert "full menu" not in preamble


# ── the argument-lookup instruction (chat + trigger parity) ───────────────────


def test_chat_index_text_carries_arg_lookup_instruction():
    """The search-mode index text (used by BOTH the chat turn and, since the fix,
    the unrestricted trigger/schedule turn) must tell the agent to look up a
    runnable's arguments before running it — otherwise a schema-less run_runnable
    leads it to guess. The bare _build_catalog_index() deliberately omits it."""
    # defer_builtins off keeps _build_builtin_index() empty — the same state the
    # headless trigger path is in (built-ins inline), isolating the runnable index.
    b = _Bridge(
        [_rn("a", "ping", description="Ping a host.")],
        config={"llm": {"tool_mode": "search", "defer_builtins": False}},
    )
    index = b._chat_index_text()
    assert "search_runnables or describe_runnable" in index
    assert "before running it" in index
    assert "fleet__ping" in index  # the runnable menu is present
    # The bare menu (what the trigger path used before the fix) lacks it.
    assert "before running it" not in b._build_catalog_index()


def test_chat_index_text_empty_outside_search_mode():
    b = _Bridge([_rn("a", "ping")], config={"llm": {"tool_mode": "dedup"}})
    assert b._chat_index_text() == ""
