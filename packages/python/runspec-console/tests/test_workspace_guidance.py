"""
test_workspace_guidance.py — the standing session-workspace block.

PR #605 (0.261.0) wrote the "how to read a spill back / when to set `deliver` /
prefer workspace_json_get over run_python_sandboxed" guidance into
``_console_context()`` — but since 0.250.0 the chat turn folds ``_presence_caveat()``
instead, and headless turns fold only ``_base_system()``, so no agent turn ever
saw it (only the on-demand ``console_status`` tool did). The guidance now lives in
``_workspace_guidance()`` and rides every turn's system prompt.
"""

from __future__ import annotations

import threading
import types

import runspec_console.config as cfg
from runspec_console.bridge import Bridge
from runspec_console.bridge_const import WORKSPACE_GUIDANCE


def _guidance_stub(monkeypatch, *, spill_threshold=32_768):
    monkeypatch.setattr(
        cfg, "read_workspace", lambda: {"spill_threshold": spill_threshold}
    )
    ns = types.SimpleNamespace()
    ns._workspace_guidance = types.MethodType(Bridge._workspace_guidance, ns)
    return ns


def test_guidance_names_the_reading_tools_and_deliver():
    text = WORKSPACE_GUIDANCE
    assert "workspace_json_get" in text
    assert "workspace_materialize" in text
    assert "run_python_sandboxed" in text  # named as the *last* resort, not the first
    assert "`deliver`" in text and "`extract`" in text and "`digest`" in text
    # A JSON object is the case the line tools can't page — say so explicitly.
    assert "minified JSON object is one line" in text


def test_guidance_is_static_and_on_by_default(monkeypatch):
    ns = _guidance_stub(monkeypatch)
    assert ns._workspace_guidance() == WORKSPACE_GUIDANCE
    assert ns._workspace_guidance() == ns._workspace_guidance()  # cache-stable


def test_guidance_empty_when_spill_disabled(monkeypatch):
    # [workspace] spill_threshold = 0 → nothing ever spills → no advice about it.
    ns = _guidance_stub(monkeypatch, spill_threshold=0)
    assert ns._workspace_guidance() == ""


def test_guidance_empty_when_config_unreadable(monkeypatch):
    def boom():
        raise OSError("no config")

    monkeypatch.setattr(cfg, "read_workspace", boom)
    ns = types.SimpleNamespace()
    ns._workspace_guidance = types.MethodType(Bridge._workspace_guidance, ns)
    assert ns._workspace_guidance() == ""


# ── chat turn: rides the system prompt after base + date ──────────────────────


def _chat_stub(monkeypatch, guidance):
    from runspec_console import memory as memory_mod

    monkeypatch.setattr(memory_mod, "build_header", lambda: "")
    captured: dict = {}
    done = threading.Event()
    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._chat_cancels = {}
    s._ai_enabled = True
    s._now_context = lambda: "DATE"
    s._presence_caveat = lambda: "PRESENCE"
    s._workspace_guidance = lambda: guidance
    s._base_system = lambda: "BASE"
    s._chat_index_text = lambda: ""
    s._catalog_system_section = Bridge._catalog_system_section
    s._dispatch = lambda *a, **k: None

    def get_adapter(system_override=None):
        captured["system"] = system_override
        return object()

    s._get_adapter = get_adapter

    async def fake_turn(chat_id, message, adapter, system_parts=None, index_text=""):
        captured["system_parts"] = system_parts
        done.set()

    s._agentic_chat_turn = fake_turn
    s._spawn_chat_turn = types.MethodType(Bridge._spawn_chat_turn, s)
    return s, captured, done


def test_chat_turn_folds_workspace_guidance_into_system(monkeypatch):
    s, captured, done = _chat_stub(monkeypatch, "WS")
    s._spawn_chat_turn("hi")
    assert done.wait(2)
    # Order: base, date, workspace guidance, then the presence caveat.
    assert captured["system"] == "BASE\n\nDATE\n\nWS\n\nPRESENCE"
    # It is counted in the context gauge's "system" bucket (static prompt text).
    assert captured["system_parts"]["system"] == len("BASE") + len("DATE") + len("WS")


def test_chat_turn_omits_empty_guidance(monkeypatch):
    s, captured, done = _chat_stub(monkeypatch, "")
    s._spawn_chat_turn("hi")
    assert done.wait(2)
    assert captured["system"] == "BASE\n\nDATE\n\nPRESENCE"


# ── headless turn: same block, same reason ────────────────────────────────────


def test_headless_turn_folds_workspace_guidance(monkeypatch):
    ns = types.SimpleNamespace()
    ns._ai_enabled = True
    ns._now_context = lambda: "NOW"
    ns._workspace_guidance = lambda: "WS"
    ns._tool_mode = lambda: "dedup"
    ns._chat_index_text = lambda: ""
    ns._catalog_system_section = Bridge._catalog_system_section
    captured: dict = {}

    def get_adapter(system_override=None):
        captured["system"] = system_override
        raise RuntimeError("stop here")  # composed — no need to run the loop

    ns._get_adapter = get_adapter
    ns._trigger_run_agent_turn = types.MethodType(Bridge._trigger_run_agent_turn, ns)
    out = ns._trigger_run_agent_turn("card-1", "SYS", "do it", draft=False)
    assert out.startswith("⚠ Trigger could not run")
    assert captured["system"] == "NOW\n\nSYS\n\nWS"


# ── the sandbox tool no longer sells itself as the way to read a spill ────────


def test_run_python_description_defers_reading_to_workspace_tools(monkeypatch):
    from runspec_console import sandbox
    from runspec_console.bridge_agent_tools import AgentToolsMixin

    monkeypatch.setattr(sandbox, "available", lambda: True)
    ns = types.SimpleNamespace()
    ns._exec_tools = types.MethodType(AgentToolsMixin._exec_tools, ns)
    tools = {t["name"]: t for t in ns._exec_tools()}
    desc = tools["run_python_sandboxed"]["description"]
    assert "not merely to READ" in desc
    assert "workspace_json_get" in desc and "workspace_materialize" in desc
