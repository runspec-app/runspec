"""Tests for the console-config agent tools (manage_hosts / manage_groups /
manage_triggers / manage_rota) — schema presence, the read/write autonomy
split, handler behaviour, validation rejections, and the two-layer chat-only
guarantee (headless toolset exclusion + run-origin guard).

Same lightweight-stub pattern as test_action_tools.py: a tiny bridge that
borrows just the methods under test, so no pywebview/COM/LLM is needed."""

from __future__ import annotations

import json
import threading

from runspec_console import config as config_mod
from runspec_console import groups as groups_mod
from runspec_console.bridge import Bridge
from runspec_console.bridge_const import _BUILTIN_TOOL_NAMES, _CONFIG_TOOL_NAMES
from runspec_console.bridge_triggers import TriggersMixin


class _ConfigBridge:
    _custom_agent_tools = Bridge._custom_agent_tools
    _denylist_tool = Bridge._denylist_tool
    _outlook_tools = Bridge._outlook_tools
    _repo_tools = Bridge._repo_tools
    _memory_tools = Bridge._memory_tools
    _status_tools = Bridge._status_tools
    _workspace_tools = Bridge._workspace_tools
    _exec_tools = Bridge._exec_tools
    _action_tools = Bridge._action_tools
    _config_tools = Bridge._config_tools
    _run_config_tool = Bridge._run_config_tool
    _config_manage_hosts = Bridge._config_manage_hosts
    _config_manage_groups = Bridge._config_manage_groups
    _config_manage_triggers = Bridge._config_manage_triggers
    _config_manage_rota = Bridge._config_manage_rota
    _effective_tool_autonomy = Bridge._effective_tool_autonomy
    _clean_trigger = staticmethod(Bridge._clean_trigger)

    def __init__(self, hosts=None, groups=None):
        self._hosts = hosts or []
        self._runnables_cache = []
        self._filters = []
        self._credentials = []
        self._lock = threading.Lock()
        self._run_origin: dict = {}
        self._trigger_engine = None
        self._groups = groups or []
        # Recorded mutations
        self.saved_hosts: list = []
        self.saved_groups: list = []
        self.renamed: list = []
        self.saved_triggers: list = []
        self.deleted_triggers: list = []
        self.toggled: list = []
        self.saved_rota: list = []
        self.dispatched: list = []

    # collaborators the handlers delegate to -------------------------------
    def _reload_credentials(self):
        pass

    def _groups_map(self):
        out: dict[str, list[str]] = {}
        for h in self._hosts:
            g = str(h.get("group") or "").strip()
            if g:
                out.setdefault(g, []).append(h["name"])
        return out

    def get_jump_hosts(self):
        return [dict(h) for h in self._hosts]

    def save_jump_hosts(self, hosts):
        self.saved_hosts.append(hosts)
        self._hosts = hosts

    def get_groups(self):
        return [dict(g) for g in self._groups]

    def save_groups(self, groups):
        self.saved_groups.append(groups)
        self._groups = groups

    def rename_group(self, old, new):
        if not old.strip() or not new.strip():
            raise ValueError("A group needs a name.")
        self.renamed.append((old, new))
        for g in self._groups:
            if g.get("name") == old:
                g["name"] = new

    def save_trigger(self, trigger):
        self.saved_triggers.append(trigger)
        return {"ok": True}

    def delete_trigger(self, name):
        self.deleted_triggers.append(name)
        return {"ok": name == "known"}

    def set_trigger_enabled(self, name, enabled):
        self.toggled.append((name, enabled))
        return {"ok": name == "known"}

    def save_rota(self, rota):
        self.saved_rota.append(rota)
        return {"ok": True}

    def _dispatch(self, event, payload):
        self.dispatched.append((event, payload))


# ── registration + autonomy ──────────────────────────────────────────────────


def test_config_tools_present_in_both_custom_agent_tool_branches():
    remote = _ConfigBridge(hosts=[{"name": "prod", "ssh": "deploy@prod"}])
    local_only = _ConfigBridge(hosts=[{"name": "local"}])
    for b in (remote, local_only):
        names = {t["name"] for t in b._custom_agent_tools()}
        assert _CONFIG_TOOL_NAMES <= names


def test_config_tools_are_builtin_tracked():
    assert _CONFIG_TOOL_NAMES <= _BUILTIN_TOOL_NAMES


def test_read_actions_autonomous_mutations_confirm():
    b = _ConfigBridge()
    for name in _CONFIG_TOOL_NAMES:
        assert b._effective_tool_autonomy(name, {"action": "list"}) == "autonomous"
        assert b._effective_tool_autonomy(name, {"action": "get"}) == "autonomous"
        for action in ("add", "update", "remove", "save", "rename", "set", "delete"):
            assert b._effective_tool_autonomy(name, {"action": action}) == "confirm"
        # Missing/unknown action gates by default.
        assert b._effective_tool_autonomy(name, {}) == "confirm"
        assert b._effective_tool_autonomy(name, {"action": "bogus"}) == "confirm"


# ── chat-only: layer 1 (headless toolset) ────────────────────────────────────


def test_headless_filter_strips_config_tools_regardless_of_allowlist():
    tools = [
        {"name": "manage_triggers"},
        {"name": "manage_hosts"},
        {"name": "create_schedule"},
        {"name": "prod__disk-usage"},
    ]
    for allow in (None, [], ["manage_triggers", "create_schedule"]):
        kept = {t["name"] for t in TriggersMixin._filter_headless_tools(tools, allow)}
        assert kept & _CONFIG_TOOL_NAMES == set()
        assert "prod__disk-usage" in kept
    # The governed narrowing still works as before.
    kept = {
        t["name"] for t in TriggersMixin._filter_headless_tools(tools, ["emit_event"])
    }
    assert "create_schedule" not in kept


# ── chat-only: layer 2 (origin guard) ────────────────────────────────────────


def test_trigger_originated_turn_is_refused_and_writes_nothing():
    b = _ConfigBridge(hosts=[{"name": "prod"}])
    b._run_origin["card1"] = {"trigger": "outlook", "agent": True, "runbook": ""}
    out = b._run_config_tool(
        "manage_hosts", {"action": "remove", "name": "prod"}, "card1"
    )
    assert out.startswith("✗") and "chat-only" in out
    assert b.saved_hosts == []
    # A chat turn (no registered origin) is allowed.
    out = b._run_config_tool("manage_hosts", {"action": "list"}, "chat1")
    assert json.loads(out)[0]["name"] == "prod"


# ── manage_hosts ─────────────────────────────────────────────────────────────


def test_manage_hosts_add_update_remove_roundtrip():
    b = _ConfigBridge(hosts=[{"name": "prod", "hostname": "prod", "room_port": 8765}])
    out = b._run_config_tool(
        "manage_hosts",
        {
            "action": "add",
            "name": "web-01",
            "hostname": "web-01.internal",
            "user": "deploy",
            "runspec_paths": ["/opt/venvs/ops/bin/runspec"],
            "group": "web",
        },
        "",
    )
    assert out.startswith("✓")
    assert [h["name"] for h in b.saved_hosts[-1]] == ["prod", "web-01"]

    # Partial update touches only the provided keys; unmanaged keys survive.
    out = b._run_config_tool(
        "manage_hosts", {"action": "update", "name": "prod", "group": "web"}, ""
    )
    assert out.startswith("✓")
    prod = next(h for h in b.saved_hosts[-1] if h["name"] == "prod")
    assert prod["group"] == "web"
    assert prod["room_port"] == 8765
    assert prod["hostname"] == "prod"

    out = b._run_config_tool("manage_hosts", {"action": "remove", "name": "prod"}, "")
    assert out.startswith("✓")
    assert [h["name"] for h in b.saved_hosts[-1]] == ["web-01"]


def test_manage_hosts_rejections_never_save():
    b = _ConfigBridge(hosts=[{"name": "prod"}])
    cases = [
        {"action": "add", "name": "prod"},  # duplicate
        {"action": "update", "name": "nope"},  # unknown
        {"action": "remove", "name": "nope"},  # unknown
        {"action": "add"},  # missing name
        {"action": "add", "name": "bad", "port": 99999},  # invalid port
        {"action": "add", "name": "bad2", "runspec_paths": [""]},  # blank path
    ]
    for ti in cases:
        out = b._run_config_tool("manage_hosts", ti, "")
        assert out.startswith("✗"), ti
    assert b.saved_hosts == []


# ── manage_groups ────────────────────────────────────────────────────────────


def test_manage_groups_lifecycle():
    b = _ConfigBridge(
        hosts=[{"name": "web-01", "group": "web"}],
        groups=[{"name": "web", "description": ""}],
    )
    listed = json.loads(b._run_config_tool("manage_groups", {"action": "list"}, ""))
    assert listed["groups"][0]["name"] == "web"
    assert listed["members"] == {"web": ["web-01"]}

    assert b._run_config_tool(
        "manage_groups", {"action": "add", "name": "db", "description": "databases"}, ""
    ).startswith("✓")
    assert {g["name"] for g in b.saved_groups[-1]} == {"web", "db"}

    assert b._run_config_tool(
        "manage_groups", {"action": "rename", "name": "db", "new_name": "data"}, ""
    ).startswith("✓")
    assert b.renamed == [("db", "data")]

    out = b._run_config_tool("manage_groups", {"action": "remove", "name": "web"}, "")
    assert out.startswith("✓") and "1 member host(s)" in out

    for ti in (
        {"action": "add", "name": "data"},  # renamed group still registered
        {"action": "update", "name": "ghost"},
        {"action": "rename", "name": "x"},  # missing new_name
        {"action": "remove", "name": "ghost"},
    ):
        assert b._run_config_tool("manage_groups", ti, "").startswith("✗"), ti


# ── manage_triggers ──────────────────────────────────────────────────────────

_VALID_TRIGGER = {
    "source": "outlook",
    "event": "new_mail",
    "mode": "rule",
    "run": "local__record-ticket",
    "match": {"subject": "(?i)outage"},
    "map": {"summary": "{{ subject }}"},
}


def test_manage_triggers_save_validates_then_persists(monkeypatch):
    b = _ConfigBridge()
    out = b._run_config_tool(
        "manage_triggers",
        {"action": "save", "name": "t1", "trigger": dict(_VALID_TRIGGER)},
        "",
    )
    assert out.startswith("✓")
    assert b.saved_triggers[-1]["name"] == "t1"
    assert ("runspec:triggers_updated", {"name": "t1"}) in b.dispatched


def test_manage_triggers_invalid_rejected_without_write():
    b = _ConfigBridge()
    bad = [
        {},  # missing everything
        {"source": "outlook"},  # missing event
        {**_VALID_TRIGGER, "match": {"subject": "("}},  # bad regex
        {**_VALID_TRIGGER, "autonomy": "bogus"},  # invalid autonomy level
    ]
    for trig in bad:
        out = b._run_config_tool(
            "manage_triggers", {"action": "save", "name": "t1", "trigger": trig}, ""
        )
        assert out.startswith("✗ Not saved — invalid trigger:"), trig
    assert b.saved_triggers == []
    assert b.dispatched == []


def test_manage_triggers_list_delete_toggle(monkeypatch):
    monkeypatch.setattr(
        config_mod, "read_triggers", lambda: [dict(_VALID_TRIGGER, name="known")]
    )
    b = _ConfigBridge()
    listed = json.loads(b._run_config_tool("manage_triggers", {"action": "list"}, ""))
    assert listed["triggers"][0]["name"] == "known"
    assert listed["errors"] == []

    assert b._run_config_tool(
        "manage_triggers", {"action": "delete", "name": "known"}, ""
    ).startswith("✓")
    assert b._run_config_tool(
        "manage_triggers", {"action": "delete", "name": "ghost"}, ""
    ).startswith("✗")
    assert b._run_config_tool(
        "manage_triggers", {"action": "enable", "name": "known"}, ""
    ).startswith("✓")
    assert b._run_config_tool(
        "manage_triggers", {"action": "disable", "name": "known"}, ""
    ).startswith("✓")
    assert b.toggled == [("known", True), ("known", False)]


# ── manage_rota ──────────────────────────────────────────────────────────────


def test_manage_rota_get_and_partial_set(monkeypatch):
    current = {"enabled": True, "mon": {"on": True, "start": "09:00", "end": "17:30"}}
    monkeypatch.setattr(config_mod, "read_rota", lambda: dict(current))
    b = _ConfigBridge()
    got = json.loads(b._run_config_tool("manage_rota", {"action": "get"}, ""))
    assert got["enabled"] is True

    out = b._run_config_tool(
        "manage_rota",
        {
            "action": "set",
            "rota": {"tue": {"on": True, "start": "10:00", "end": "16:00"}},
        },
        "",
    )
    assert out.startswith("✓")
    merged = b.saved_rota[-1]
    assert merged["mon"]["start"] == "09:00"  # untouched keys survive
    assert merged["tue"]["start"] == "10:00"
    assert ("runspec:triggers_updated", {"rota": True}) in b.dispatched


def test_manage_rota_rejections_never_save(monkeypatch):
    monkeypatch.setattr(config_mod, "read_rota", lambda: {})
    b = _ConfigBridge()
    bad = [
        {"action": "set"},  # missing rota
        {"action": "set", "rota": {}},  # empty patch
        {"action": "set", "rota": {"mon": {"start": "25:00"}}},  # bad HH:MM
        {"action": "set", "rota": {"source": "moon"}},  # bad source
        {"action": "set", "rota": {"bogus": 1}},  # unknown key
    ]
    for ti in bad:
        assert b._run_config_tool("manage_rota", ti, "").startswith("✗"), ti
    assert b.saved_rota == []


# ── schema enums ─────────────────────────────────────────────────────────────


def test_schema_enums_from_live_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "groups_path", lambda: tmp_path / "none.toml")
    monkeypatch.setattr(
        groups_mod, "load_groups", lambda path: [{"name": "web", "description": ""}]
    )
    monkeypatch.setattr(config_mod, "read_triggers", lambda: [{"name": "t1"}])
    b = _ConfigBridge(hosts=[{"name": "prod", "group": "db"}])
    tools = {t["name"]: t for t in b._config_tools()}
    host_props = tools["manage_hosts"]["input_schema"]["properties"]
    assert host_props["name"]["enum"] == ["prod"]
    assert host_props["group"]["enum"] == ["db", "web"]  # registry ∪ host-derived
    assert (
        "t1"
        in tools["manage_triggers"]["input_schema"]["properties"]["name"]["description"]
    )
