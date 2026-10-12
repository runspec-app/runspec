"""query.py — PromQL queries and series/label discovery (``/api/v1/query*``, ``/series``, ``/labels``)."""

from __future__ import annotations

import time
from typing import Any

from runspec_prometheus_core.client import (
    PrometheusConfig,
    PrometheusError,
    auto_step,
    iso,
    parse_duration,
    parse_time,
    prometheus_request,
    truncate,
)

# Range/exemplar windows default to the last hour when no start is given.
DEFAULT_LOOKBACK = 3600.0


def _shape(envelope: dict[str, Any], items: list[Any], limit: int | None, **extra: Any) -> dict[str, Any]:
    kept, truncated = truncate(items, limit)
    out: dict[str, Any] = {k: v for k, v in extra.items() if v is not None}
    out.update(count=len(items), truncated=truncated, result=kept)
    for key in ("warnings", "infos"):
        # The server's own limit notice is redundant with `truncated`.
        notes = [n for n in envelope.get(key) or [] if n != "results truncated due to limit"]
        if notes:
            out[key] = notes
    return out


def _window(start: str | float | None, end: str | float | None, *, now: float) -> tuple[float, float]:
    end_ts = parse_time(end, now=now)
    if end_ts is None:
        end_ts = now
    start_ts = parse_time(start, now=now)
    if start_ts is None:
        start_ts = end_ts - DEFAULT_LOOKBACK
    if start_ts >= end_ts:
        raise PrometheusError(f"start ({iso(start_ts)}) must be before end ({iso(end_ts)})")
    return start_ts, end_ts


def query(config: PrometheusConfig, promql: str, at: str | float | None = None, limit: int | None = 100) -> dict[str, Any]:
    """Instant query: one value per series at ``at`` (default now).

    Returns ``{result_type, result, count, truncated, time?, warnings?}``;
    ``result`` holds at most ``limit`` series (0 = all).
    """
    ts = parse_time(at)
    env = prometheus_request("POST", "/api/v1/query", config=config, params={"query": promql, "time": ts})
    data = env["data"] or {}
    result = data.get("result")
    # vector/matrix results are lists of series; a scalar/string is one [ts, value] pair.
    items: list[Any] = (result or []) if data.get("resultType") in ("vector", "matrix") else [result]
    return _shape(env, items, limit, result_type=data.get("resultType"), time=iso(ts))


def range_query(
    config: PrometheusConfig,
    promql: str,
    start: str | float | None = None,
    end: str | float | None = None,
    step: str | float | None = None,
    limit: int | None = 100,
) -> dict[str, Any]:
    """Range query over ``start``..``end`` (default the last hour up to now).

    ``step`` is a duration (``30s``, ``5m``) or seconds; when omitted one is
    chosen to give about 250 points. ``result`` holds at most ``limit`` series.
    """
    start_ts, end_ts = _window(start, end, now=time.time())
    if step is None or step == "":
        step_s = float(auto_step(start_ts, end_ts))
    elif isinstance(step, (int, float)):
        step_s = float(step)
    else:
        try:
            step_s = float(step)
        except ValueError:
            step_s = parse_duration(step)
    if step_s <= 0:
        raise PrometheusError("step must be a positive duration (e.g. 30s, 5m, 1h)")
    env = prometheus_request(
        "POST",
        "/api/v1/query_range",
        config=config,
        params={"query": promql, "start": start_ts, "end": end_ts, "step": step_s},
    )
    data = env["data"] or {}
    return _shape(env, data.get("result") or [], limit, result_type=data.get("resultType"), start=iso(start_ts), end=iso(end_ts), step=step_s)


def exemplar_query(
    config: PrometheusConfig,
    promql: str,
    start: str | float | None = None,
    end: str | float | None = None,
    limit: int | None = 100,
) -> dict[str, Any]:
    """Trace exemplars for the series selected by ``promql`` over the window
    (default the last hour). ``result`` holds at most ``limit`` series."""
    start_ts, end_ts = _window(start, end, now=time.time())
    env = prometheus_request("POST", "/api/v1/query_exemplars", config=config, params={"query": promql, "start": start_ts, "end": end_ts})
    return _shape(env, env["data"] or [], limit, start=iso(start_ts), end=iso(end_ts))


def _range_params(matches: list[str] | None, start: str | float | None, end: str | float | None, limit: int | None) -> dict[str, Any]:
    params: dict[str, Any] = {"match[]": list(matches or []), "start": parse_time(start), "end": parse_time(end)}
    # Ask the server to stop early too (Prometheus 2.49+ honours `limit`; older ignore it).
    if limit and limit > 0:
        params["limit"] = limit + 1
    return params


def series(
    config: PrometheusConfig,
    matches: list[str],
    start: str | float | None = None,
    end: str | float | None = None,
    limit: int | None = 100,
) -> dict[str, Any]:
    """Series (label sets) matching any of the ``matches`` selectors (at least one)."""
    if not matches:
        raise PrometheusError('series needs at least one match selector, e.g. up or {job="node"}')
    env = prometheus_request("POST", "/api/v1/series", config=config, params=_range_params(matches, start, end, limit))
    return _shape(env, env["data"] or [], limit)


def label_names(
    config: PrometheusConfig,
    matches: list[str] | None = None,
    start: str | float | None = None,
    end: str | float | None = None,
    limit: int | None = 100,
) -> dict[str, Any]:
    """Label names, optionally only those on series matching ``matches``."""
    env = prometheus_request("POST", "/api/v1/labels", config=config, params=_range_params(matches, start, end, limit))
    return _shape(env, env["data"] or [], limit)


def label_values(
    config: PrometheusConfig,
    label: str,
    matches: list[str] | None = None,
    start: str | float | None = None,
    end: str | float | None = None,
    limit: int | None = 100,
) -> dict[str, Any]:
    """Values of one label (e.g. ``job``), optionally only on series matching ``matches``."""
    if not label:
        raise PrometheusError("label_values needs a label name")
    env = prometheus_request("GET", f"/api/v1/label/{label}/values", config=config, params=_range_params(matches, start, end, limit))
    return _shape(env, env["data"] or [], limit, label=label)
