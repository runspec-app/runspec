"""alerting.py — alert rules with their live state, contact points, and silences.

Rule state comes from Grafana's Prometheus-compatible rules endpoint
(``/api/prometheus/grafana/api/v1/rules``, or a datasource's own rules with
``datasource``); a rule's full definition from the provisioning API.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from runspec_grafana_core.client import GrafanaConfig, GrafanaError, drop_empty, grafana_request, preview, truncate
from runspec_grafana_core.datasources import fetch_datasource

RULE_STATES = ("firing", "pending", "inactive", "error", "nodata")
RULE_INCLUDE_OPTIONS = ("alerts", "query", "annotations")
SILENCE_STATES = ("active", "pending", "expired", "any")


def _is_active(alert: dict[str, Any]) -> bool:
    # Grafana lists every instance, Normal ones included ("Normal",
    # "Normal (MissingSeries)"); Prometheus rulers list only pending/firing.
    return not str(alert.get("state") or "").lower().startswith(("normal", "inactive"))


def _rule_row(rule: dict[str, Any], group: dict[str, Any], include: list[str]) -> dict[str, Any]:
    alerts = [a for a in rule.get("alerts") or [] if isinstance(a, dict) and _is_active(a)]
    error, _ = preview(rule.get("lastError"), 300)
    row: dict[str, Any] = drop_empty(
        {
            "uid": rule.get("uid"),
            "name": rule.get("name"),
            "state": rule.get("state"),
            "health": rule.get("health"),
            "folder": group.get("file"),
            "group": group.get("name"),
            "labels": rule.get("labels"),
            "last_error": error,
            "last_evaluation": rule.get("lastEvaluation"),
        }
    )
    row["active_alerts"] = len(alerts)
    if "query" in include and rule.get("query"):
        row["query"] = rule["query"]
    if "annotations" in include and rule.get("annotations"):
        row["annotations"] = rule["annotations"]
    if "alerts" in include and alerts:
        row["alerts"] = [drop_empty({"labels": a.get("labels"), "state": a.get("state"), "active_at": a.get("activeAt"), "value": a.get("value")}) for a in alerts[:10]]
    return row


def list_alert_rules(
    config: GrafanaConfig,
    state: str | None = None,
    search: str | None = None,
    folder: str | None = None,
    datasource: str | None = None,
    include: list[str] | None = None,
    limit: int | None = 100,
) -> dict[str, Any]:
    """Alert rules and their current state, firing first.

    ``state`` keeps one of firing/pending/inactive/error/nodata (matched
    against state or health); ``search`` is a case-insensitive substring of
    the rule name; ``folder`` matches the folder title. ``datasource`` reads a
    Prometheus/Loki datasource's own (ruler) rules instead of Grafana-managed
    ones. ``include`` adds ``alerts`` (up to 10 active ones per rule), ``query``,
    ``annotations``.
    """
    include = list(include or [])
    unknown = sorted(set(include) - set(RULE_INCLUDE_OPTIONS))
    if unknown:
        raise GrafanaError(f"Unknown include part(s): {', '.join(unknown)} — choose from {', '.join(RULE_INCLUDE_OPTIONS)}")
    if state and state not in RULE_STATES:
        raise GrafanaError(f"state must be one of {', '.join(RULE_STATES)}")
    source = "grafana"
    if datasource:
        source = str(fetch_datasource(config, datasource).get("uid"))
    body = grafana_request("GET", f"/api/prometheus/{quote(source, safe='')}/api/v1/rules", config=config) or {}
    if isinstance(body, dict) and body.get("status") == "error":
        raise GrafanaError(f"Grafana rules: {body.get('error', 'unknown error')}")
    groups = ((body.get("data") or {}) if isinstance(body, dict) else {}).get("groups") or []
    rows = []
    for group in groups:
        if folder and (group.get("file") or "").lower() != folder.lower():
            continue
        for rule in group.get("rules") or []:
            if not isinstance(rule, dict) or rule.get("type", "alerting") != "alerting":
                continue
            if search and search.lower() not in (rule.get("name") or "").lower():
                continue
            if state and state not in ((rule.get("state") or "").lower(), (rule.get("health") or "").lower()):
                continue
            rows.append(_rule_row(rule, group, include))
    order = {"firing": 0, "pending": 1}
    rows.sort(key=lambda r: (order.get(str(r.get("state")).lower(), 2), r.get("folder") or "", r.get("name") or ""))
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row.get("state"))] = counts.get(str(row.get("state")), 0) + 1
    kept, cut = truncate(rows, limit)
    return {"count": len(rows), "by_state": counts, "truncated": cut, "rules": kept}


def get_alert_rule(config: GrafanaConfig, uid: str, raw: bool = False) -> dict[str, Any]:
    """A Grafana-managed alert rule's definition by UID: condition, queries,
    labels, annotations, pending period, no-data/error handling."""
    rule = grafana_request("GET", f"/api/v1/provisioning/alert-rules/{quote(uid, safe='')}", config=config) or {}
    if raw:
        return rule
    queries = []
    for item in rule.get("data") or []:
        model = item.get("model") or {}
        queries.append(
            drop_empty(
                {
                    "ref_id": item.get("refId"),
                    "datasource_uid": item.get("datasourceUid"),
                    "query": model.get("expr") or model.get("expression") or model.get("query"),
                    "type": model.get("type") if item.get("datasourceUid") == "__expr__" else None,
                    "range": item.get("relativeTimeRange"),
                }
            )
        )
    return drop_empty(
        {
            "uid": rule.get("uid"),
            "title": rule.get("title"),
            "folder_uid": rule.get("folderUID"),
            "rule_group": rule.get("ruleGroup"),
            "condition": rule.get("condition"),
            "for": rule.get("for"),
            "labels": rule.get("labels"),
            "annotations": rule.get("annotations"),
            "no_data_state": rule.get("noDataState"),
            "exec_err_state": rule.get("execErrState"),
            "is_paused": rule.get("isPaused") or None,
            "updated": rule.get("updated"),
            "queries": queries,
        }
    )


def list_contact_points(config: GrafanaConfig, name: str | None = None, include_settings: bool = False, limit: int | None = 100) -> dict[str, Any]:
    """Notification contact points: ``[{uid, name, type, settings?}]``. Grafana
    redacts secret settings (webhook URLs, keys) unless the token may read them."""
    items = [c for c in grafana_request("GET", "/api/v1/provisioning/contact-points", config=config, params={"name": name}) or [] if isinstance(c, dict)]
    rows = []
    for c in items:
        row = drop_empty({"uid": c.get("uid"), "name": c.get("name"), "type": c.get("type"), "disable_resolve_message": c.get("disableResolveMessage") or None})
        if include_settings and c.get("settings"):
            row["settings"] = c["settings"]
        rows.append(row)
    kept, cut = truncate(rows, limit)
    return {"count": len(rows), "truncated": cut, "contact_points": kept}


def _matchers(matchers: list[dict[str, Any]]) -> str:
    """Silence matchers as PromQL-style text: ``alertname="X", job=~"api.*"``."""
    ops = {(True, False): "=", (False, False): "!=", (True, True): "=~", (False, True): "!~"}
    out = []
    for m in matchers:
        op = ops[(m.get("isEqual") is not False, bool(m.get("isRegex")))]
        out.append(f'{m.get("name")}{op}"{m.get("value")}"')
    return ", ".join(out)


def list_silences(config: GrafanaConfig, state: str = "active", limit: int | None = 100) -> dict[str, Any]:
    """Alertmanager silences in Grafana's built-in Alertmanager:
    ``[{id, state, matchers, starts_at, ends_at, created_by, comment}]``."""
    if state not in SILENCE_STATES:
        raise GrafanaError(f"state must be one of {', '.join(SILENCE_STATES)}")
    items = [s for s in grafana_request("GET", "/api/alertmanager/grafana/api/v2/silences", config=config) or [] if isinstance(s, dict)]
    if state != "any":
        items = [s for s in items if (s.get("status") or {}).get("state") == state]
    items.sort(key=lambda s: s.get("endsAt") or "", reverse=True)
    rows = [
        drop_empty(
            {
                "id": s.get("id"),
                "state": (s.get("status") or {}).get("state"),
                "matchers": _matchers(s.get("matchers") or []),
                "starts_at": s.get("startsAt"),
                "ends_at": s.get("endsAt"),
                "created_by": s.get("createdBy"),
                "comment": preview(s.get("comment"), 300)[0],
            }
        )
        for s in items
    ]
    kept, cut = truncate(rows, limit)
    return {"count": len(rows), "truncated": cut, "silences": kept}
