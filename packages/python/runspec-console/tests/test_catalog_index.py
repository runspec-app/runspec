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
    def __init__(self, runnables, config=None):
        self._lock = threading.Lock()
        self._runnables_cache = runnables
        self._config = config or {}

    def get_config(self):
        return self._config

    _first_sentence = staticmethod(Bridge._first_sentence)
    _sanitize_tool_name = staticmethod(Bridge._sanitize_tool_name)
    _dedup_tool_name = Bridge._dedup_tool_name
    _catalog_index_enabled = Bridge._catalog_index_enabled
    _build_catalog_index = Bridge._build_catalog_index
    _fold_catalog_index = Bridge._fold_catalog_index


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
    out = _Bridge(
        runnables, config={"llm": {"tool_index_limit": 4}}
    )._build_catalog_index()
    lines = out.splitlines()
    assert len(lines) == 5  # 4 + overflow note
    assert "and 6 more" in lines[-1]


def test_index_empty_when_no_runnables():
    assert _Bridge([])._build_catalog_index() == ""


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
