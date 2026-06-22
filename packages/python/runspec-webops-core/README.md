# runspec-webops-core

Pure-Python HTTPS/TLS certificate + web-endpoint helpers — the importable logic
core behind the cross-platform web runnables in
[`runspec-linux`](../runspec-linux) and [`runspec-windows`](../runspec-windows).

Stdlib only (`ssl` / `socket` / `http.client`) — **no `runspec` dependency, no
`runspec.toml`, and no entry points**, so installing it exposes the helper
functions for import without surfacing any runnables (it is invisible to
`runspec local` / `runspec serve` discovery).

```python
from runspec_webops_core import connect_peer, leaf_report, probe, resolve

peer = connect_peer("example.com", 443, 10)
print(leaf_report(peer, warn_days=30))        # days-to-expiry, issuer, SANs, trust
print(probe("https://example.com", 10))       # status, timing, redirect target
print(resolve("example.com", want_reverse=True))
```

| Function | What it does |
|---|---|
| `connect_peer` / `leaf_report` / `chain_report` | TLS certificate inspection — expiry, issuer, SANs, fingerprint, full chain, trust result |
| `probe` / `classify` | Single HTTP(S) request — status, response time, redirect target |
| `fetch_headers` | Response headers + a security-header report (HSTS, CSP, …) |
| `trace_redirects` | Walk the redirect chain, hop by hop, with loop detection |
| `resolve` | Forward (A/AAAA) and reverse (PTR) DNS lookups |
| `load_document` / `summarize_spec` / `describe_operation` | Read an OpenAPI/Swagger spec (URL or file) and condense its shape — operations, params, request/response schemas — to bounded JSON |

OpenAPI/Swagger specs in JSON parse with the stdlib; YAML needs the optional
`yaml` extra (`pip install runspec-webops-core[yaml]`).

Each function returns plain data and *raises* on failure (`ConnectError`,
`ResolveError` — both subclasses of `WebopsCoreError`).

## Corporate wrapping

Depend on `runspec-webops-core` alone, import the helpers, and ship your own
private package with its own `runspec.toml` that bakes in corporate defaults
(proxies, allowed hosts, redaction) as plain params — only your wrapped
runnables surface in the venv, the generic published code stays clean.
