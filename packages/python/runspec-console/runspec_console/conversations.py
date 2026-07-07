"""
conversations.py — persisted chat conversations (machine-local JSON store).

Each conversation is one ``{id}.json`` file under ``config.conversations_dir()``,
holding metadata + an ordered list of **turns** in a provider-agnostic transcript
shape (so the UI can render it and a resume can rebuild a clean text history
without depending on any provider's native message format — Anthropic/Bedrock
keep raw SDK objects in the live history, which don't serialize cleanly).

Storage is **machine-local** and **never synced** (transcripts can be sensitive
and are instance-specific). The store is bounded by the ``[chat_history]``
retention settings via :func:`prune`.

A turn::

    {
      "user": "what failed last night?",      # the operator's message
      "assistant": "Three jobs errored …",     # the agent's prose (all iterations)
      "tools": [{"name": "...", "input": {...}, "output": "..."}],
      "usage": {"input": 0, "output": 0, ...} | None,
      "model": "claude-opus-4-8",
      "ts": "2026-06-28T10:00:00+00:00",
    }

Pure and dependency-free (stdlib JSON) — unit-testable on any OS.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_TITLE_LEN = 60
_PREVIEW_LEN = 140


def new_id() -> str:
    """A fresh conversation id (hex, filesystem-safe)."""
    return uuid.uuid4().hex[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_id(conv_id: str) -> str:
    """Keep only filename-safe chars — conversation ids are hex from ``new_id``,
    but never trust an id straight into a path."""
    return "".join(c for c in str(conv_id) if c.isalnum() or c in "-_")


def _path(directory: Path, conv_id: str) -> Path:
    return directory / f"{_safe_id(conv_id)}.json"


def _title_from(turn: dict[str, Any]) -> str:
    text = str(turn.get("user", "")).strip().replace("\n", " ")
    if not text:
        return "New conversation"
    return text[:_TITLE_LEN] + "…" if len(text) > _TITLE_LEN else text


def load(directory: Path, conv_id: str) -> dict[str, Any] | None:
    """The full conversation record, or ``None`` if absent/unreadable."""
    p = _path(directory, conv_id)
    if not _safe_id(conv_id) or not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write(directory: Path, conv: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    p = _path(directory, conv["id"])
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(conv, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, p)  # atomic on POSIX + Windows


def append_turn(
    directory: Path,
    conv_id: str,
    turn: dict[str, Any],
    *,
    provider: str = "",
    model: str = "",
) -> dict[str, Any]:
    """Append ``turn`` to conversation ``conv_id`` (creating it on first turn),
    bumping ``updated``/``turn_count`` and setting a title from the first user
    message. Returns the updated record."""
    conv = load(directory, conv_id)
    if conv is None:
        conv = {
            "id": conv_id,
            "title": _title_from(turn),
            "created": _now(),
            "updated": _now(),
            "provider": provider,
            "model": model,
            "turns": [],
        }
    conv.setdefault("turns", []).append(turn)
    conv["updated"] = _now()
    conv["turn_count"] = len(conv["turns"])
    if provider:
        conv["provider"] = provider
    if model:
        conv["model"] = model
    if not str(conv.get("title", "")).strip():
        conv["title"] = _title_from(turn)
    _write(directory, conv)
    return conv


def list_meta(directory: Path) -> list[dict[str, Any]]:
    """Lightweight metadata for every saved conversation, newest first:
    ``{id, title, created, updated, turn_count, preview}``."""
    out: list[dict[str, Any]] = []
    if not directory.exists():
        return out
    for p in directory.glob("*.json"):
        try:
            conv = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(conv, dict) or not conv.get("id"):
            continue
        turns = conv.get("turns") or []
        last = turns[-1] if turns else {}
        preview = str(last.get("assistant") or last.get("user") or "").strip()
        preview = preview.replace("\n", " ")
        if len(preview) > _PREVIEW_LEN:
            preview = preview[:_PREVIEW_LEN] + "…"
        out.append(
            {
                "id": str(conv["id"]),
                "title": str(conv.get("title") or "")
                or _title_from(turns[0] if turns else {}),
                "created": str(conv.get("created", "")),
                "updated": str(conv.get("updated", "")),
                "turn_count": len(turns),
                "preview": preview,
            }
        )
    out.sort(key=lambda c: c.get("updated", ""), reverse=True)
    return out


def delete(
    directory: Path, conv_id: str, *, workspaces_root: Path | None = None
) -> None:
    """Delete a conversation and (when ``workspaces_root`` is given) the
    sibling per-session workspace so retention drops both together — one
    entity, one lifetime. The workspace argument is optional so this pure
    module keeps its zero-dep contract; callers passing in the root also get
    the workspace cleanup for free."""
    try:
        _path(directory, conv_id).unlink()
    except FileNotFoundError:
        pass
    if workspaces_root is not None:
        try:
            from . import workspaces

            ws = workspaces_root / workspaces._safe_id(conv_id)
            workspaces.delete_workspace(ws)
        except OSError:
            pass


def rename(directory: Path, conv_id: str, title: str) -> bool:
    conv = load(directory, conv_id)
    if conv is None:
        return False
    new_title = str(title).strip()
    if new_title:
        conv["title"] = new_title
        _write(directory, conv)
    return True


def split_for_compaction(
    turns: list[dict[str, Any]], *, keep_first: int = 1, keep_recent: int = 20
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a conversation's turns into ``(head, middle, recent)`` for resume-time
    compaction: ``head`` (the opening turns that frame the conversation) and
    ``recent`` (the latest turns) are kept verbatim; ``middle`` is the overflow a
    caller condenses into a note/summary.

    Returns an empty ``middle`` (and ``recent``) when there's nothing to condense —
    the conversation fits in ``keep_first + keep_recent`` turns, or ``keep_recent``
    is non-positive — so the caller uses every turn verbatim."""
    turns = list(turns or [])
    kf = max(0, keep_first)
    kr = keep_recent if keep_recent and keep_recent > 0 else 0
    if kr <= 0 or len(turns) <= kf + kr:
        return turns, [], []
    return turns[:kf], turns[kf : len(turns) - kr], turns[-kr:]


def turns_to_text(turns: list[dict[str, Any]]) -> str:
    """A plain-text rendering of turns (``User:`` / ``Assistant:`` lines) — input
    for a resume summary. Tool calls are noted compactly."""
    lines: list[str] = []
    for t in turns or []:
        user = str(t.get("user", "")).strip()
        if user:
            lines.append(f"User: {user}")
        assistant = str(t.get("assistant", "")).strip()
        if assistant:
            lines.append(f"Assistant: {assistant}")
        for tool in t.get("tools", []) or []:
            out = str(tool.get("output", ""))[:200]
            lines.append(f"  (ran {tool.get('name', 'tool')} → {out})")
    return "\n".join(lines)


def _parse_ts(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return None


def prune(
    directory: Path,
    *,
    max_conversations: int = 0,
    max_age_days: int = 0,
    workspaces_root: Path | None = None,
) -> int:
    """Enforce retention: drop conversations older than ``max_age_days`` and keep
    at most ``max_conversations`` (newest). A non-positive bound disables that
    check. When ``workspaces_root`` is given, each conversation's sibling
    workspace is dropped alongside it — symmetric lifetime. Returns the number
    removed."""
    removed = 0
    if max_age_days and max_age_days > 0:
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
        for m in list_meta(directory):
            ts = _parse_ts(m.get("updated", ""))
            if ts is not None and ts < cutoff:
                delete(directory, m["id"], workspaces_root=workspaces_root)
                removed += 1
    if max_conversations and max_conversations > 0:
        metas = list_meta(directory)
        for m in metas[max_conversations:]:
            delete(directory, m["id"], workspaces_root=workspaces_root)
            removed += 1
    return removed
