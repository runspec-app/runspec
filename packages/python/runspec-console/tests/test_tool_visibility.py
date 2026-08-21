"""
test_tool_visibility.py — the ``[tool_visibility]`` hide feature.

Two independent glob lists curate which runnables appear where:
``hidden_from_agent`` drops runnables from the agent toolset only;
``hidden_from_all`` also drops them from the Forms tab + catalogs. Agent surfaces
filter by the *union*; Forms/catalog surfaces filter by ``hidden_from_all`` only.
Hiding is curation, not a safety block — hidden runnables stay directly runnable.

Covered: the pure matcher (``runnable_hidden``), the config reader
(``read_tool_visibility``), the ``_visible_runnables`` chokepoint for both
surfaces, that ``_runnables_to_tools`` actually excludes hidden tools, and that
``save_tool_visibility`` round-trips into config.toml (validating input).
"""

from __future__ import annotations

import threading

import pytest

import runspec_console.config as cfg
from runspec_console.bridge import Bridge
from runspec_console.bridge_utils import runnable_hidden


# ── pure matcher ──────────────────────────────────────────────────────────────


def test_runnable_hidden_name_glob():
    assert runnable_hidden("outlook-unread", "m365", "local", ["outlook-*"])
    assert not runnable_hidden("system-info", "sys", "local", ["outlook-*"])


def test_runnable_hidden_group_and_host_qualifiers():
    assert runnable_hidden("anything", "m365", "h", ["group:m365"])
    assert not runnable_hidden("anything", "sys", "h", ["group:m365"])
    assert runnable_hidden("anything", "g", "prod-1", ["host:prod-*"])
    assert not runnable_hidden("anything", "g", "dev-1", ["host:prod-*"])


def test_runnable_hidden_empty_hides_nothing():
    assert not runnable_hidden("outlook-unread", "m365", "local", [])
    assert not runnable_hidden("outlook-unread", "m365", "local", None)


def test_runnable_hidden_accepts_bare_string():
    assert runnable_hidden("teams-read-chat", "m365", "local", "teams-*")


# ── config reader ─────────────────────────────────────────────────────────────


@pytest.fixture
def _cfg(monkeypatch):
    """Swap ``config.read_config`` so readers/writers hit an in-memory dict."""
    store: dict = {}

    def _write(data):
        store.clear()
        store.update(data)

    monkeypatch.setattr(cfg, "read_config", lambda: dict(store))
    monkeypatch.setattr(cfg, "write_config", _write)
    return store


def test_read_tool_visibility_absent_is_empty(_cfg):
    assert cfg.read_tool_visibility() == {
        "hidden_from_agent": [],
        "hidden_from_all": [],
    }


def test_read_tool_visibility_reads_and_cleans(_cfg):
    _cfg["tool_visibility"] = {
        "hidden_from_agent": ["outlook-*", "  ", "teams-*"],
        "hidden_from_all": ["graph-*", 5],
    }
    got = cfg.read_tool_visibility()
    assert got["hidden_from_agent"] == ["outlook-*", "teams-*"]
    assert got["hidden_from_all"] == ["graph-*", "5"]


def test_read_tool_visibility_non_list_ignored(_cfg):
    _cfg["tool_visibility"] = {"hidden_from_agent": "outlook-*"}
    assert cfg.read_tool_visibility()["hidden_from_agent"] == []


# ── _visible_runnables chokepoint ─────────────────────────────────────────────


class _VisBridge:
    """Minimal harness binding the real ``_visible_runnables`` filter."""

    _visible_runnables = Bridge._visible_runnables

    def __init__(self, runnables):
        self._lock = threading.Lock()
        self._runnables_cache = runnables


def _rn(name, group="m365", host="local"):
    return {"name": name, "group": group, "host": host, "args": []}


CACHE = [
    _rn("outlook-unread"),
    _rn("teams-read-chat"),
    _rn("system-info", group="sys"),
    _rn("disk-usage", group="sys"),
]


def _names(rows):
    return {r["name"] for r in rows}


def test_visible_agent_filters_union(monkeypatch):
    monkeypatch.setattr(
        cfg,
        "read_tool_visibility",
        lambda: {"hidden_from_agent": ["outlook-*"], "hidden_from_all": ["teams-*"]},
    )
    b = _VisBridge(list(CACHE))
    assert _names(b._visible_runnables("agent")) == {"system-info", "disk-usage"}


def test_visible_forms_filters_only_hidden_from_all(monkeypatch):
    monkeypatch.setattr(
        cfg,
        "read_tool_visibility",
        lambda: {"hidden_from_agent": ["outlook-*"], "hidden_from_all": ["teams-*"]},
    )
    b = _VisBridge(list(CACHE))
    # forms keeps outlook (agent-only hide), drops teams (hidden everywhere)
    assert _names(b._visible_runnables("forms")) == {
        "outlook-unread",
        "system-info",
        "disk-usage",
    }


def test_visible_empty_config_returns_all(monkeypatch):
    monkeypatch.setattr(
        cfg,
        "read_tool_visibility",
        lambda: {"hidden_from_agent": [], "hidden_from_all": []},
    )
    b = _VisBridge(list(CACHE))
    assert _names(b._visible_runnables("agent")) == _names(CACHE)


# ── _runnables_to_tools integration (dedup) ───────────────────────────────────


class _ToolsBridge:
    """Binds enough real methods to build the dedup tool layout."""

    def __init__(self, runnables, mode="dedup"):
        self._lock = threading.Lock()
        self._runnables_cache = runnables
        self._credentials = []
        self._mode = mode

    def get_config(self):
        return {"llm": {"tool_mode": self._mode}, "ssh": {}}

    def _reload_credentials(self):
        pass

    def _custom_agent_tools(self):
        return []

    def _host_entry(self, host):
        return {"name": host, "ssh": None}

    _tool_mode = Bridge._tool_mode
    _visible_runnables = Bridge._visible_runnables
    _sanitize_tool_name = staticmethod(Bridge._sanitize_tool_name)
    _dedup_tool_name = Bridge._dedup_tool_name
    _host_selector_key = Bridge._host_selector_key
    _union_scoped_credentials = Bridge._union_scoped_credentials
    _allowed_hosts = Bridge._allowed_hosts
    _build_input_schema = Bridge._build_input_schema
    _runnables_to_tools = Bridge._runnables_to_tools
    _defer_builtins_enabled = Bridge._defer_builtins_enabled
    _scoped_credentials = Bridge._scoped_credentials
    _scope_matches = staticmethod(Bridge._scope_matches)
    _credential_entry = Bridge._credential_entry
    _declared_arg_names = staticmethod(Bridge._declared_arg_names)
    _declared_arg_name_set = staticmethod(Bridge._declared_arg_name_set)
    _secret_arg_names = staticmethod(Bridge._secret_arg_names)


def test_runnables_to_tools_excludes_hidden(monkeypatch):
    monkeypatch.setattr(
        cfg,
        "read_tool_visibility",
        lambda: {"hidden_from_agent": ["outlook-*", "teams-*"], "hidden_from_all": []},
    )
    b = _ToolsBridge(list(CACHE))
    tool_names = {t["name"] for t in b._runnables_to_tools()}
    assert not any(
        n.endswith("outlook-unread") or n.endswith("teams-read-chat")
        for n in tool_names
    )
    assert any(n.endswith("system-info") for n in tool_names)


# ── save round-trip ───────────────────────────────────────────────────────────


class _SaveBridge:
    save_tool_visibility = Bridge.save_tool_visibility
    get_tool_visibility = Bridge.get_tool_visibility

    def _dispatch(self, *a, **k):
        pass


def test_save_tool_visibility_round_trips(_cfg):
    b = _SaveBridge()
    out = b.save_tool_visibility(
        {
            "hidden_from_agent": ["outlook-*", "outlook-*"],
            "hidden_from_all": ["graph-*"],
        }
    )
    # de-duped, persisted, and read back
    assert out == {"hidden_from_agent": ["outlook-*"], "hidden_from_all": ["graph-*"]}
    assert _cfg["tool_visibility"] == {
        "hidden_from_agent": ["outlook-*"],
        "hidden_from_all": ["graph-*"],
    }


def test_save_tool_visibility_empty_drops_section(_cfg):
    _cfg["tool_visibility"] = {"hidden_from_agent": ["x-*"]}
    _cfg["other"] = {"keep": True}
    b = _SaveBridge()
    b.save_tool_visibility({"hidden_from_agent": [], "hidden_from_all": []})
    assert "tool_visibility" not in _cfg
    assert _cfg["other"] == {"keep": True}  # other sections preserved


def test_save_tool_visibility_rejects_bad_input(_cfg):
    b = _SaveBridge()
    with pytest.raises(ValueError):
        b.save_tool_visibility({"hidden_from_agent": ["ok", ""]})
    with pytest.raises(ValueError):
        b.save_tool_visibility({"hidden_from_all": "not-a-list"})
