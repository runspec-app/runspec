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

from runspec_console.bridge import Bridge
from tests.session_stub import install_sessions
from runspec_console.bridge_const import _AGENT_TOOL_OUTPUT_CAP


class _AgentBridge:
    _run_tool_sync = Bridge._run_tool_sync
    _finalize_tool_output = Bridge._finalize_tool_output
    _current_workspace_root = Bridge._current_workspace_root
    _tool_output_cap = Bridge._tool_output_cap
    # Routing helpers _run_tool_sync delegates to (per-host tool names resolve via
    # the host__name fallback under either tool_mode layout).
    _resolve_tool_target = Bridge._resolve_tool_target
    _fallback_for_host = Bridge._fallback_for_host
    _tool_mode = Bridge._tool_mode
    _dedup_tool_name = Bridge._dedup_tool_name
    _dedup_tool_aliases = Bridge._dedup_tool_aliases
    _host_selector_key = Bridge._host_selector_key
    _sanitize_tool_name = staticmethod(Bridge._sanitize_tool_name)
    _declared_arg_name_set = staticmethod(Bridge._declared_arg_name_set)
    _secret_arg_names = staticmethod(Bridge._secret_arg_names)
    _positionals_for = staticmethod(Bridge._positionals_for)
    _apply_credentials = Bridge._apply_credentials
    _credential_env = Bridge._credential_env
    _scoped_credentials = Bridge._scoped_credentials
    _scope_matches = staticmethod(Bridge._scope_matches)
    _has_scope = staticmethod(Bridge._has_scope)
    _declared_use = staticmethod(Bridge._declared_use)
    _declared_interface = Bridge._declared_interface
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

    monkeypatch.setattr("runspec_console.executor.run_local", fake_run_local)
    monkeypatch.setattr(
        "runspec_console.bridge_utils._paths", lambda _e: ["/opt/app/runspec"]
    )
    b = _AgentBridge(entry={"name": "local"}, cached=_cached("local"))
    b._current_workspace_root = lambda chat_id="": None  # no session workspace
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


# ── workspace spill ───────────────────────────────────────────────────────────


def _run_with_workspace(
    monkeypatch, tmp_path, line: str, conv_id: str = "convtest01"
) -> tuple[str, "object"]:
    """Same fake executor as _run_with_stdout, but the bridge carries a
    conversation id and the workspaces_root points at ``tmp_path`` — so a
    spilled output is written under it and we can inspect the on-disk file."""

    def fake_run_local(rp, runnable, args, command_path, on_line, on_done, **kw):
        on_line(line, "stdout")
        on_done(0, 1)

    monkeypatch.setattr("runspec_console.executor.run_local", fake_run_local)
    monkeypatch.setattr(
        "runspec_console.bridge_utils._paths", lambda _e: ["/opt/app/runspec"]
    )
    # Point workspaces_root + read_workspace at the tmp_path so no state
    # touches the real user config.
    monkeypatch.setattr("runspec_console.config.workspaces_root", lambda: tmp_path)
    monkeypatch.setattr(
        "runspec_console.config.workspace_dir",
        lambda sid: tmp_path / sid,
    )
    monkeypatch.setattr(
        "runspec_console.config.read_workspace",
        lambda: {"spill_threshold": 32_768, "headless_retention_days": 7},
    )
    b = _AgentBridge(entry={"name": "local"}, cached=_cached("local"))
    install_sessions(b, conv_id)
    return b._run_tool_sync("local__list-processes", {}), b


def test_output_above_threshold_spills_to_workspace(monkeypatch, tmp_path):
    """A 200 KB stdout with a workspace configured returns the short envelope
    naming the file, and the FULL raw output lands on disk uncapped."""
    payload = "x" * 200_000  # far above the 32 KB spill threshold
    out, bridge = _run_with_workspace(monkeypatch, tmp_path, payload)

    assert "Output spilled to workspace" in out  # envelope header
    assert "tool-results/" in out  # path pointer
    assert "workspace_read" in out  # reach for these next
    assert len(out) < len(payload)  # envelope is much smaller than the raw

    # The raw file on disk is uncapped and complete.
    ws = tmp_path / bridge._chat_session_id / "tool-results"
    files = list(ws.glob("*"))
    assert len(files) == 1
    assert files[0].read_text() == payload


def test_output_below_threshold_stays_inline(monkeypatch, tmp_path):
    """Small outputs are unchanged — the whole point of the threshold is to
    keep the fast path fast."""
    payload = "hello world"
    out, _bridge = _run_with_workspace(monkeypatch, tmp_path, payload)
    assert out == payload
    # No workspace directory created — nothing to spill.
    assert not list(tmp_path.glob("*/tool-results/*"))


def test_no_workspace_id_falls_back_to_truncation(monkeypatch, tmp_path):
    """When no session workspace is active (a stand-alone runnable run from
    the Console tab, or a first-turn tool call before persistence has minted
    an id), the pre-workspace cap-and-truncate behaviour still applies."""
    huge = "y" * (_AGENT_TOOL_OUTPUT_CAP + 5000)
    # Bridge with NO conversation id and NO workspace mapping.
    monkeypatch.setattr(
        "runspec_console.config.read_workspace",
        lambda: {"spill_threshold": 32_768, "headless_retention_days": 7},
    )
    out = _run_with_stdout(monkeypatch, huge)  # uses the plain _AgentBridge
    # Same as the pre-workspace test: truncated with the visible marker.
    assert "truncated" in out and "incomplete" in out
