"""
trace.py — opt-in diagnostic trace of the agent's tool results and the messages
handed to the LLM adapter (i.e. what the console sends to the model / your proxy).

This exists to answer one question definitively: *what bytes does the model
actually receive?* When the trace is enabled, the agent loop appends one JSON
line per event to a file you can open — the full tool-result string and a
JSON-safe rendering of the outbound message list — so you can see whether the
data reached the request payload (console → proxy) intact, independent of how the
cards render it.

Enable it by either running the console with ``--debug`` or setting
``RUNSPEC_AGENT_TRACE=/path/to/trace.jsonl``. With ``--debug`` and no explicit
path it lands at ``{tempdir}/runspec_agent_trace.jsonl``. The writer is
best-effort and never raises into the agent loop.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def trace_path(debug: bool) -> str | None:
    """Resolve the trace file path, or ``None`` when tracing is off.

    ``RUNSPEC_AGENT_TRACE`` (explicit path) wins; otherwise a default under the
    OS temp dir is used only when ``debug`` is on. Returning ``None`` is the
    signal to skip — tracing is never on unless explicitly asked for."""
    explicit = os.environ.get("RUNSPEC_AGENT_TRACE", "").strip()
    if explicit:
        return explicit
    if debug:
        return str(Path(tempfile.gettempdir()) / "runspec_agent_trace.jsonl")
    return None


def jsonsafe_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render the adapter message list into plain JSON-serialisable form.

    Message ``content`` may hold provider SDK objects (e.g. Anthropic content
    blocks) that don't serialise; coerce anything unknown to ``str`` so the trace
    captures the real text without choking. Tool-result strings (the thing under
    investigation) pass through verbatim."""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            rendered: Any = content
        elif isinstance(content, list):
            rendered = [_coerce_block(b) for b in content]
        else:
            rendered = str(content)
        entry: dict[str, Any] = {"role": role, "content": rendered}
        if "tool_calls" in m:  # OpenAI assistant turn
            entry["tool_calls"] = [_coerce(tc) for tc in m["tool_calls"]]
        if "tool_call_id" in m:  # OpenAI tool turn
            entry["tool_call_id"] = m["tool_call_id"]
        out.append(entry)
    return out


def _coerce_block(block: Any) -> Any:
    """A content block is either an already-plain dict (Anthropic tool_result /
    tool_use) or an SDK object — keep dicts, stringify the rest."""
    if isinstance(block, dict):
        return {k: _coerce(v) for k, v in block.items()}
    return str(block)


def _coerce(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {k: _coerce(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce(v) for v in value]
    return str(value)


def write_trace(path: str, kind: str, data: dict[str, Any]) -> None:
    """Append one ``{ts, kind, ...data}`` JSON line. Best-effort: any I/O or
    serialisation failure is swallowed so diagnostics never break a chat turn."""
    try:
        record = {"ts": round(time.time(), 3), "kind": kind, **data}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
