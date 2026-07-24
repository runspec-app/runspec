"""
test_files_tool.py — the read-only ``list_files`` built-in agent tool.

Browse a directory on local, a fleet host, or an ad-hoc SSH host (reached
directly by default; jump only when unreachable) so the agent can find exact
paths before download_file/relay_file. Thin wrapper over the Forms
path-picker's ``list_dir`` and the Transfer dialog's cached ad-hoc browser
``list_dir_adhoc``. These tests bind the real Bridge methods onto a light stub
(the pattern used across the suite).
"""

from __future__ import annotations

import json

from runspec_console.bridge import Bridge
from runspec_console.bridge_agent_tools import _FILES_TOOL_MAX_ENTRIES
from runspec_console.bridge_const import (
    _BUILTIN_TOOL_NAMES,
    _FILES_TOOL_NAMES,
    _HOT_BUILTIN_NAMES,
)


def _entry(name, etype="file", size=10, mtime=1700000000):
    return {"name": name, "type": etype, "size": size, "mtime": mtime}


class _Stub:
    _files_tools = Bridge._files_tools
    _run_files_tool = Bridge._run_files_tool
    _effective_tool_autonomy = Bridge._effective_tool_autonomy

    def __init__(self, hosts=None, credentials=None, listing=None, error=None):
        self._hosts = hosts or []
        self._credentials = credentials or []
        self._listing = listing if listing is not None else [_entry("a.log")]
        self._error = error
        self.list_dir_calls: list = []
        self.adhoc_calls: list = []

    def _result(self):
        if self._error:
            return {"error": self._error}
        return {"cwd": "/srv", "parent": "/", "entries": list(self._listing)}

    def list_dir(self, host, path="", dirs_only=False, **kw):
        self.list_dir_calls.append((host, path, dirs_only))
        return self._result()

    def list_dir_adhoc(
        self, jump, host, path="", credential_id=None, dirs_only=False, **kw
    ):
        self.adhoc_calls.append((jump, host, path, credential_id, dirs_only))
        return self._result()


# ── registration ──────────────────────────────────────────────────────────────


def test_list_files_is_a_hot_builtin():
    assert "list_files" in _FILES_TOOL_NAMES
    assert _FILES_TOOL_NAMES <= _BUILTIN_TOOL_NAMES
    assert _FILES_TOOL_NAMES <= _HOT_BUILTIN_NAMES  # inline even in search mode


def test_list_files_is_autonomous():
    assert _Stub()._effective_tool_autonomy("list_files", {}) == "autonomous"


def test_dispatch_routes_to_files_handler():
    stub = _Stub()
    out = Bridge._run_tool_sync(stub, "list_files", {"path": "/srv"})
    assert json.loads(out)["cwd"] == "/srv"


# ── schema ────────────────────────────────────────────────────────────────────


def test_schema_host_enum_and_conditional_adhoc_props():
    stub = _Stub(
        hosts=[{"name": "web1", "ssh": "u@web1"}],
        credentials=[{"id": "svc", "label": "Svc"}],
    )
    (tool,) = stub._files_tools()
    props = tool["input_schema"]["properties"]
    assert props["host"]["enum"] == ["local", "web1"]
    assert props["source_via"]["enum"] == ["web1"]
    assert props["source_credential"]["enum"] == ["svc"]
    # The direct-by-default rule is stated where the agent reads it.
    assert "directly" in tool["description"]
    assert (
        "only when the console can't reach it directly"
        in (props["source_via"]["description"])
    )


def test_schema_omits_jump_and_credential_when_unavailable():
    (tool,) = _Stub()._files_tools()
    props = tool["input_schema"]["properties"]
    assert "source_via" not in props  # no remotes to jump through
    assert "source_credential" not in props
    assert "source_target" in props  # jumpless ad-hoc still offered


# ── handler routing ───────────────────────────────────────────────────────────


def test_defaults_to_local_home():
    stub = _Stub()
    out = json.loads(stub._run_files_tool({}))
    assert stub.list_dir_calls == [("local", "", False)]
    assert out["entries"] == [_entry("a.log")]


def test_fleet_host_routes_to_list_dir():
    stub = _Stub(hosts=[{"name": "web1", "ssh": "u@web1"}])
    stub._run_files_tool({"host": "web1", "path": "/var/log", "dirs_only": True})
    assert stub.list_dir_calls == [("web1", "/var/log", True)]


def test_adhoc_routes_jumpless_by_default():
    stub = _Stub()
    stub._run_files_tool({"source_target": "db9.corp", "path": "/tmp"})
    # jump is "" — the console connects directly; no fleet host involved.
    assert stub.adhoc_calls == [("", "db9.corp", "/tmp", None, False)]


def test_adhoc_with_jump_and_credential():
    stub = _Stub(hosts=[{"name": "jump1", "ssh": "u@jump1"}])
    stub._run_files_tool(
        {
            "source_target": "db9.corp",
            "source_via": "jump1",
            "source_credential": "svc",
        }
    )
    assert stub.adhoc_calls == [("jump1", "db9.corp", "", "svc", False)]


def test_host_and_target_is_an_error():
    stub = _Stub()
    out = stub._run_files_tool({"host": "local", "source_target": "db9"})
    assert "Error" in out and stub.list_dir_calls == [] and stub.adhoc_calls == []


def test_via_without_target_is_an_error():
    out = _Stub()._run_files_tool({"source_via": "jump1"})
    assert "Error" in out


def test_listing_error_passes_through():
    out = _Stub(error="Permission denied")._run_files_tool({"path": "/root"})
    assert out == "Error: Permission denied"


def test_listing_is_capped_with_note():
    entries = [_entry(f"f{i:04d}") for i in range(_FILES_TOOL_MAX_ENTRIES + 50)]
    out = json.loads(_Stub(listing=entries)._run_files_tool({}))
    assert len(out["entries"]) == _FILES_TOOL_MAX_ENTRIES
    assert out["truncated"] is True and "note" in out
