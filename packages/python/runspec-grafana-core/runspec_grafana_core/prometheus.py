"""prometheus.py — PromQL queries and metric/label discovery against a Prometheus
datasource, through Grafana's datasource proxy (so only the Grafana token is needed).

``datasource`` takes a UID or name; when omitted the default Prometheus
datasource is used (or the only one). Range results come back summarised per
series (points, first, last, min, max) unless ``include_values`` asks for the
samples, because a matrix is the biggest thing a query can return.
"""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import quote

from runspec_grafana_core.client import GrafanaConfig, GrafanaError, datasource_request, iso, parse_duration, parse_time, truncate, window
from runspec_grafana_core.datasources import resolve_datasource

# Range queries with no explicit step aim for about this many points.
DEFAULT_RANGE_POINTS = 250


def _num(value: Any) -> float | str:
    """A sample value as a number; NaN/±Inf stay strings (not valid JSON numbers)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return number if math.isfinite(number) else str(value)


def compact_series(result_type: str | None, result: Any, *, include_values: bool = False) -> list[dict[str, Any]]:
    """Shape a Prometheus/Loki metric result (pure).

    vector → ``[{metric, time, value}]``; matrix → ``[{metric, points, first,
    last, min, max}]`` plus ``values`` (``[[time, value], …]``) with
    ``include_values``; scalar/string → ``[{time, value}]``.
    """
    if result_type in ("scalar", "string"):
        ts, value = (result or [None, None])[:2]
        return [{"time": iso(float(ts)) if ts is not None else None, "value": _num(value)}]
    rows: list[dict[str, Any]] = []
    for series in result or []:
        if not isinstance(series, dict):
            continue
        row: dict[str, Any] = {"metric": series.get("metric") or {}}
        if result_type == "vector":
            ts, value = (series.get("value") or [None, None])[:2]
            row.update(time=iso(float(ts)) if ts is not None else None, value=_num(value))
        else:
            samples = series.get("values") or []
            numbers = [n for n in (_num(v) for _, v in samples) if isinstance(n, float)]
            row["points"] = len(samples)
            if samples:
                row["first"] = [iso(float(samples[0][0])), _num(samples[0][1])]
                row["last"] = [iso(float(samples[-1][0])), _num(samples[-1][1])]
            if numbers:
                row.update(min=min(numbers), max=max(numbers))
            if include_values:
                row["values"] = [[iso(float(ts)), _num(v)] for ts, v in samples]
        rows.append(row)
    return rows


def _ds(ds: dict[str, Any]) -> dict[str, Any]:
    return {"uid": ds.get("uid"), "name": ds.get("name")}


def _envelope_notes(env: dict[str, Any]) -> dict[str, Any]:
    return {k: env[k] for k in ("warnings", "infos") if env.get(k)}


def auto_step(start: float, end: float) -> float:
    """A step giving about :data:`DEFAULT_RANGE_POINTS` points, at least 1 second."""
    return float(max(math.floor((end - start) / DEFAULT_RANGE_POINTS), 1))


def query_prometheus(
    config: GrafanaConfig,
    query: str,
    datasource: str | None = None,
    query_type: str = "instant",
    at: str | None = None,
    start: str | None = None,
    end: str | None = None,
    step: str | None = None,
    limit: int | None = 50,
    include_values: bool = False,
) -> dict[str, Any]:
    """Run PromQL against a Prometheus datasource.

    ``query_type="instant"`` evaluates at ``at`` (default now); ``"range"``
    evaluates over ``start``..``end`` (default the last hour) every ``step``
    (default ~250 points). Returns ``{datasource, result_type, count,
    truncated, result, …}`` with at most ``limit`` series (0 = all).
    """
    if query_type not in ("instant", "range"):
        raise GrafanaError(f"query_type must be 'instant' or 'range', not {query_type!r}")
    ds = resolve_datasource(config, datasource, "prometheus")
    uid = str(ds.get("uid"))
    meta: dict[str, Any] = {}
    if query_type == "instant":
        ts = parse_time(at)
        env = datasource_request(config, uid, "/api/v1/query", {"query": query, "time": ts})
        meta["time"] = iso(ts)
    else:
        start_ts, end_ts = window(start, end)
        step_s = parse_duration(step) if step else auto_step(start_ts, end_ts)
        env = datasource_request(config, uid, "/api/v1/query_range", {"query": query, "start": start_ts, "end": end_ts, "step": step_s})
        meta.update(start=iso(start_ts), end=iso(end_ts), step=step_s)
    data = env.get("data") or {}
    rows = compact_series(data.get("resultType"), data.get("result"), include_values=include_values)
    kept, cut = truncate(rows, limit)
    out: dict[str, Any] = {"datasource": _ds(ds), "result_type": data.get("resultType"), **{k: v for k, v in meta.items() if v is not None}}
    out.update(count=len(rows), truncated=cut, result=kept, **_envelope_notes(env))
    return out


def _selector_body(selector: str | None) -> str:
    text = (selector or "").strip()
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1].strip()
    return text


def histogram_query(metric: str, percentile: float, selector: str | None = None, by: list[str] | None = None, rate_interval: str = "5m") -> str:
    """The ``histogram_quantile`` PromQL for a classic histogram (pure).

    ``metric`` is the histogram's base name (a trailing ``_bucket`` is fine);
    ``percentile`` is 0–100; ``by`` keeps those labels apart.
    """
    if not 0 < percentile < 100:
        raise GrafanaError("percentile must be between 0 and 100 (e.g. 95 or 99.9)")
    if not re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]*", metric):
        raise GrafanaError(f"{metric!r} is not a metric name")
    parse_duration(rate_interval)
    base = metric[: -len("_bucket")] if metric.endswith("_bucket") else metric
    matchers = _selector_body(selector)
    series = f"{base}_bucket{{{matchers}}}" if matchers else f"{base}_bucket"
    grouping = ", ".join(["le", *[b for b in (by or []) if b != "le"]])
    return f"histogram_quantile({percentile / 100:g}, sum by ({grouping}) (rate({series}[{rate_interval}])))"


def query_prometheus_histogram(
    config: GrafanaConfig,
    metric: str,
    percentile: float = 95.0,
    selector: str | None = None,
    by: list[str] | None = None,
    rate_interval: str = "5m",
    datasource: str | None = None,
    query_type: str = "instant",
    at: str | None = None,
    start: str | None = None,
    end: str | None = None,
    step: str | None = None,
    limit: int | None = 50,
    include_values: bool = False,
) -> dict[str, Any]:
    """A percentile (p50/p95/p99 …) from a histogram metric — builds the
    ``histogram_quantile`` query (returned as ``query``) and runs it."""
    promql = histogram_query(metric, percentile, selector, by, rate_interval)
    out = query_prometheus(
        config,
        promql,
        datasource=datasource,
        query_type=query_type,
        at=at,
        start=start,
        end=end,
        step=step,
        limit=limit,
        include_values=include_values,
    )
    return {"query": promql, **out}


def _discovery(env: dict[str, Any], ds: dict[str, Any], limit: int | None, key: str = "result", **extra: Any) -> dict[str, Any]:
    items = list(env.get("data") or [])
    kept, cut = truncate(items, limit)
    return {"datasource": _ds(ds), **{k: v for k, v in extra.items() if v is not None}, "count": len(items), "truncated": cut, key: kept}


def _window_params(start: str | None, end: str | None) -> dict[str, Any]:
    return {"start": parse_time(start), "end": parse_time(end)}


def list_prometheus_metric_names(
    config: GrafanaConfig,
    datasource: str | None = None,
    regex: str | None = None,
    matches: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = 100,
) -> dict[str, Any]:
    """Metric names, optionally only those matching ``regex`` (searched, case-insensitive)
    or present on series matching ``matches`` selectors."""
    ds = resolve_datasource(config, datasource, "prometheus")
    env = datasource_request(config, str(ds.get("uid")), "/api/v1/label/__name__/values", {"match[]": list(matches or []), **_window_params(start, end)})
    if regex:
        try:
            pattern = re.compile(regex, re.IGNORECASE)
        except re.error as exc:
            raise GrafanaError(f"Bad regex {regex!r}: {exc}") from None
        env = {**env, "data": [n for n in env.get("data") or [] if pattern.search(n)]}
    return _discovery(env, ds, limit, key="metrics")


def list_prometheus_label_names(
    config: GrafanaConfig,
    datasource: str | None = None,
    matches: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = 100,
) -> dict[str, Any]:
    """Label names, optionally only those on series matching ``matches``."""
    ds = resolve_datasource(config, datasource, "prometheus")
    env = datasource_request(config, str(ds.get("uid")), "/api/v1/labels", {"match[]": list(matches or []), **_window_params(start, end)})
    return _discovery(env, ds, limit, key="labels")


def list_prometheus_label_values(
    config: GrafanaConfig,
    label: str,
    datasource: str | None = None,
    matches: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = 100,
) -> dict[str, Any]:
    """The values of one label (``job``, ``instance`` …), optionally filtered by ``matches``."""
    ds = resolve_datasource(config, datasource, "prometheus")
    path = f"/api/v1/label/{quote(label, safe='')}/values"
    env = datasource_request(config, str(ds.get("uid")), path, {"match[]": list(matches or []), **_window_params(start, end)})
    return _discovery(env, ds, limit, key="values", label=label)


def list_prometheus_metric_metadata(config: GrafanaConfig, datasource: str | None = None, metric: str | None = None, limit: int | None = 50) -> dict[str, Any]:
    """Type, help text and unit for scraped metrics: ``[{metric, type, help, unit?}]``."""
    ds = resolve_datasource(config, datasource, "prometheus")
    env = datasource_request(config, str(ds.get("uid")), "/api/v1/metadata", {"metric": metric})
    data = env.get("data") or {}
    rows = []
    for name in sorted(data):
        for entry in data[name] or [{}]:
            rows.append({"metric": name, **{k: v for k, v in entry.items() if v}})
            break  # targets usually agree; one entry per metric keeps it compact
    kept, cut = truncate(rows, limit)
    return {"datasource": _ds(ds), "count": len(rows), "truncated": cut, "metadata": kept}
