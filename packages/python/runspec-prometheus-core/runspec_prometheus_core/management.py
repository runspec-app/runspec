"""management.py — the ``/-/`` management endpoints and the TSDB admin API.

``reload`` and ``quit`` need Prometheus started with ``--web.enable-lifecycle``;
the TSDB admin calls need ``--web.enable-admin-api``. Errors name the flag.
"""

from __future__ import annotations

from typing import Any

from runspec_prometheus_core.client import PrometheusConfig, PrometheusError, iso, parse_time, prometheus_request


def healthy(config: PrometheusConfig) -> dict[str, Any]:
    """Liveness check (``/-/healthy``). Raises when Prometheus answers unhealthy."""
    return {"healthy": True, "message": prometheus_request("GET", "/-/healthy", config=config, raw=True).strip()}


def ready(config: PrometheusConfig) -> dict[str, Any]:
    """Readiness check (``/-/ready``): ready to serve queries. A not-ready server
    answers 503, reported as ``ready: false`` rather than raised."""
    try:
        text = prometheus_request("GET", "/-/ready", config=config, raw=True)
    except PrometheusError as exc:
        if exc.status_code == 503:
            return {"ready": False, "message": str(exc)}
        raise
    return {"ready": True, "message": text.strip()}


def reload(config: PrometheusConfig) -> dict[str, Any]:
    """Reload the configuration and rule files (``POST /-/reload``)."""
    text = prometheus_request("POST", "/-/reload", config=config, raw=True)
    return {"reloaded": True, "message": text.strip()}


def quit(config: PrometheusConfig) -> dict[str, Any]:  # noqa: A001 - mirrors the MCP tool name
    """Ask Prometheus to shut down gracefully (``POST /-/quit``)."""
    text = prometheus_request("POST", "/-/quit", config=config, raw=True)
    return {"quitting": True, "message": text.strip()}


def snapshot(config: PrometheusConfig, skip_head: bool = False) -> dict[str, Any]:
    """Snapshot all current data under ``<data-dir>/snapshots/``; returns its name.
    ``skip_head`` leaves out data still in the in-memory head block."""
    data = prometheus_request("POST", "/api/v1/admin/tsdb/snapshot", config=config, params={"skip_head": True if skip_head else None})["data"] or {}
    return {"snapshot": data.get("name"), **data}


def delete_series(config: PrometheusConfig, matches: list[str], start: str | float, end: str | float) -> dict[str, Any]:
    """Delete samples of the series matching ``matches`` between ``start`` and ``end``.

    Both bounds are required (without them Prometheus deletes across the whole
    retention window). Deletion leaves tombstones; :func:`clean_tombstones` or
    the next compaction frees the disk.
    """
    if not matches:
        raise PrometheusError("delete_series needs at least one match selector")
    start_ts, end_ts = parse_time(start), parse_time(end)
    if start_ts is None or end_ts is None:
        raise PrometheusError("delete_series needs both start and end, so it can't delete all data by accident")
    if start_ts >= end_ts:
        raise PrometheusError("start must be before end")
    prometheus_request("POST", "/api/v1/admin/tsdb/delete_series", config=config, params={"match[]": list(matches), "start": start_ts, "end": end_ts})
    return {"deleted": True, "matches": list(matches), "start": iso(start_ts), "end": iso(end_ts)}


def clean_tombstones(config: PrometheusConfig) -> dict[str, Any]:
    """Remove deleted data from disk and clear tombstones (I/O heavy)."""
    prometheus_request("POST", "/api/v1/admin/tsdb/clean_tombstones", config=config)
    return {"cleaned": True}
