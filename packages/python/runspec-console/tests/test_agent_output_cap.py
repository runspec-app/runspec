"""
test_agent_output_cap.py — the agent must receive a runnable's full JSON output.

``_run_tool_sync`` hands a runnable's stdout to the model as the tool result. A
data-heavy runspec-linux runnable emits one large compact ``json.dumps`` line;
the old 16 KB cap sliced it mid-structure, so the model got invalid, partial
JSON. These tests drive the real method with a fake executor and assert a normal
large result passes through whole, and an over-cap dump is marked as incomplete.
"""

from __future__ import annotations

import json
import threading

import runspec_console.bridge as bridge_mod
from runspec_console.bridge import Bridge, _AGENT_TOOL_OUTPUT_CAP


class _AgentBridge:
    _run_tool_sync = Bridge._run_tool_sync
    _secret_arg_names = staticmethod(Bridge._secret_arg_names)
    _positionals_for = staticmethod(Bridge._positionals_for)
    _apply_credentials = Bridge._apply_credentials
    _scoped_credentials = Bridge._scoped_credentials
    _scope_matches = staticmethod(Bridge._scope_matches)
    _credential_entry = Bridge._credential_entry
    _declared_arg_names = staticmethod(Bridge._declared_arg_names)

    def _become_params(self, cached, hostname):
        return "", "sudo", None

    def __init__(self, entry, cached):
        self._entry = entry
        self._runnables_cache = [cached]
        self._lock = threading.Lock()
        self._credentials: list = []

    def _host_entry(self, _host):
        return self._entry

    def get_config(self):
        return {"ssh": {}}

    def _reload_credentials(self):
        pass


def _cached(host: str) -> dict:
    return {"host": host, "name": "list-processes", "group": None, "args": []}


def _run_with_stdout(monkeypatch, line: str) -> str:
    """Run the agent tool path with a fake executor that emits one stdout line."""

    def fake_run_local(rp, runnable, args, command_path, on_line, on_done, **kw):
        on_line(line, "stdout")
        on_done(0, 1)

    monkeypatch.setattr(bridge_mod, "run_local", fake_run_local)
    monkeypatch.setattr(bridge_mod, "_paths", lambda _e: ["/opt/app/runspec"])
    b = _AgentBridge(entry={"name": "local"}, cached=_cached("local"))
    return b._run_tool_sync("local__list-processes", {})


def test_large_json_passes_through_whole(monkeypatch):
    # ~200 KB of JSON — far over the old 16 KB cap, well under the new one.
    procs = [{"pid": i, "name": f"proc-{i}", "rss_kb": i * 7} for i in range(4000)]
    payload = json.dumps(procs)
    assert len(payload) > 16384  # would have been cut before

    out = _run_with_stdout(monkeypatch, payload)

    assert out == payload  # complete and verbatim — no truncation marker
    assert json.loads(out) == procs  # still valid JSON the model can parse


def test_over_cap_output_is_marked_incomplete(monkeypatch):
    huge = "x" * (_AGENT_TOOL_OUTPUT_CAP + 5000)
    out = _run_with_stdout(monkeypatch, huge)

    assert len(out) > _AGENT_TOOL_OUTPUT_CAP
    assert out.startswith("x" * 100)
    assert "truncated" in out and "incomplete" in out  # the model is told it's cut
