"""metadata.py — metric and target metadata (``/api/v1/metadata``, ``/api/v1/targets/metadata``)."""

from __future__ import annotations

from typing import Any

from runspec_prometheus_core.client import PrometheusConfig, prometheus_request


def metric_metadata(config: PrometheusConfig, metric: str | None = None, limit: int | None = 100) -> dict[str, Any]:
    """Type/help/unit for scraped metrics — one metric when ``metric`` is set,
    else up to ``limit`` metrics (0 = all). Returns ``{metadata, count}``."""
    params = {"metric": metric, "limit": limit if limit and limit > 0 else None}
    data = prometheus_request("GET", "/api/v1/metadata", config=config, params=params)["data"] or {}
    return {"count": len(data), "metadata": data}


def targets_metadata(
    config: PrometheusConfig,
    match_target: str | None = None,
    metric: str | None = None,
    limit: int | None = 100,
) -> dict[str, Any]:
    """Metric metadata as reported per scrape target. ``match_target`` is a
    label selector over targets (e.g. ``{job="node"}``); ``limit`` caps targets."""
    params = {"match_target": match_target, "metric": metric, "limit": limit if limit and limit > 0 else None}
    data = prometheus_request("GET", "/api/v1/targets/metadata", config=config, params=params)["data"] or []
    return {"count": len(data), "result": data}
