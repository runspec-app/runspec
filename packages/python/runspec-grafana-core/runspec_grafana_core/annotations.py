"""annotations.py — read and create annotations (``/api/annotations``).

Annotations mark events on dashboards: deploys, incidents, alert state changes.
Grafana stores times as epoch milliseconds; these helpers take the same time
forms as the query verbs and return RFC 3339.
"""

from __future__ import annotations

import time as _time
from typing import Any

from runspec_grafana_core.client import GrafanaConfig, GrafanaError, drop_empty, grafana_request, iso, parse_time, preview, truncate, window

ANNOTATION_TYPES = ("annotation", "alert")


def _ms(ts: float | None) -> int | None:
    return None if ts is None else int(ts * 1000)


def get_annotations(
    config: GrafanaConfig,
    start: str | None = None,
    end: str | None = None,
    dashboard_uid: str | None = None,
    panel_id: int | None = None,
    tags: list[str] | None = None,
    match_any: bool = False,
    type: str | None = None,
    limit: int | None = 100,
) -> dict[str, Any]:
    """Annotations in ``start``..``end`` (default the last 24 hours), newest first.

    ``tags`` must all match unless ``match_any``; ``type`` is ``annotation``
    (user-made) or ``alert`` (alert state changes). Text is cut to 300 chars
    (``text_chars`` gives the full length).
    """
    if type and type not in ANNOTATION_TYPES:
        raise GrafanaError(f"type must be one of {', '.join(ANNOTATION_TYPES)}")
    start_ts, end_ts = window(start, end, default_lookback=86400.0)
    params = {
        "from": _ms(start_ts),
        "to": _ms(end_ts),
        "dashboardUID": dashboard_uid,
        "panelId": panel_id,
        "tags": list(tags or []),
        "matchAny": True if match_any else None,
        "type": type,
        "limit": limit + 1 if limit and limit > 0 else 5000,
    }
    items = [a for a in grafana_request("GET", "/api/annotations", config=config, params=params) or [] if isinstance(a, dict)]
    kept, cut = truncate(items, limit)
    rows = []
    for a in kept:
        text, full = preview(a.get("text"), 300)
        rows.append(
            drop_empty(
                {
                    "id": a.get("id"),
                    "time": iso(a["time"] / 1000) if a.get("time") else None,
                    "time_end": iso(a["timeEnd"] / 1000) if a.get("timeEnd") and a.get("timeEnd") != a.get("time") else None,
                    "text": text,
                    "text_chars": full,
                    "tags": a.get("tags"),
                    "dashboard_uid": a.get("dashboardUID"),
                    "panel_id": a.get("panelId") or None,
                    "alert_name": a.get("alertName"),
                    "new_state": a.get("newState"),
                    "by": a.get("login") or a.get("email"),
                }
            )
        )
    return {"start": iso(start_ts), "end": iso(end_ts), "count": len(rows), "truncated": cut, "annotations": rows}


def get_annotation_tags(config: GrafanaConfig, tag: str | None = None, limit: int | None = 100) -> dict[str, Any]:
    """Annotation tags in use, with how many annotations carry each; ``tag`` filters by prefix."""
    body = grafana_request("GET", "/api/annotations/tags", config=config, params={"tag": tag, "limit": limit if limit and limit > 0 else None}) or {}
    tags = ((body.get("result") or {}) if isinstance(body, dict) else {}).get("tags") or []
    rows = [{"tag": t.get("tag"), "count": t.get("count")} for t in tags if isinstance(t, dict)]
    rows.sort(key=lambda r: r["count"] or 0, reverse=True)
    return {"count": len(rows), "tags": rows}


def create_annotation(
    config: GrafanaConfig,
    text: str,
    tags: list[str] | None = None,
    dashboard_uid: str | None = None,
    panel_id: int | None = None,
    at: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Create an annotation at ``at`` (default now), or a region up to ``end``.

    Without ``dashboard_uid`` it is an organization-wide annotation, shown on
    any dashboard with a tag-filtered annotation query. Returns ``{id, time, …}``.
    """
    if not text.strip():
        raise GrafanaError("Annotation text is empty")
    if panel_id is not None and not dashboard_uid:
        raise GrafanaError("panel_id needs dashboard_uid")
    now = _time.time()
    at_ts = parse_time(at, now=now)
    start_ts = at_ts if at_ts is not None else now
    end_ts = parse_time(end, now=now)
    if end_ts is not None and end_ts < start_ts:
        raise GrafanaError(f"end ({iso(end_ts)}) is before the annotation time ({iso(start_ts)})")
    body = drop_empty(
        {
            "text": text,
            "tags": list(tags or []),
            "dashboardUID": dashboard_uid,
            "panelId": panel_id,
            "time": _ms(start_ts),
            "timeEnd": _ms(end_ts),
        }
    )
    result = grafana_request("POST", "/api/annotations", config=config, json_body=body) or {}
    return drop_empty(
        {
            "id": result.get("id") if isinstance(result, dict) else None,
            "time": iso(start_ts),
            "time_end": iso(end_ts),
            "dashboard_uid": dashboard_uid,
            "panel_id": panel_id,
            "tags": list(tags or []),
            "message": result.get("message") if isinstance(result, dict) else None,
        }
    )
