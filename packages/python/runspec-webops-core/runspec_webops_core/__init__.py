"""runspec-webops-core — pure-Python HTTPS/TLS + web-endpoint helpers.

This package has **no dependency on runspec** and ships **no runspec.toml and no
entry points**, so installing it exposes the helper functions for import without
surfacing any runnables (it is invisible to ``runspec local`` / ``runspec serve``
discovery). ``runspec-linux`` and ``runspec-windows`` both depend on it and wrap
each helper in a cross-platform runnable; a private (e.g. Nexus-hosted) package
can instead import these helpers directly and ship its own runnables.

Everything is stdlib only (``ssl`` / ``socket`` / ``http.client``). Each function
returns plain data and *raises* on failure (see ``runspec_webops_core.errors``).
"""

from runspec_webops_core.dns import resolve
from runspec_webops_core.errors import ConnectError, ResolveError, WebopsCoreError
from runspec_webops_core.http import (
    classify,
    fetch_body,
    fetch_headers,
    probe,
    trace_redirects,
)
from runspec_webops_core.openapi import (
    SpecError,
    describe_operation,
    load_document,
    parse_document,
    summarize_spec,
)
from runspec_webops_core.tls import (
    CertInfo,
    TlsPeer,
    cert_status,
    chain_report,
    connect_peer,
    days_until,
    leaf_report,
)

__all__ = [
    # errors
    "WebopsCoreError",
    "ConnectError",
    "ResolveError",
    "SpecError",
    # tls
    "CertInfo",
    "TlsPeer",
    "connect_peer",
    "leaf_report",
    "chain_report",
    "days_until",
    "cert_status",
    # http
    "probe",
    "classify",
    "fetch_headers",
    "fetch_body",
    "trace_redirects",
    # dns
    "resolve",
    # openapi
    "load_document",
    "parse_document",
    "summarize_spec",
    "describe_operation",
]
