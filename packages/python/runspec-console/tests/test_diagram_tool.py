"""render_diagram: the agent tool that draws Mermaid to a PNG via the web view.

The UI side (diagramRender.ts) is stood in for by a fake ``_dispatch`` that
answers through ``diagram_render_result`` on another thread, as the real web
view does.
"""

from __future__ import annotations

import base64
import json
import threading

import pytest

from runspec_console import bridge_diagram
from runspec_console.bridge import Bridge
from runspec_console.bridge_const import (
    _BUILTIN_TOOL_NAMES,
    _DIAGRAM_TOOL_NAMES,
    _HOT_BUILTIN_NAMES,
    _MCP_SERVER_BUILTIN_NAMES,
    ToolCancelled,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"


class _Stub:
    diagram_render_result = Bridge.diagram_render_result
    _waiters = Bridge._waiters
    _diagram_tools = Bridge._diagram_tools
    _run_diagram_tool = Bridge._run_diagram_tool

    def __init__(self, root, answer=None):
        self._window = object()
        self._root = root
        self._answer = answer  # None ⇒ the UI never answers
        self.dispatched: list[tuple[str, dict]] = []

    def _current_workspace_root(self, chat_id=""):
        return self._root

    def _dispatch(self, event, detail):
        self.dispatched.append((event, detail))
        if self._answer is not None:
            result = self._answer(detail)
            threading.Thread(
                target=self.diagram_render_result, args=(detail["request_id"], result)
            ).start()


def _ok(_detail):
    return {
        "ok": True,
        "png_base64": base64.b64encode(PNG).decode(),
        "width": 400,
        "height": 300,
    }


def test_tool_is_a_deferred_autonomous_builtin_exposed_over_mcp():
    assert _DIAGRAM_TOOL_NAMES == {"render_diagram"}
    assert _DIAGRAM_TOOL_NAMES <= _BUILTIN_TOOL_NAMES
    assert _DIAGRAM_TOOL_NAMES <= _MCP_SERVER_BUILTIN_NAMES
    assert not _DIAGRAM_TOOL_NAMES & _HOT_BUILTIN_NAMES
    stub = type(
        "S", (), {"_effective_tool_autonomy": Bridge._effective_tool_autonomy}
    )()
    assert Bridge._effective_tool_autonomy(stub, "render_diagram", {}) == "autonomous"


def test_schema():
    (tool,) = _Stub(None)._diagram_tools()
    assert tool["name"] == "render_diagram"
    assert tool["input_schema"]["required"] == ["source", "name"]
    assert "add-diagram" in tool["description"]


def test_renders_into_workspace(tmp_path):
    stub = _Stub(tmp_path, _ok)
    out = json.loads(
        stub._run_diagram_tool({"source": ["graph TD", "A-->B"], "name": "Order flow"})
    )
    assert out["path"] == str(tmp_path / "diagrams" / "order-flow.png")
    assert out["workspace_path"] == "diagrams/order-flow.png"
    assert (out["width"], out["height"], out["bytes"]) == (400, 300, len(PNG))
    assert (tmp_path / "diagrams" / "order-flow.png").read_bytes() == PNG
    assert (tmp_path / "diagrams" / "order-flow.mmd").read_text() == "graph TD\nA-->B"
    ((event, detail),) = stub.dispatched
    assert event == "runspec:render_diagram"
    assert (
        detail["kind"] == "mermaid"
        and detail["source"] == "graph TD\nA-->B"
        and detail["scale"] == 2.0
    )
    assert stub._waiters() == {}  # no leftover waiter


def test_syntax_error_comes_back(tmp_path):
    stub = _Stub(tmp_path, lambda d: {"ok": False, "error": "Parse error on line 2"})
    out = stub._run_diagram_tool({"source": "graph TD\nA-->", "name": "x"})
    assert out == "Error: Mermaid couldn't draw the diagram: Parse error on line 2"
    assert not (tmp_path / "diagrams").exists()


def test_rejects_non_png(tmp_path):
    stub = _Stub(
        tmp_path,
        lambda d: {"ok": True, "png_base64": base64.b64encode(b"<svg/>").decode()},
    )
    assert "isn't a PNG" in stub._run_diagram_tool({"source": "graph TD", "name": "x"})


@pytest.mark.parametrize(
    ("ti", "msg"),
    [
        ({"source": " ", "name": "x"}, "empty"),
        ({"source": "a", "name": "x", "kind": "plantuml"}, "only mermaid"),
        ({"source": "x" * 100_001, "name": "x"}, "over 100000"),
    ],
)
def test_input_checks(tmp_path, ti, msg):
    stub = _Stub(tmp_path, _ok)
    assert msg in stub._run_diagram_tool(ti)
    assert stub.dispatched == []


def test_needs_the_window(tmp_path):
    stub = _Stub(tmp_path, _ok)
    stub._window = None
    assert "console window" in stub._run_diagram_tool(
        {"source": "graph TD", "name": "x"}
    )
    assert stub.dispatched == []


def test_times_out_and_cancels(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_diagram, "_RENDER_TIMEOUT_S", 0.5)
    stub = _Stub(tmp_path)  # never answers
    assert "didn't answer" in stub._run_diagram_tool(
        {"source": "graph TD", "name": "x"}
    )
    assert stub._waiters() == {}
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ToolCancelled):
        stub._run_diagram_tool({"source": "graph TD", "name": "x"}, cancel_event=cancel)


def test_late_or_unknown_answer_is_ignored():
    stub = _Stub(None)
    assert stub.diagram_render_result("nope", {"ok": True}) == {"ok": False}


def test_diagram_stem():
    assert bridge_diagram.diagram_stem("Order flow / v2") == "order-flow-v2"
    assert bridge_diagram.diagram_stem("  ") == "diagram"
