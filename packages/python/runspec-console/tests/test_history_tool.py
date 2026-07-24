"""
test_history_tool.py — the read-only ``search_history`` built-in agent tool.

The tool queries the same audit records the History tab shows (via the
reporting mixin's get_history / get_history_all) and returns a bounded,
newest-first JSON digest with the heavy fields stripped. These tests bind the
real Bridge methods onto a light stub (the pattern used across the suite) and
exercise the schema builder, the handler's filtering/bounding, and the
autonomy/dispatch registration.
"""

from __future__ import annotations

import json

from runspec_console.bridge import Bridge
from runspec_console.bridge_agent_tools import (
    _HISTORY_TOOL_DEFAULT_LIMIT,
    _HISTORY_TOOL_LOG_DEFAULT_LIMIT,
    _HISTORY_TOOL_LOG_LINES,
    _HISTORY_TOOL_LOG_MAX_LIMIT,
    _HISTORY_TOOL_MAX_LIMIT,
)
from runspec_console.bridge_const import _BUILTIN_TOOL_NAMES, _HISTORY_TOOL_NAMES


def _rec(**over):
    r = {
        "id": "abc123",
        "runId": "r1",
        "runnable": "deploy",
        "group": "ops",
        "host": "local",
        "operator": "alice",
        "runAs": "",
        "exitCode": 0,
        "durationMs": 1200,
        "ts": "2026-07-22T10:00:00",
        "args": {"env": "prod"},
        "argSources": {"env": "cli"},
        "commandPath": [],
        "logLines": [
            {"ts": "2026-07-22T10:00:00", "level": "INFO", "message": "hello"}
        ],
        "initiatedBy": "user",
        "trigger": "",
        "runbook": "",
    }
    r.update(over)
    return r


class _Stub:
    # Methods under test, bound from the real Bridge mixins.
    _history_tools = Bridge._history_tools
    _run_history_tool = Bridge._run_history_tool
    _slim_history_record = staticmethod(Bridge._slim_history_record)
    _effective_tool_autonomy = Bridge._effective_tool_autonomy

    def __init__(self, hosts=None, all_records=None, per_host=None):
        self._hosts = hosts or []
        self._all_records = all_records or []
        self._per_host = per_host or {}
        self.history_calls: list[tuple[str, str | None]] = []

    def get_history_all(self):
        return list(self._all_records)

    def get_history(self, host, runnable=None):
        self.history_calls.append((host, runnable))
        recs = self._per_host.get(host, [])
        if runnable:
            # The real filter is filename-glob based; exact-name is the
            # equivalent model for these tests.
            recs = [r for r in recs if r.get("runnable") == runnable]
        return list(recs)


# ── registration ──────────────────────────────────────────────────────────────


def test_search_history_is_a_builtin():
    assert "search_history" in _HISTORY_TOOL_NAMES
    assert _HISTORY_TOOL_NAMES <= _BUILTIN_TOOL_NAMES


def test_search_history_is_autonomous():
    stub = _Stub()
    assert stub._effective_tool_autonomy("search_history", {}) == "autonomous"


def test_dispatch_routes_to_history_handler():
    stub = _Stub(all_records=[_rec()])
    stub_result = Bridge._run_tool_sync(stub, "search_history", {})
    assert json.loads(stub_result)["matches"] == 1


def test_history_tool_offered_by_custom_agent_tools():
    stub = _Stub(hosts=[])
    stub._denylist_tool = lambda: None
    for name in (
        "_outlook_tools",
        "_repo_tools",
        "_status_tools",
        "_memory_tools",
        "_files_tools",
        "_action_tools",
        "_workspace_tools",
        "_exec_tools",
        "_config_tools",
    ):
        setattr(stub, name, list)
    names = [t["name"] for t in Bridge._custom_agent_tools(stub)]
    assert "search_history" in names


# ── schema ────────────────────────────────────────────────────────────────────


def test_schema_host_enum_lists_local_plus_fleet():
    stub = _Stub(hosts=[{"name": "web1", "ssh": "user@web1"}, {"name": "db1"}])
    (tool,) = stub._history_tools()
    assert tool["name"] == "search_history"
    assert tool["input_schema"]["properties"]["host"]["enum"] == [
        "local",
        "web1",
        "db1",
    ]


# ── handler: filtering, bounding, slimming ────────────────────────────────────


def test_default_query_returns_slim_records():
    stub = _Stub(all_records=[_rec(), _rec(runnable="backup", exitCode=1)])
    out = json.loads(stub._run_history_tool({}))
    assert out["matches"] == 2 and out["returned"] == 2
    first = out["records"][0]
    assert first["runnable"] == "deploy"
    assert first["operator"] == "alice"
    assert first["initiatedBy"] == "user"
    assert "logLines" not in first  # heavy fields stripped
    assert "argSources" not in first


def test_runnable_exact_match_beats_substring():
    stub = _Stub(all_records=[_rec(runnable="deploy"), _rec(runnable="deploy-all")])
    out = json.loads(stub._run_history_tool({"runnable": "deploy"}))
    assert out["matches"] == 1
    assert out["records"][0]["runnable"] == "deploy"


def test_runnable_substring_fallback():
    stub = _Stub(all_records=[_rec(runnable="deploy-all")])
    out = json.loads(stub._run_history_tool({"runnable": "deploy"}))
    assert out["matches"] == 1
    assert out["records"][0]["runnable"] == "deploy-all"


def test_failed_only_filters_zero_exit():
    stub = _Stub(all_records=[_rec(), _rec(runnable="backup", exitCode=2)])
    out = json.loads(stub._run_history_tool({"failed_only": True}))
    assert out["matches"] == 1
    assert out["records"][0]["runnable"] == "backup"


def test_limit_defaults_and_clamps():
    stub = _Stub(all_records=[_rec(ts=f"2026-07-22T10:00:{i:02d}") for i in range(60)])
    out = json.loads(stub._run_history_tool({}))
    assert out["returned"] == _HISTORY_TOOL_DEFAULT_LIMIT
    assert "note" in out  # truncation is visible
    out = json.loads(stub._run_history_tool({"limit": 999}))
    assert out["returned"] == _HISTORY_TOOL_MAX_LIMIT
    out = json.loads(stub._run_history_tool({"limit": "bogus"}))
    assert out["returned"] == _HISTORY_TOOL_DEFAULT_LIMIT


def test_empty_result_carries_note():
    stub = _Stub()
    out = json.loads(stub._run_history_tool({"runnable": "ghost"}))
    assert out["matches"] == 0
    assert "note" in out


def test_exception_slimmed_to_type_and_message():
    rec = _rec(
        exitCode=1,
        exception={
            "type": "ValueError",
            "message": "boom",
            "module": "m",
            "frames": [{"file": "a.py", "line": 1}],
        },
    )
    stub = _Stub(all_records=[rec])
    out = json.loads(stub._run_history_tool({}))
    assert out["records"][0]["error"] == {"type": "ValueError", "message": "boom"}
    assert "frames" not in json.dumps(out)


# ── handler: host scoping ─────────────────────────────────────────────────────


def test_host_query_passes_runnable_through():
    stub = _Stub(
        hosts=[{"name": "web1", "ssh": "user@web1"}],
        per_host={"web1": [_rec(host="web1")]},
    )
    out = json.loads(stub._run_history_tool({"host": "web1", "runnable": "deploy"}))
    assert out["matches"] == 1
    assert stub.history_calls == [("web1", "deploy")]


def test_host_query_substring_fallback_refetches_unfiltered():
    stub = _Stub(
        hosts=[{"name": "web1", "ssh": "user@web1"}],
        per_host={"web1": [_rec(host="web1", runnable="deploy-all")]},
    )
    out = json.loads(stub._run_history_tool({"host": "web1", "runnable": "deploy"}))
    assert out["matches"] == 1
    assert out["records"][0]["runnable"] == "deploy-all"
    # First the narrow (glob) read, then the unfiltered fallback.
    assert stub.history_calls == [("web1", "deploy"), ("web1", None)]


def test_slim_record_carries_run_id():
    stub = _Stub(all_records=[_rec()])
    out = json.loads(stub._run_history_tool({}))
    assert out["records"][0]["runId"] == "r1"


# ── handler: log drill (include_log / run_id) ─────────────────────────────────


def test_log_excluded_by_default_included_on_request():
    stub = _Stub(all_records=[_rec()])
    out = json.loads(stub._run_history_tool({}))
    assert "log" not in out["records"][0]
    out = json.loads(stub._run_history_tool({"include_log": True}))
    assert out["records"][0]["log"] == ["2026-07-22T10:00:00 INFO hello"]


def test_run_id_selects_one_run_and_implies_log():
    stub = _Stub(all_records=[_rec(runId="r2", runnable="backup"), _rec(runId="r1")])
    out = json.loads(stub._run_history_tool({"run_id": "r1"}))
    assert out["matches"] == 1
    rec = out["records"][0]
    assert rec["runId"] == "r1" and "log" in rec


def test_log_tail_capped_with_visible_marker():
    lines = [
        {"ts": f"t{i}", "level": "INFO", "message": f"line {i}"}
        for i in range(_HISTORY_TOOL_LOG_LINES + 50)
    ]
    stub = _Stub(all_records=[_rec(logLines=lines)])
    rec = json.loads(stub._run_history_tool({"include_log": True}))["records"][0]
    assert len(rec["log"]) == _HISTORY_TOOL_LOG_LINES
    assert rec["log"][-1].endswith(f"line {_HISTORY_TOOL_LOG_LINES + 49}")
    assert "logTruncated" in rec


def test_include_log_tightens_limits():
    stub = _Stub(all_records=[_rec(ts=f"2026-07-22T10:00:{i:02d}") for i in range(60)])
    out = json.loads(stub._run_history_tool({"include_log": True}))
    assert out["returned"] == _HISTORY_TOOL_LOG_DEFAULT_LIMIT
    out = json.loads(stub._run_history_tool({"include_log": True, "limit": 999}))
    assert out["returned"] == _HISTORY_TOOL_LOG_MAX_LIMIT


def test_log_note_when_no_lines_recorded():
    stub = _Stub(all_records=[_rec(logLines=[])])
    out = json.loads(stub._run_history_tool({"include_log": True}))
    assert "log_note" in out
    assert out["records"][0]["log"] == []


def test_unknown_host_is_a_clear_error():
    stub = _Stub(hosts=[{"name": "web1"}])
    out = stub._run_history_tool({"host": "nope"})
    assert "unknown host 'nope'" in out
    assert "web1" in out and "local" in out
