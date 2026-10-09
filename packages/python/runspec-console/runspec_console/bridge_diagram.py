"""bridge_diagram.py — DiagramMixin: the ``render_diagram`` agent tool.

Turns Mermaid text into a PNG in the session workspace, so the agent can attach
it somewhere that can't draw Mermaid itself (``confluence add-diagram --image``).
The drawing happens **in the console's own web view** (WebView2 on Windows),
with Mermaid bundled in the UI: nothing is installed or launched, and a fleet
host needs no browser. The tool dispatches ``runspec:render_diagram``
({request_id, kind, source, scale}); the UI renders it (``diagramRender.ts``)
and answers through :meth:`diagram_render_result` with the PNG as base64. The
tool waits for that answer, writes ``diagrams/<name>.png`` (plus the source as
``<name>.mmd``) into the workspace, and returns the absolute path.

Autonomous: it only writes inside the current session's workspace, like the
``workspace_*`` tools. Exposed by the console's MCP server too, so Claude Code
gets the same renderer.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import uuid
from typing import TYPE_CHECKING, Any

from .bridge_const import ToolCancelled, logger

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401

# How long to wait for the web view to answer. The first render loads the
# Mermaid chunk, so this is generous; a normal render takes well under a second.
_RENDER_TIMEOUT_S = 45.0
_MAX_SOURCE_CHARS = 100_000
_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_waiters_lock = threading.Lock()


def diagram_stem(name: str) -> str:
    """A safe file stem for a diagram name (``Order flow`` → ``order-flow``)."""
    stem = _NAME_RE.sub("-", (name or "").strip()).strip("-.").lower()
    return stem or "diagram"


class DiagramMixin:
    _window: Any
    _diagram_waiters: dict[str, tuple[threading.Event, dict[str, Any]]]

    # ── JS API ──

    def diagram_render_result(
        self, request_id: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        """The UI's answer to a ``runspec:render_diagram`` request. Unknown or
        late ids (the tool already timed out) are ignored."""
        with _waiters_lock:
            entry = self._waiters().pop(str(request_id), None)
        if entry is None:
            logger.debug("diagram_render_result for unknown request %s", request_id)
            return {"ok": False}
        event, holder = entry
        holder["result"] = (
            result if isinstance(result, dict) else {"ok": False, "error": "bad result"}
        )
        event.set()
        return {"ok": True}

    # ── tool schema + dispatch ──

    def _waiters(self) -> dict[str, tuple[threading.Event, dict[str, Any]]]:
        waiters = getattr(self, "_diagram_waiters", None)
        if waiters is None:
            waiters = {}
            self._diagram_waiters = waiters
        return waiters

    def _diagram_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "render_diagram",
                "description": (
                    "Draw a Mermaid diagram (flowchart, sequence, class, state, ER, "
                    "gantt, pie, mindmap, git graph …) to a PNG in the session "
                    "workspace, using the console's own renderer — nothing to "
                    "install. Returns the PNG's absolute path, size, and its "
                    "workspace-relative path. Use it before putting a diagram "
                    "somewhere that can't draw Mermaid itself, e.g. pass the path "
                    "as --image to confluence add-diagram (upload it first if that "
                    "runs on a remote host). A syntax error comes back as Mermaid's "
                    "message so you can fix the text and retry."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "description": (
                                "The Mermaid text with real line breaks, e.g. "
                                "'graph TD' then 'A-->B' on the next line."
                            ),
                        },
                        "name": {
                            "type": "string",
                            "description": (
                                "Diagram name; names the file (Order flow → "
                                "diagrams/order-flow.png). Re-using a name "
                                "overwrites that file."
                            ),
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["mermaid"],
                            "description": "Diagram language (only mermaid for now).",
                        },
                        "scale": {
                            "type": "number",
                            "description": "Pixel density, default 2 (sharp on high-DPI screens); 1–4.",
                        },
                    },
                    "required": ["source", "name"],
                },
            }
        ]

    def _run_diagram_tool(
        self,
        ti: dict[str, Any],
        chat_id: str = "",
        cancel_event: threading.Event | None = None,
    ) -> str:
        source = ti.get("source")
        if isinstance(source, list):  # tolerate a list of lines
            source = "\n".join(str(line) for line in source)
        source = str(source or "")
        if not source.strip():
            return "Error: 'source' is empty."
        if len(source) > _MAX_SOURCE_CHARS:
            return f"Error: the diagram source is over {_MAX_SOURCE_CHARS} characters."
        kind = str(ti.get("kind") or "mermaid").strip().lower()
        if kind != "mermaid":
            return f"Error: can't draw {kind} diagrams — only mermaid."
        if getattr(self, "_window", None) is None:
            return "Error: render_diagram needs the console window, which isn't open."
        stem = diagram_stem(str(ti.get("name") or ""))
        try:
            scale = float(ti.get("scale") or 2)
        except (TypeError, ValueError):
            scale = 2.0

        request_id = uuid.uuid4().hex
        event = threading.Event()
        holder: dict[str, Any] = {}
        with _waiters_lock:
            self._waiters()[request_id] = (event, holder)
        try:
            self._dispatch(  # type: ignore[attr-defined]
                "runspec:render_diagram",
                {
                    "request_id": request_id,
                    "kind": kind,
                    "source": source,
                    "scale": scale,
                },
            )
            waited = 0.0
            while not event.wait(0.25):
                waited += 0.25
                if cancel_event is not None and cancel_event.is_set():
                    raise ToolCancelled()
                if waited >= _RENDER_TIMEOUT_S:
                    return (
                        "Error: the console window didn't answer the render request "
                        f"within {int(_RENDER_TIMEOUT_S)}s."
                    )
        finally:
            with _waiters_lock:
                self._waiters().pop(request_id, None)

        result = holder.get("result") or {}
        if not result.get("ok"):
            return f"Error: Mermaid couldn't draw the diagram: {result.get('error') or 'unknown error'}"
        try:
            png = base64.b64decode(str(result.get("png_base64") or ""), validate=True)
        except (ValueError, TypeError):
            png = b""
        if not png.startswith(_PNG_MAGIC):
            return "Error: the renderer returned something that isn't a PNG."

        root = self._current_workspace_root(chat_id)  # type: ignore[attr-defined]
        if root is None:
            return "Error: no active session workspace to save the diagram in."
        folder = root / "diagrams"
        folder.mkdir(parents=True, exist_ok=True)
        png_path = folder / f"{stem}.png"
        png_path.write_bytes(png)
        (folder / f"{stem}.mmd").write_text(source, encoding="utf-8")
        return json.dumps(
            {
                "path": str(png_path),
                "workspace_path": f"diagrams/{stem}.png",
                "source_path": f"diagrams/{stem}.mmd",
                "width": result.get("width"),
                "height": result.get("height"),
                "bytes": len(png),
            }
        )
