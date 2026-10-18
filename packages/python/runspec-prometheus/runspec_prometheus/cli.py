"""cli.py — the ``prometheus`` entry point.

One binary, a verb per Prometheus MCP tool. ``rs.parse("prometheus")`` resolves
the subcommand and its args; we build a :class:`PrometheusConfig` from the
environment (scoped by the global ``--env``, with ``--url`` /
``--prometheus-token`` overriding), unpack each ``.value`` at the boundary, and
call the matching :mod:`runspec_prometheus_core` helper.

Failures (:class:`PrometheusError` and arg-shape ``ValueError``s) become a JSON
``{"error": ...}`` payload + a non-zero exit.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import runspec as rs
from runspec_prometheus_core import (
    PrometheusConfig,
    PrometheusError,
    alertmanagers,
    build_info,
    clean_tombstones,
    config_from_env,
    delete_series,
    exemplar_query,
    flags,
    get_config,
    healthy,
    label_names,
    label_values,
    list_alerts,
    list_rules,
    list_stores,
    list_targets,
    metric_metadata,
    query,
    quit,
    range_query,
    ready,
    reload,
    runtime_info,
    series,
    snapshot,
    targets_metadata,
    tsdb_stats,
    wal_replay_status,
)

# Verbs with no args of their own: verb → helper(config).
_NO_ARGS = {
    "alertmanagers": alertmanagers,
    "build-info": build_info,
    "runtime-info": runtime_info,
    "config": get_config,
    "flags": flags,
    "wal-replay-status": wal_replay_status,
    "list-stores": list_stores,
    "healthy": healthy,
    "ready": ready,
    "reload": reload,
    "clean-tombstones": clean_tombstones,
    "quit": quit,
}


def _chosen(arg: rs.Arg) -> Any:
    """The arg's value when the caller chose it: typed on the command line, or
    set through the automatic ``RUNSPEC_PROMETHEUS_ARG_*`` key (the console's
    secret channel). A value runspec read from one of the declared ``PROMETHEUS_*``
    aliases is left to :func:`config_from_env`, which reads the same variables
    but puts ``PROMETHEUS_<ENV>_*`` first when ``--env`` is set."""
    if arg.source == "cli":
        return arg.value
    auto_key = "RUNSPEC_PROMETHEUS_ARG_" + arg.name.upper().replace("-", "_")
    return arg.value if os.environ.get(auto_key) is not None else None


def _emit(payload: Any) -> None:
    print(json.dumps(payload, default=str))


def _list(arg: rs.Arg) -> list[str]:
    return list(arg.value) if arg.value else []


def _dispatch(spec: rs.RunSpec, config: PrometheusConfig) -> Any:
    """Run the active verb against ``config``, unpacking ``spec.<arg>.value`` for
    only the args the verb declares (globals + its own)."""
    command = spec.runspec_command

    if command in _NO_ARGS:
        return _NO_ARGS[command](config)
    if command == "query":
        return query(config, spec.query.value, at=spec.time.value, limit=spec.limit.value)
    if command == "range-query":
        return range_query(config, spec.query.value, start=spec.start.value, end=spec.end.value, step=spec.step.value, limit=spec.limit.value)
    if command == "exemplar-query":
        return exemplar_query(config, spec.query.value, start=spec.start.value, end=spec.end.value, limit=spec.limit.value)
    if command == "series":
        return series(config, _list(spec.match), start=spec.start.value, end=spec.end.value, limit=spec.limit.value)
    if command == "label-names":
        return label_names(config, _list(spec.match), start=spec.start.value, end=spec.end.value, limit=spec.limit.value)
    if command == "label-values":
        return label_values(config, spec.label.value, _list(spec.match), start=spec.start.value, end=spec.end.value, limit=spec.limit.value)
    if command == "metric-metadata":
        return metric_metadata(config, metric=spec.metric.value, limit=spec.limit.value)
    if command == "targets-metadata":
        return targets_metadata(config, match_target=spec.match_target.value, metric=spec.metric.value, limit=spec.limit.value)
    if command == "list-alerts":
        return list_alerts(config, state=spec.state.value)
    if command == "list-rules":
        return list_rules(
            config,
            rule_type=spec.rule_type.value,
            rule_names=_list(spec.rule_name),
            rule_groups=_list(spec.rule_group),
            exclude_alerts=spec.exclude_alerts.value,
        )
    if command == "list-targets":
        return list_targets(config, state=spec.state.value, scrape_pool=spec.scrape_pool.value, health=spec.health.value, limit=spec.limit.value)
    if command == "tsdb-stats":
        return tsdb_stats(config, limit=spec.limit.value)
    if command == "snapshot":
        return snapshot(config, skip_head=spec.skip_head.value)
    if command == "delete-series":
        return delete_series(config, _list(spec.match), start=spec.start.value, end=spec.end.value)

    # require-command = true makes a bare invocation unreachable.
    raise SystemExit("no command — see `prometheus --help`")  # pragma: no cover


def main() -> None:
    spec = rs.parse("prometheus")
    # runspec routes INFO logging to stdout; httpx logs every request at INFO,
    # which would put "HTTP Request: ..." ahead of the JSON result.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        config = config_from_env(
            env=spec.env.value,
            url=_chosen(spec.url),
            token=_chosen(spec.prometheus_token),
            username=_chosen(spec.prometheus_user),
            password=_chosen(spec.prometheus_password),
        )
        result = _dispatch(spec, config)
    except (PrometheusError, ValueError) as exc:
        _emit({"error": str(exc), "kind": type(exc).__name__})
        sys.exit(1)
    _emit(result)


if __name__ == "__main__":
    main()
