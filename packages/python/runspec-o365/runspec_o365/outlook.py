"""Outlook runnables (Microsoft Graph): outlook-unread, outlook-search, outlook-folders."""

from __future__ import annotations

import runspec as rs

from runspec_o365._emit import run_graph
from runspec_o365.client import graph_get

_MESSAGE_SELECT = "subject,from,receivedDateTime,isRead,bodyPreview,webLink"


def format_messages(data: dict) -> list[dict]:
    """Reshape a Graph messages response (``{"value": [...]}``) into rows."""
    rows = []
    for msg in data.get("value", []):
        sender = (msg.get("from") or {}).get("emailAddress") or {}
        rows.append(
            {
                "subject": msg.get("subject"),
                "from": sender.get("name"),
                "from_address": sender.get("address"),
                "received": msg.get("receivedDateTime"),
                "is_read": msg.get("isRead"),
                "preview": msg.get("bodyPreview"),
                "web_link": msg.get("webLink"),
            }
        )
    return rows


def format_folders(data: dict) -> list[dict]:
    """Reshape a Graph mailFolders response into rows with unread/total counts."""
    return [
        {
            "name": f.get("displayName"),
            "unread": f.get("unreadItemCount"),
            "total": f.get("totalItemCount"),
            "id": f.get("id"),
        }
        for f in data.get("value", [])
    ]


def main_outlook_unread() -> None:
    spec = rs.parse("outlook-unread")
    count = spec.count.value
    run_graph(
        lambda: format_messages(
            graph_get(
                "/me/mailFolders/inbox/messages",
                params={
                    "$filter": "isRead eq false",
                    "$top": count,
                    "$select": _MESSAGE_SELECT,
                    "$orderby": "receivedDateTime desc",
                },
            )
        )
    )


def main_outlook_search() -> None:
    spec = rs.parse("outlook-search")
    query = str(spec.query.value)
    count = spec.count.value
    run_graph(
        lambda: format_messages(
            graph_get(
                "/me/messages",
                params={"$search": f'"{query}"', "$top": count, "$select": _MESSAGE_SELECT},
            )
        )
    )


def main_outlook_folders() -> None:
    rs.parse("outlook-folders")
    run_graph(lambda: format_folders(graph_get("/me/mailFolders", params={"$top": 100})))
