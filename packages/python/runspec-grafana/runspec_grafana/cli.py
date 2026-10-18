"""cli.py — the ``grafana`` entry point.

One binary, a verb per Grafana MCP read tool. ``rs.parse("grafana")`` resolves
the subcommand and its args; we build a :class:`GrafanaConfig` from the
environment (scoped by the global ``--env``, with ``--url`` / ``--org-id`` /
the auth args overriding when the caller set them — see :func:`_chosen`), unpack each ``.value`` at the boundary, and
call the matching :mod:`runspec_grafana_core` helper.

Agents, pipes, the console's Forms tab and schedulers get compact JSON; a
person at a terminal gets readable text (:mod:`runspec_grafana.render`).
Failures (:class:`GrafanaError` and arg-shape ``ValueError``s) become a JSON
``{"error": ...}`` payload + a non-zero exit.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import runspec as rs
from runspec_grafana_core import (
    GrafanaConfig,
    GrafanaError,
    api_get,
    check_datasources,
    config_from_env,
    create_annotation,
    get_alert_rule,
    get_annotation_tags,
    get_annotations,
    get_dashboard,
    get_datasource,
    health,
    list_alert_rules,
    list_contact_points,
    list_dashboard_versions,
    list_datasources,
    list_loki_label_names,
    list_loki_label_values,
    list_prometheus_label_names,
    list_prometheus_label_values,
    list_prometheus_metric_metadata,
    list_prometheus_metric_names,
    list_silences,
    query_loki_logs,
    query_loki_patterns,
    query_loki_stats,
    query_prometheus,
    query_prometheus_histogram,
    search_dashboards,
    search_folders,
    whoami,
)

from runspec_grafana.render import human_text


def _chosen(arg: rs.Arg) -> Any:
    """The arg's value when the caller chose it: typed on the command line, or
    set through the automatic ``RUNSPEC_GRAFANA_ARG_*`` key (the console's
    secret channel). A value runspec read from one of the declared ``GRAFANA_*``
    aliases is left to :func:`config_from_env`, which reads the same variables
    but puts ``GRAFANA_<ENV>_*`` first when ``--env`` is set."""
    if arg.source == "cli":
        return arg.value
    auto_key = "RUNSPEC_GRAFANA_ARG_" + arg.name.upper().replace("-", "_")
    return arg.value if os.environ.get(auto_key) is not None else None


def _emit(payload: Any, *, text: str | None = None) -> None:
    print(text if text is not None else json.dumps(payload, default=str))


def _list(arg: rs.Arg) -> list[str]:
    return list(arg.value) if arg.value else []


def _dispatch(spec: rs.RunSpec, config: GrafanaConfig) -> Any:
    """Run the active verb against ``config``, unpacking ``spec.<arg>.value`` for
    only the args the verb declares (globals + its own)."""
    command = spec.runspec_command

    # ── identity / dashboards ──
    if command == "whoami":
        return whoami(config)
    if command == "health":
        return health(config)
    if command == "search-dashboards":
        return search_dashboards(
            config,
            query=spec.query.value,
            tags=_list(spec.tag),
            folder_uids=_list(spec.folder_uid),
            starred=spec.starred.value,
            limit=spec.limit.value,
        )
    if command == "search-folders":
        return search_folders(config, query=spec.query.value, limit=spec.limit.value)
    if command == "get-dashboard":
        return get_dashboard(
            config,
            spec.uid.value,
            version=spec.version.value,
            include=_list(spec.include),
            panel_id=spec.panel.value,
            raw=spec.raw.value,
        )
    if command == "list-dashboard-versions":
        return list_dashboard_versions(config, spec.uid.value, limit=spec.limit.value)

    # ── datasources ──
    if command == "list-datasources":
        return list_datasources(config, ds_type=spec.type.value, limit=spec.limit.value)
    if command == "get-datasource":
        return get_datasource(config, spec.datasource.value)
    if command == "check-datasource":
        return check_datasources(config, refs=_list(spec.datasource), limit=spec.limit.value)

    # ── Prometheus ──
    if command == "query-prometheus":
        return query_prometheus(
            config,
            spec.query.value,
            datasource=spec.datasource.value,
            query_type=spec.query_type.value,
            at=spec.time.value,
            start=spec.start.value,
            end=spec.end.value,
            step=spec.step.value,
            limit=spec.limit.value,
            include_values=spec.include_values.value,
        )
    if command == "query-prometheus-histogram":
        return query_prometheus_histogram(
            config,
            spec.metric.value,
            percentile=spec.percentile.value,
            selector=spec.selector.value,
            by=_list(spec.by),
            rate_interval=spec.rate_interval.value,
            datasource=spec.datasource.value,
            query_type=spec.query_type.value,
            at=spec.time.value,
            start=spec.start.value,
            end=spec.end.value,
            step=spec.step.value,
            limit=spec.limit.value,
            include_values=spec.include_values.value,
        )
    if command == "list-prometheus-metric-names":
        return list_prometheus_metric_names(
            config,
            datasource=spec.datasource.value,
            regex=spec.regex.value,
            matches=_list(spec.match),
            start=spec.start.value,
            end=spec.end.value,
            limit=spec.limit.value,
        )
    if command == "list-prometheus-label-names":
        return list_prometheus_label_names(config, datasource=spec.datasource.value, matches=_list(spec.match), start=spec.start.value, end=spec.end.value, limit=spec.limit.value)
    if command == "list-prometheus-label-values":
        return list_prometheus_label_values(
            config,
            spec.label.value,
            datasource=spec.datasource.value,
            matches=_list(spec.match),
            start=spec.start.value,
            end=spec.end.value,
            limit=spec.limit.value,
        )
    if command == "list-prometheus-metric-metadata":
        return list_prometheus_metric_metadata(config, datasource=spec.datasource.value, metric=spec.metric.value, limit=spec.limit.value)

    # ── Loki ──
    if command == "query-loki-logs":
        return query_loki_logs(
            config,
            spec.query.value,
            datasource=spec.datasource.value,
            start=spec.start.value,
            end=spec.end.value,
            limit=spec.limit.value,
            direction=spec.direction.value,
            max_line_chars=spec.max_line_chars.value,
            include_values=spec.include_values.value,
        )
    if command == "list-loki-label-names":
        return list_loki_label_names(config, datasource=spec.datasource.value, query=spec.query.value, start=spec.start.value, end=spec.end.value, limit=spec.limit.value)
    if command == "list-loki-label-values":
        return list_loki_label_values(
            config,
            spec.label.value,
            datasource=spec.datasource.value,
            query=spec.query.value,
            start=spec.start.value,
            end=spec.end.value,
            limit=spec.limit.value,
        )
    if command == "query-loki-stats":
        return query_loki_stats(config, spec.query.value, datasource=spec.datasource.value, start=spec.start.value, end=spec.end.value)
    if command == "query-loki-patterns":
        return query_loki_patterns(config, spec.query.value, datasource=spec.datasource.value, start=spec.start.value, end=spec.end.value, limit=spec.limit.value)

    # ── alerting ──
    if command == "list-alert-rules":
        return list_alert_rules(
            config,
            state=spec.state.value,
            search=spec.search.value,
            folder=spec.folder.value,
            datasource=spec.datasource.value,
            include=_list(spec.include),
            limit=spec.limit.value,
        )
    if command == "get-alert-rule":
        return get_alert_rule(config, spec.uid.value, raw=spec.raw.value)
    if command == "list-contact-points":
        return list_contact_points(config, name=spec.name.value, include_settings=spec.include_settings.value, limit=spec.limit.value)
    if command == "list-silences":
        return list_silences(config, state=spec.state.value, limit=spec.limit.value)

    # ── annotations ──
    if command == "get-annotations":
        return get_annotations(
            config,
            start=spec.start.value,
            end=spec.end.value,
            dashboard_uid=spec.dashboard_uid.value,
            panel_id=spec.panel.value,
            tags=_list(spec.tag),
            match_any=spec.match_any.value,
            type=spec.type.value,
            limit=spec.limit.value,
        )
    if command == "get-annotation-tags":
        return get_annotation_tags(config, tag=spec.tag.value, limit=spec.limit.value)
    if command == "create-annotation":
        return create_annotation(
            config,
            spec.text.value,
            tags=_list(spec.tag),
            dashboard_uid=spec.dashboard_uid.value,
            panel_id=spec.panel.value,
            at=spec.time.value,
            end=spec.end.value,
        )

    # ── escape hatch ──
    if command == "api-get":
        return api_get(config, spec.path.value, params=_list(spec.param))

    # require-command = true makes a bare invocation unreachable.
    raise SystemExit("no command — see `grafana --help`")  # pragma: no cover


def _wants_raw(spec: rs.RunSpec) -> bool:
    return "raw" in spec and bool(spec.raw.value)


def _terminal() -> bool:
    # runspec's logging replaces sys.stdout with a tee whose isatty() is always
    # False, so ask the real stdout.
    stream = sys.__stdout__
    return bool(stream and stream.isatty())


def main() -> None:
    spec = rs.parse("grafana")
    try:
        config = config_from_env(
            env=spec.env.value,
            url=_chosen(spec.url),
            token=_chosen(spec.grafana_token),
            org_id=_chosen(spec.org_id),
            username=_chosen(spec.grafana_user),
            password=_chosen(spec.grafana_password),
        )
        result = _dispatch(spec, config)
    except (GrafanaError, ValueError) as exc:
        _emit({"error": str(exc), "kind": type(exc).__name__})
        sys.exit(1)
    # A person at a terminal gets readable text; agents, pipes, the console's
    # Forms tab and schedulers get JSON.
    text = None
    if not spec.runspec_agent and _terminal() and not _wants_raw(spec) and spec.runspec_command != "api-get":
        text = human_text(result)
    _emit(result, text=text)


if __name__ == "__main__":
    main()
