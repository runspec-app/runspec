"""loki.py — LogQL queries, label discovery, stream stats, and log patterns against
a Loki datasource, through Grafana's datasource proxy.

``datasource`` takes a UID or name; when omitted the default Loki datasource is
used (or the only one). Log lines come back flattened newest-first (or oldest
first with ``direction="forward"``), each cut to ``max_line_chars``, with the
stream labels listed once and referenced by index.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from runspec_grafana_core.client import GrafanaConfig, GrafanaError, datasource_request, iso, preview, truncate, window
from runspec_grafana_core.datasources import resolve_datasource
from runspec_grafana_core.prometheus import compact_series

# A log query with no start looks back this far.
DEFAULT_LOOKBACK = 3600.0


def _ns(ts: float) -> int:
    return int(ts * 1_000_000_000)


def _ds(ds: dict[str, Any]) -> dict[str, Any]:
    return {"uid": ds.get("uid"), "name": ds.get("name")}


def compact_streams(result: list[Any], *, direction: str = "backward", max_line_chars: int | None = 500) -> dict[str, Any]:
    """Flatten a Loki ``streams`` result (pure): ``{streams: [{labels, lines}],
    entries: [{time, stream, line, line_chars?}]}`` — ``stream`` indexes
    ``streams`` so repeated labels aren't repeated per line."""
    streams: list[dict[str, Any]] = []
    entries: list[tuple[int, dict[str, Any]]] = []
    for index, stream in enumerate(s for s in result or [] if isinstance(s, dict)):
        values = stream.get("values") or []
        streams.append({"labels": stream.get("stream") or {}, "lines": len(values)})
        for ts_ns, line, *_ in values:
            text, full = preview(line, max_line_chars)
            entry: dict[str, Any] = {"time": iso(int(ts_ns) / 1e9), "stream": index, "line": text}
            if full is not None:
                entry["line_chars"] = full
            entries.append((int(ts_ns), entry))
    entries.sort(key=lambda pair: pair[0], reverse=direction != "forward")
    return {"streams": streams, "entries": [e for _, e in entries]}


def query_loki_logs(
    config: GrafanaConfig,
    query: str,
    datasource: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = 50,
    direction: str = "backward",
    max_line_chars: int | None = 500,
    include_values: bool = False,
) -> dict[str, Any]:
    """Run a LogQL query over ``start``..``end`` (default the last hour).

    A log query returns ``{line_count, truncated, streams, entries}`` with at
    most ``limit`` lines; a metric query (``rate(...)``, ``count_over_time``)
    returns per-series summaries like :func:`query_prometheus`.
    """
    if direction not in ("backward", "forward"):
        raise GrafanaError(f"direction must be 'backward' or 'forward', not {direction!r}")
    ds = resolve_datasource(config, datasource, "loki")
    start_ts, end_ts = window(start, end, default_lookback=DEFAULT_LOOKBACK)
    # Ask Loki for one more line than the limit so `truncated` is honest.
    params = {
        "query": query,
        "start": _ns(start_ts),
        "end": _ns(end_ts),
        "limit": limit + 1 if limit and limit > 0 else 5000,
        "direction": direction,
    }
    env = datasource_request(config, str(ds.get("uid")), "/loki/api/v1/query_range", params)
    data = env.get("data") or {}
    result_type = data.get("resultType")
    out: dict[str, Any] = {"datasource": _ds(ds), "result_type": result_type, "start": iso(start_ts), "end": iso(end_ts)}
    if result_type == "streams":
        shaped = compact_streams(data.get("result") or [], direction=direction, max_line_chars=max_line_chars)
        kept, cut = truncate(shaped["entries"], limit)
        out.update(line_count=len(kept), truncated=cut, streams=shaped["streams"], entries=kept)
    else:
        rows = compact_series(result_type, data.get("result"), include_values=include_values)
        kept, cut = truncate(rows, limit)
        out.update(count=len(rows), truncated=cut, result=kept)
    if env.get("warnings"):
        out["warnings"] = env["warnings"]
    return out


def _window_ns(start: str | None, end: str | None) -> dict[str, Any]:
    start_ts, end_ts = window(start, end, default_lookback=DEFAULT_LOOKBACK)
    return {"start": _ns(start_ts), "end": _ns(end_ts)}


def list_loki_label_names(config: GrafanaConfig, datasource: str | None = None, query: str | None = None, start: str | None = None, end: str | None = None, limit: int | None = 100) -> dict[str, Any]:
    """Label names seen in the window (default last hour), optionally only on streams matching ``query``."""
    ds = resolve_datasource(config, datasource, "loki")
    env = datasource_request(config, str(ds.get("uid")), "/loki/api/v1/labels", {"query": query, **_window_ns(start, end)})
    items = list(env.get("data") or [])
    kept, cut = truncate(items, limit)
    return {"datasource": _ds(ds), "count": len(items), "truncated": cut, "labels": kept}


def list_loki_label_values(
    config: GrafanaConfig,
    label: str,
    datasource: str | None = None,
    query: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = 100,
) -> dict[str, Any]:
    """The values of one log label (``app``, ``namespace`` …) seen in the window."""
    ds = resolve_datasource(config, datasource, "loki")
    path = f"/loki/api/v1/label/{quote(label, safe='')}/values"
    env = datasource_request(config, str(ds.get("uid")), path, {"query": query, **_window_ns(start, end)})
    items = list(env.get("data") or [])
    kept, cut = truncate(items, limit)
    return {"datasource": _ds(ds), "label": label, "count": len(items), "truncated": cut, "values": kept}


def query_loki_stats(config: GrafanaConfig, query: str, datasource: str | None = None, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    """How much a stream selector covers in the window: ``{streams, chunks, entries, bytes}``
    — check this before a broad log query."""
    ds = resolve_datasource(config, datasource, "loki")
    env = datasource_request(config, str(ds.get("uid")), "/loki/api/v1/index/stats", {"query": query, **_window_ns(start, end)})
    stats = env.get("data") or {}
    return {"datasource": _ds(ds), "query": query, **(stats if isinstance(stats, dict) else {"stats": stats})}


def query_loki_patterns(
    config: GrafanaConfig,
    query: str,
    datasource: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = 20,
) -> dict[str, Any]:
    """Log patterns Loki detected for a stream selector, most frequent first:
    ``[{pattern, count, level?}]`` (needs Loki's pattern ingester)."""
    ds = resolve_datasource(config, datasource, "loki")
    try:
        env = datasource_request(config, str(ds.get("uid")), "/loki/api/v1/patterns", {"query": query, **_window_ns(start, end)})
    except GrafanaError as exc:
        if exc.status_code == 404:
            raise GrafanaError(f"{exc} — this Loki has no pattern ingester (pattern_ingester.enabled: true)", 404) from None
        raise
    rows = []
    for item in env.get("data") or []:
        if not isinstance(item, dict):
            continue
        count = sum(int(sample[1]) for sample in item.get("samples") or [] if len(sample) > 1)
        row: dict[str, Any] = {"pattern": item.get("pattern"), "count": count}
        if item.get("level"):
            row["level"] = item["level"]
        rows.append(row)
    rows.sort(key=lambda r: int(r["count"]), reverse=True)
    kept, cut = truncate(rows, limit)
    return {"datasource": _ds(ds), "count": len(rows), "truncated": cut, "patterns": kept}
