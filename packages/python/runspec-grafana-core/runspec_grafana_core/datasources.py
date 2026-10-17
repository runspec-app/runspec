"""datasources.py — list, look up, resolve, and health-check datasources (``/api/datasources``)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from runspec_grafana_core.client import GrafanaConfig, GrafanaError, drop_empty, grafana_request, truncate

# Fields of a datasource record nobody asks for (or that only repeat others).
_NOISE = {"id", "orgId", "typeLogoUrl", "typeName", "version", "readOnly", "withCredentials", "basicAuthUser"}


def _row(ds: dict[str, Any]) -> dict[str, Any]:
    return drop_empty({"uid": ds.get("uid"), "name": ds.get("name"), "type": ds.get("type"), "is_default": ds.get("isDefault") or None})


def list_datasources(config: GrafanaConfig, ds_type: str | None = None, limit: int | None = 100) -> dict[str, Any]:
    """Configured datasources: ``{count, truncated, datasources: [{uid, name, type, is_default?}]}``.
    ``ds_type`` keeps one plugin type (``prometheus``, ``loki``, ``tempo`` …)."""
    items = [d for d in grafana_request("GET", "/api/datasources", config=config) or [] if isinstance(d, dict)]
    if ds_type:
        items = [d for d in items if (d.get("type") or "").lower() == ds_type.lower()]
    kept, cut = truncate(items, limit)
    return {"count": len(items), "truncated": cut, "datasources": [_row(d) for d in kept]}


def fetch_datasource(config: GrafanaConfig, ref: str) -> dict[str, Any]:
    """A datasource record by UID, falling back to its name."""
    try:
        return grafana_request("GET", f"/api/datasources/uid/{quote(ref, safe='')}", config=config) or {}
    except GrafanaError as exc:
        if exc.status_code not in (400, 404):
            raise
    try:
        return grafana_request("GET", f"/api/datasources/name/{quote(ref, safe='')}", config=config) or {}
    except GrafanaError as exc:
        if exc.status_code == 404:
            raise GrafanaError(f"No datasource with UID or name {ref!r} — see list-datasources", 404) from None
        raise


def get_datasource(config: GrafanaConfig, ref: str) -> dict[str, Any]:
    """One datasource by UID or name: its settings (``json_data``) and which
    secrets are set (``secure_fields``, names only — never values)."""
    ds = fetch_datasource(config, ref)
    out = {k: v for k, v in ds.items() if k not in _NOISE and k not in ("jsonData", "secureJsonFields", "isDefault", "basicAuth")}
    out["is_default"] = bool(ds.get("isDefault"))
    if ds.get("basicAuth"):
        out["basic_auth"] = True
    out["json_data"] = ds.get("jsonData")
    out["secure_fields"] = sorted(k for k, v in (ds.get("secureJsonFields") or {}).items() if v)
    return drop_empty(out)


def resolve_datasource(config: GrafanaConfig, ref: str | None, ds_type: str) -> dict[str, Any]:
    """The datasource to query: ``ref`` (UID or name) when given, else the
    default datasource of ``ds_type`` — or the only one of that type.

    Raises :class:`GrafanaError` naming the candidates when the choice is
    ambiguous or the datasource is the wrong type.
    """
    if ref:
        ds = fetch_datasource(config, ref)
        if (ds.get("type") or "").lower() != ds_type:
            raise GrafanaError(f"Datasource {ds.get('name')!r} is type {ds.get('type')!r}, not {ds_type!r}")
        return ds
    candidates = [d for d in grafana_request("GET", "/api/datasources", config=config) or [] if isinstance(d, dict) and (d.get("type") or "").lower() == ds_type]
    if not candidates:
        raise GrafanaError(f"No {ds_type} datasource in this Grafana — see list-datasources")
    defaults = [d for d in candidates if d.get("isDefault")]
    if len(candidates) == 1 or len(defaults) == 1:
        return (defaults or candidates)[0]
    names = ", ".join(f"{d.get('name')} ({d.get('uid')})" for d in candidates[:10])
    raise GrafanaError(f"Several {ds_type} datasources — pass --datasource with one of: {names}")


def check_datasources(config: GrafanaConfig, refs: list[str] | None = None, limit: int | None = 50) -> dict[str, Any]:
    """Run each datasource's health check (Grafana's "Save & test").

    ``refs`` are UIDs or names; when empty, every datasource (up to ``limit``).
    Returns ``{checked, failing, results: [{uid, name, type, status, message}]}``
    — a failing check is reported, not raised.
    """
    if refs:
        targets = [fetch_datasource(config, r) for r in refs]
        cut = False
    else:
        everything = [d for d in grafana_request("GET", "/api/datasources", config=config) or [] if isinstance(d, dict)]
        targets, cut = truncate(everything, limit)
    results = []
    for ds in targets:
        row = _row(ds)
        try:
            body = grafana_request("GET", f"/api/datasources/uid/{quote(str(ds.get('uid')), safe='')}/health", config=config) or {}
            row["status"] = body.get("status", "OK") if isinstance(body, dict) else "OK"
            message = body.get("message") if isinstance(body, dict) else None
        except GrafanaError as exc:
            row["status"] = "ERROR"
            message = str(exc)
        if message:
            row["message"] = message
        results.append(row)
    failing = sum(1 for r in results if str(r.get("status")).upper() != "OK")
    return {"checked": len(results), "failing": failing, "truncated": cut, "results": results}
