"""Teams runnables (Microsoft Graph): teams-list-chats, teams-read-chat."""

from __future__ import annotations

import runspec as rs

from runspec_windows.graph._emit import run_graph
from runspec_windows.graph.client import graph_get


def _chat_title(chat: dict) -> str | None:
    """Best-effort display name for a chat (topic, else member names)."""
    if chat.get("topic"):
        return str(chat["topic"])
    members = chat.get("members") or []
    names = [m.get("displayName") for m in members if m.get("displayName")]
    return ", ".join(names) if names else None


def format_chats(data: dict) -> list[dict]:
    """Reshape a Graph ``/me/chats`` response into rows."""
    rows = []
    for chat in data.get("value", []):
        rows.append(
            {
                "id": chat.get("id"),
                "type": chat.get("chatType"),
                "topic": _chat_title(chat),
                "last_updated": chat.get("lastUpdatedDateTime"),
            }
        )
    return rows


def format_messages(data: dict) -> list[dict]:
    """Reshape a Graph chat ``messages`` response into rows."""
    rows = []
    for msg in data.get("value", []):
        sender = ((msg.get("from") or {}).get("user")) or {}
        body = (msg.get("body") or {}).get("content")
        rows.append(
            {
                "from": sender.get("displayName"),
                "created": msg.get("createdDateTime"),
                "type": msg.get("messageType"),
                "body": body,
            }
        )
    return rows


def main_teams_list_chats() -> None:
    spec = rs.parse("teams-list-chats")
    count = int(spec.count)
    run_graph(lambda: format_chats(graph_get("/me/chats", params={"$top": count, "$expand": "members", "$orderby": "lastMessagePreview/createdDateTime desc"})))


def main_teams_read_chat() -> None:
    spec = rs.parse("teams-read-chat")
    chat_id = str(spec.chat_id)
    count = int(spec.count)
    run_graph(lambda: format_messages(graph_get(f"/chats/{chat_id}/messages", params={"$top": count})))
