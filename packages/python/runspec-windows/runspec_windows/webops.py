"""Web/TLS runnables — thin cross-platform wrappers over runspec-webops-core.

These probe remote HTTP(S)/TLS endpoints and resolve DNS using only the Python
stdlib (via ``runspec-webops-core``), so they behave identically on Linux and
Windows. All are read-only network probes (``autonomy = "autonomous"``).

Exit codes follow the web-monitor convention so a caller can branch on them:
``0`` success, ``1`` a reportable problem (expiring/expired cert, unexpected
status), ``2`` the endpoint was unreachable.
"""

from __future__ import annotations

import json
import sys

import runspec as rs
from runspec_webops_core import (
    ConnectError,
    ResolveError,
    SpecError,
    chain_report,
    connect_peer,
    describe_operation,
    fetch_headers,
    leaf_report,
    load_document,
    probe,
    resolve,
    summarize_spec,
    trace_redirects,
)


def _emit(obj: object) -> None:
    print(json.dumps(obj))


def main_cert_check() -> None:
    spec = rs.parse("cert-check")
    host, port = spec.host.value, spec.port.value
    try:
        peer = connect_peer(host, port, spec.timeout.value, proxy=spec.proxy.value)
        report = leaf_report(peer, spec.warn_days.value)
    except ConnectError as e:
        _emit({"error": str(e), "host": host, "port": port})
        sys.exit(2)
    _emit({"host": host, "port": port, **report})
    if report["status"] != "ok":
        sys.exit(1)


def main_cert_chain() -> None:
    spec = rs.parse("cert-chain")
    host, port = spec.host.value, spec.port.value
    try:
        peer = connect_peer(host, port, spec.timeout.value, proxy=spec.proxy.value)
    except ConnectError as e:
        _emit({"error": str(e), "host": host, "port": port})
        sys.exit(2)
    _emit({"host": host, "port": port, "chain": chain_report(peer)})


def main_tls_info() -> None:
    spec = rs.parse("tls-info")
    host, port = spec.host.value, spec.port.value
    try:
        peer = connect_peer(host, port, spec.timeout.value, proxy=spec.proxy.value)
    except ConnectError as e:
        _emit({"error": str(e), "host": host, "port": port})
        sys.exit(2)
    _emit(
        {
            "host": host,
            "port": port,
            "protocol": peer.protocol,
            "cipher": peer.cipher_name,
            "cipher_version": peer.cipher_version,
            "authorized": peer.authorized,
            "authorization_error": peer.authorization_error,
        }
    )


def main_http_check() -> None:
    spec = rs.parse("http-check")
    url = spec.url.value
    expect = spec.expect_status.value
    try:
        result = probe(url, spec.timeout.value, insecure=spec.insecure.value, proxy=spec.proxy.value)
    except ConnectError as e:
        _emit({"error": str(e), "url": url})
        sys.exit(2)
    _emit(result)
    if expect is not None and result["status"] != expect:
        sys.exit(1)


def main_http_headers() -> None:
    spec = rs.parse("http-headers")
    url = spec.url.value
    try:
        result = fetch_headers(url, spec.timeout.value, method=spec.method.value, insecure=spec.insecure.value, proxy=spec.proxy.value)
    except ConnectError as e:
        _emit({"error": str(e), "url": url})
        sys.exit(2)
    _emit(result)


def main_redirect_trace() -> None:
    spec = rs.parse("redirect-trace")
    url = spec.url.value
    try:
        result = trace_redirects(url, spec.timeout.value, max_hops=spec.max_hops.value, insecure=spec.insecure.value, proxy=spec.proxy.value)
    except ConnectError as e:
        _emit({"error": str(e), "url": url})
        sys.exit(2)
    _emit(result)


def main_dns_lookup() -> None:
    spec = rs.parse("dns-lookup")
    host = spec.host.value
    try:
        _emit(resolve(host, want_reverse=spec.reverse.value))
    except ResolveError as e:
        _emit({"error": str(e), "host": host})
        sys.exit(1)


def main_api_spec() -> None:
    spec = rs.parse("api-spec")
    source = spec.source.value
    try:
        doc = load_document(source, spec.timeout.value, insecure=spec.insecure.value, proxy=spec.proxy.value)
        if spec.operation.value or spec.path.value:
            result: object = describe_operation(
                doc,
                operation_id=spec.operation.value or None,
                path=spec.path.value or None,
                method=spec.method.value or None,
            )
        else:
            result = summarize_spec(doc, tag=spec.tag.value or None, path_filter=spec.match.value or None)
    except SpecError as e:
        _emit({"error": str(e), "source": source})
        sys.exit(2)
    _emit(result)
