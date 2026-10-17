"""dashboards.py — identity, search, and dashboards (``/api/user``, ``/api/search``, ``/api/dashboards``).

Dashboards come back as a compact summary by default — title, folder, tags,
variables, and one row per panel — because full dashboard JSON is large and
mostly layout. ``include=["queries"]`` adds each panel's queries, ``raw=True``
returns the API response untouched.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from runspec_grafana_core.client import GrafanaConfig, GrafanaError, drop_empty, grafana_request, preview, truncate

DASHBOARD_INCLUDE_OPTIONS = ("queries", "links")


def whoami(config: GrafanaConfig) -> dict[str, Any]:
    """The identity behind the credentials: login, email, name, server-admin
    flag, current org, and the orgs it can reach (with roles)."""
    user = grafana_request("GET", "/api/user", config=config) or {}
    try:
        orgs = grafana_request("GET", "/api/user/orgs", config=config) or []
    except GrafanaError:
        orgs = []  # service accounts can't list their orgs on some versions
    return drop_empty(
        {
            "login": user.get("login"),
            "name": user.get("name"),
            # A service account's email is just its login again.
            "email": user.get("email") if user.get("email") != user.get("login") else None,
            "is_grafana_admin": user.get("isGrafanaAdmin"),
            "org_id": user.get("orgId"),
            "orgs": [{"org_id": o.get("orgId"), "name": o.get("name"), "role": o.get("role")} for o in orgs if isinstance(o, dict)],
            "url": config.url,
        }
    )


def health(config: GrafanaConfig) -> dict[str, Any]:
    """Server health (``/api/health``): version, commit, and database status. Needs no auth."""
    return grafana_request("GET", "/api/health", config=config) or {}


def _search_row(config: GrafanaConfig, hit: dict[str, Any]) -> dict[str, Any]:
    return drop_empty(
        {
            "uid": hit.get("uid"),
            "title": hit.get("title"),
            "folder": hit.get("folderTitle"),
            "folder_uid": hit.get("folderUid"),
            "tags": hit.get("tags"),
            "starred": hit.get("isStarred") or None,
            "url": f"{config.url}{hit['url']}" if hit.get("url") else None,
        }
    )


def search_dashboards(
    config: GrafanaConfig,
    query: str | None = None,
    tags: list[str] | None = None,
    folder_uids: list[str] | None = None,
    starred: bool = False,
    limit: int | None = 50,
) -> dict[str, Any]:
    """Find dashboards by title text, tag (all must match), folder, or starred.

    Returns ``{count, truncated, dashboards: [{uid, title, folder, tags, url}]}``.
    """
    # Ask for one more than the limit so `truncated` is honest.
    params = {
        "type": "dash-db",
        "query": query,
        "tag": list(tags or []),
        "folderUIDs": list(folder_uids or []),
        "starred": True if starred else None,
        "limit": limit + 1 if limit and limit > 0 else 5000,
    }
    hits = grafana_request("GET", "/api/search", config=config, params=params) or []
    kept, cut = truncate(hits, limit)
    return {"count": len(kept), "truncated": cut, "dashboards": [_search_row(config, h) for h in kept]}


def search_folders(config: GrafanaConfig, query: str | None = None, limit: int | None = 50) -> dict[str, Any]:
    """Find folders by title text. Returns ``{count, truncated, folders: [{uid, title, folder?, url}]}``."""
    params = {"type": "dash-folder", "query": query, "limit": limit + 1 if limit and limit > 0 else 5000}
    hits = grafana_request("GET", "/api/search", config=config, params=params) or []
    kept, cut = truncate(hits, limit)
    return {"count": len(kept), "truncated": cut, "folders": [_search_row(config, h) for h in kept]}


# ── dashboards ─────────────────────────────────────────────────────────────


def _datasource_ref(value: Any) -> dict[str, Any] | str | None:
    if isinstance(value, dict):
        return drop_empty({"uid": value.get("uid"), "type": value.get("type")}) or None
    return value or None


def _flatten_panels(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    """Every panel, rows unpacked: collapsed rows keep theirs under ``panels``,
    legacy (schemaVersion <= 14) dashboards under ``rows[].panels``."""
    out: list[dict[str, Any]] = []

    def walk(panels: list[Any], row: str | None) -> None:
        for panel in panels or []:
            if not isinstance(panel, dict):
                continue
            if panel.get("type") == "row":
                walk(panel.get("panels") or [], panel.get("title"))
                continue
            out.append({**panel, "_row": row})

    walk(dashboard.get("panels") or [], None)
    for row in dashboard.get("rows") or []:
        if isinstance(row, dict):
            walk(row.get("panels") or [], row.get("title"))
    return out


def _target_text(target: dict[str, Any]) -> str | None:
    for key in ("expr", "query", "rawSql", "expression", "rawQuery", "queryText", "target"):
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def panel_queries(panel: dict[str, Any]) -> list[dict[str, Any]]:
    """A panel's queries: ``[{ref_id, query, datasource}]`` (``query`` empty
    for a visual-editor target with no text expression; ``hidden`` when off)."""
    panel_ds = _datasource_ref(panel.get("datasource"))
    rows = []
    for target in panel.get("targets") or []:
        if not isinstance(target, dict):
            continue
        rows.append(
            drop_empty(
                {
                    "ref_id": target.get("refId"),
                    "query": _target_text(target),
                    "datasource": _datasource_ref(target.get("datasource")) or panel_ds,
                    "hidden": target.get("hide") or None,
                }
            )
        )
    return rows


def dashboard_summary(
    config: GrafanaConfig,
    body: dict[str, Any],
    include: list[str] | None = None,
    panel_id: int | None = None,
) -> dict[str, Any]:
    """Compact view of a ``/api/dashboards/uid/{uid}`` response (pure — no I/O)."""
    include = list(include or [])
    dashboard = body.get("dashboard") or body.get("data") or {}
    meta = body.get("meta") or {}
    panels = _flatten_panels(dashboard)
    if panel_id is not None:
        panels = [p for p in panels if p.get("id") == panel_id]
        if not panels:
            raise GrafanaError(f"Dashboard {dashboard.get('uid')} has no panel with id {panel_id}")
    description, description_chars = preview(dashboard.get("description"), 300)
    rows = []
    for panel in panels:
        queries = panel_queries(panel)
        row: dict[str, Any] = {
            "id": panel.get("id"),
            "title": panel.get("title"),
            "type": panel.get("type"),
            "row": panel.get("_row"),
            "datasource": _datasource_ref(panel.get("datasource")),
            "query_count": len(queries),
        }
        if "queries" in include:
            row["queries"] = queries
        rows.append(drop_empty(row))
    variables = []
    for var in (dashboard.get("templating") or {}).get("list") or []:
        if not isinstance(var, dict):
            continue
        current = var.get("current") or {}
        query = var.get("query")
        variables.append(
            drop_empty(
                {
                    "name": var.get("name"),
                    "type": var.get("type"),
                    "label": var.get("label"),
                    "query": query.get("query") if isinstance(query, dict) else query,
                    "current": current.get("text") if isinstance(current, dict) else None,
                }
            )
        )
    time_range = dashboard.get("time") or {}
    out = drop_empty(
        {
            "uid": dashboard.get("uid"),
            "title": dashboard.get("title"),
            "description": description,
            "description_chars": description_chars,
            "tags": dashboard.get("tags"),
            "folder": meta.get("folderTitle"),
            "folder_uid": meta.get("folderUid"),
            "version": dashboard.get("version") or body.get("version"),
            "updated": meta.get("updated"),
            "updated_by": meta.get("updatedBy"),
            "url": f"{config.url}{meta['url']}" if meta.get("url") else None,
            "time": f"{time_range.get('from')} to {time_range.get('to')}" if time_range else None,
            "refresh": dashboard.get("refresh"),
            "variables": variables,
        }
    )
    out["panel_count"] = len(rows)
    out["panels"] = rows
    if "links" in include and dashboard.get("links"):
        out["links"] = [drop_empty({"title": link.get("title"), "url": link.get("url"), "type": link.get("type"), "tags": link.get("tags")}) for link in dashboard["links"] if isinstance(link, dict)]
    return out


def get_dashboard(
    config: GrafanaConfig,
    uid: str,
    version: int | None = None,
    include: list[str] | None = None,
    panel_id: int | None = None,
    raw: bool = False,
) -> dict[str, Any]:
    """A dashboard by UID (or one saved ``version`` of it).

    Default: :func:`dashboard_summary` — ``include`` adds ``queries`` (each
    panel's query text + datasource) and/or ``links``; ``panel_id`` narrows the
    panels to one. ``raw=True`` returns the API response untouched.
    """
    unknown = sorted(set(include or []) - set(DASHBOARD_INCLUDE_OPTIONS))
    if unknown:
        raise GrafanaError(f"Unknown include part(s): {', '.join(unknown)} — choose from {', '.join(DASHBOARD_INCLUDE_OPTIONS)}")
    path = f"/api/dashboards/uid/{quote(uid, safe='')}"
    meta: dict[str, Any] = {}
    if version is not None:
        body = grafana_request("GET", f"{path}/versions/{version}", config=config) or {}
        if not raw:
            # A saved version carries the dashboard under `data`; take folder/url from the current one.
            meta = (grafana_request("GET", path, config=config) or {}).get("meta") or {}
            body = {"dashboard": body.get("data") or {}, "meta": meta, "version": body.get("version")}
    else:
        body = grafana_request("GET", path, config=config) or {}
    if raw:
        return body
    return dashboard_summary(config, body, include=include, panel_id=panel_id)


def list_dashboard_versions(config: GrafanaConfig, uid: str, limit: int | None = 20) -> dict[str, Any]:
    """Saved versions of a dashboard, newest first: ``[{version, created, created_by, message}]``."""
    params = {"limit": limit + 1 if limit and limit > 0 else None}
    body = grafana_request("GET", f"/api/dashboards/uid/{quote(uid, safe='')}/versions", config=config, params=params)
    # Grafana 12 wraps the list as {"versions": [...], "continueToken": ...}.
    versions = body.get("versions") if isinstance(body, dict) else body
    versions = versions or []
    kept, cut = truncate(versions, limit)
    rows = [
        drop_empty(
            {
                "version": v.get("version"),
                "created": v.get("created"),
                "created_by": v.get("createdBy"),
                "message": v.get("message"),
            }
        )
        for v in kept
        if isinstance(v, dict)
    ]
    return {"uid": uid, "count": len(rows), "truncated": cut, "versions": rows}
