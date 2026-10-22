"""cli.py — the ``pcap`` entry point.

One binary, a verb per tool. ``rs.parse("pcap")`` resolves the subcommand and
its args; we build a :class:`WiresharkConfig` from the globals, unpack each
``.value`` at the boundary, and call the matching :mod:`runspec_wireshark_core`
helper.

Agents, pipes, the console's Forms tab and schedulers get compact JSON; a
person at a terminal gets readable text (:mod:`runspec_wireshark.render`).
Failures (:class:`WiresharkError` and arg-shape ``ValueError``s) become a JSON
``{"error": ...}`` payload + a non-zero exit. A live capture and an object
export are recorded as audit events.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import runspec as rs
from runspec_wireshark_core import (
    WiresharkConfig,
    WiresharkError,
    capture,
    check,
    config_from_env,
    conversations,
    dns,
    endpoints,
    expert,
    export_objects,
    fields,
    file_info,
    follow,
    http,
    io,
    list_interfaces,
    merge,
    packet,
    packets,
    protocols,
    save_filtered,
    stats,
    summary,
    tls,
    top,
)

from runspec_wireshark.render import human_text


def _list(arg: rs.Arg) -> list[str]:
    return [str(v) for v in arg.value] if arg.value else []


def _file(spec: rs.RunSpec) -> str:
    return str(spec.file.value)


def _dispatch(spec: rs.RunSpec, config: WiresharkConfig) -> Any:
    """Run the active verb against ``config``, unpacking ``spec.<arg>.value`` for
    only the args the verb declares (globals + its own)."""
    command = spec.runspec_command

    # ── install / files ──
    if command == "check":
        return check(config)
    if command == "list-interfaces":
        return list_interfaces(config, all=spec.all.value)
    if command == "file-info":
        return file_info(config, _file(spec))
    if command == "summary":
        return summary(config, _file(spec), filter=spec.filter.value, top=spec.top.value)

    # ── packets ──
    if command == "packets":
        return packets(config, _file(spec), filter=spec.filter.value, limit=spec.limit.value, offset=spec.offset.value, absolute_time=spec.absolute_time.value)
    if command == "packet":
        return packet(config, _file(spec), spec.number.value, layers=_list(spec.layer), depth=spec.depth.value, include=_list(spec.include), raw=spec.raw.value)
    if command == "fields":
        return fields(config, _file(spec), _list(spec.field), filter=spec.filter.value, limit=spec.limit.value, offset=spec.offset.value, max_chars=spec.max_chars.value)
    if command == "top":
        return top(config, _file(spec), spec.field.value, filter=spec.filter.value, by=spec.by.value, limit=spec.limit.value)

    # ── statistics ──
    if command == "protocols":
        return protocols(config, _file(spec), filter=spec.filter.value, max_depth=spec.max_depth.value)
    if command == "conversations":
        return conversations(config, _file(spec), type=spec.type.value, filter=spec.filter.value, sort=spec.sort.value, limit=spec.limit.value)
    if command == "endpoints":
        return endpoints(config, _file(spec), type=spec.type.value, filter=spec.filter.value, limit=spec.limit.value)
    if command == "expert":
        return expert(config, _file(spec), severity=spec.severity.value, filter=spec.filter.value, limit=spec.limit.value)
    if command == "io":
        return io(config, _file(spec), interval=spec.interval.value, filter=spec.filter.value, limit=spec.limit.value)
    if command == "stats":
        return stats(config, _file(spec), spec.stat.value, max_lines=spec.max_lines.value)

    # ── streams / applications ──
    if command == "follow":
        return follow(config, _file(spec), protocol=spec.protocol.value, stream=spec.stream.value, max_bytes=spec.max_bytes.value)
    if command == "dns":
        return dns(config, _file(spec), filter=spec.filter.value, failed=spec.failed.value, limit=spec.limit.value)
    if command == "http":
        return http(config, _file(spec), filter=spec.filter.value, errors=spec.errors.value, slow_ms=spec.slow_ms.value, limit=spec.limit.value)
    if command == "tls":
        return tls(config, _file(spec), filter=spec.filter.value, failed=spec.failed.value, limit=spec.limit.value)

    # ── files ──
    if command == "capture":
        return _audited(
            "pcap.capture",
            {"interface": spec.interface.value},
            lambda: capture(
                config,
                spec.interface.value,
                duration=spec.duration.value,
                count=spec.count.value,
                capture_filter=spec.capture_filter.value,
                output=spec.output.value,
                directory=spec.capture_dir.value,
                max_mb=spec.max_mb.value,
                snaplen=spec.snaplen.value,
                overwrite=spec.overwrite.value,
            ),
            detail={"capture_filter": spec.capture_filter.value, "duration": spec.duration.value},
        )
    if command == "save-filtered":
        return save_filtered(config, _file(spec), spec.filter.value, spec.output.value, overwrite=spec.overwrite.value)
    if command == "merge":
        return merge(config, _list(spec.input), spec.output.value, append=spec.append.value, overwrite=spec.overwrite.value)
    if command == "export-objects":
        return _audited(
            "pcap.export_objects",
            {"file": _file(spec)},
            lambda: export_objects(config, _file(spec), spec.directory.value, protocol=spec.protocol.value, limit=spec.limit.value),
            detail={"protocol": spec.protocol.value, "directory": spec.directory.value},
        )

    # require-command = true makes a bare invocation unreachable.
    raise SystemExit("no command — see `pcap --help`")  # pragma: no cover


def _audited(event: str, target: dict[str, Any], action: Any, *, detail: dict[str, Any]) -> Any:
    """Run ``action`` and record it: a capture collects traffic and an export
    writes files taken from it, which someone may later need to account for."""
    detail = {k: v for k, v in detail.items() if v is not None}
    try:
        result = action()
    except (WiresharkError, ValueError) as exc:
        rs.audit(event, "failed", target=target, detail={**detail, "error": str(exc)})
        raise
    kept = {k: result[k] for k in ("file", "packets", "file_bytes", "directory", "total", "bytes") if k in result}
    rs.audit(event, "succeeded", target=target, detail={**detail, **kept})
    return result


def _terminal() -> bool:
    # runspec's logging replaces sys.stdout with a tee whose isatty() is always
    # False, so ask the real stdout.
    stream = sys.__stdout__
    return bool(stream and stream.isatty())


def main() -> None:
    spec = rs.parse("pcap")
    config = config_from_env(
        tshark=spec.tshark.value,
        keylog_file=spec.keylog_file.value,
        decode_as=_list(spec.decode_as),
        resolve_names=spec.resolve_names.value,
        timeout=float(spec.timeout.value),
    )
    try:
        result = _dispatch(spec, config)
    except (WiresharkError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "kind": type(exc).__name__}))
        sys.exit(1)
    # A person at a terminal gets readable text; agents, pipes, the console's
    # Forms tab and schedulers get JSON.
    raw = "raw" in spec and bool(spec.raw.value)
    text = human_text(spec.runspec_command, result) if not spec.runspec_agent and _terminal() and not raw else None
    print(text if text is not None else json.dumps(result, default=str))


if __name__ == "__main__":
    main()
