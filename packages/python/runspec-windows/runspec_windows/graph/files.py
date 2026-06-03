"""OneDrive/SharePoint file runnables (Microsoft Graph): onedrive-recent, onedrive-search, onedrive-list."""

from __future__ import annotations

from urllib.parse import quote

import runspec as rs

from runspec_windows.graph._emit import run_graph
from runspec_windows.graph.client import graph_get

_ITEM_SELECT = "name,size,lastModifiedDateTime,webUrl,id,folder,file,parentReference"


def format_items(data: dict) -> list[dict]:
    """Reshape a Graph driveItem collection (``{"value": [...]}``) into rows."""
    rows = []
    for it in data.get("value", []):
        rows.append(
            {
                "name": it.get("name"),
                "is_folder": "folder" in it,
                "size": it.get("size"),
                "last_modified": it.get("lastModifiedDateTime"),
                "web_url": it.get("webUrl"),
                "item_id": it.get("id"),
                "path": (it.get("parentReference") or {}).get("path"),
            }
        )
    return rows


def main_onedrive_recent() -> None:
    spec = rs.parse("onedrive-recent")
    count = int(spec.count)
    run_graph(lambda: format_items(graph_get("/me/drive/recent", params={"$top": count})))


def main_onedrive_search() -> None:
    spec = rs.parse("onedrive-search")
    query = str(spec.query)
    count = int(spec.count)
    path = f"/me/drive/root/search(q='{quote(query)}')"
    run_graph(lambda: format_items(graph_get(path, params={"$top": count, "$select": _ITEM_SELECT})))


def main_onedrive_list() -> None:
    spec = rs.parse("onedrive-list")
    folder = str(spec.path).strip("/")
    path = f"/me/drive/root:/{quote(folder)}:/children" if folder else "/me/drive/root/children"
    run_graph(lambda: format_items(graph_get(path, params={"$select": _ITEM_SELECT})))
