# runspec-webops-core Changelog

## [0.2.0] — 2026-08-26

### Added — proxy discovery + CONNECT tunnelling

- New `proxy` module: the web/TLS helpers no longer connect only *directly* to
  the target — they now resolve and use an HTTP proxy the way a browser does, so
  internal hosts reachable only via a corporate proxy stop timing out.
- `resolve_proxy(url, explicit=None)` picks the proxy for a URL in this order:
  an **explicit** override (the literal `"direct"` forces none) → the
  **environment** (`HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`, honouring
  `NO_PROXY`) → on **Windows**, the *system* configuration via WinHTTP —
  **WPAD auto-detect and PAC scripts evaluated per-URL** (`WinHttpGetProxyForUrl`,
  matching Edge/Chrome), then the per-user manual proxy (with its bypass list),
  then the machine-wide `netsh winhttp` default → otherwise **direct**. The
  Windows path is best-effort — any failure falls through to a direct connection
  rather than raising.
- `open_socket(host, port, timeout, proxy=None)` opens a raw TCP socket,
  issuing an HTTP `CONNECT` when a proxy applies so TLS is driven to the *real*
  host through the tunnel (the inspected certificate is the target's, not the
  proxy's). Plain-HTTP requests through a proxy use the absolute-URI request
  form. Embedded `user:pass@` proxy credentials become a `Proxy-Authorization:
  Basic` header.
- Every network helper gained a `proxy` parameter (default `None` =
  auto-discover; `"direct"` forces none): `probe`, `fetch_headers`,
  `trace_redirects`, `fetch_body`, `load_document`, and `connect_peer`. Proxy is
  re-resolved per redirect hop so a cross-host bypass is honoured.
- Still stdlib-only and dependency-free (the Windows path uses `ctypes`, no new
  runtime dependency).

## [0.1.0] — 2026-06-22

Initial release.

Pure-Python HTTPS/TLS + web-endpoint helpers — the importable logic core behind
the cross-platform web runnables in `runspec-linux` and `runspec-windows`. Stdlib
only (`ssl` / `socket` / `http.client`); **no `runspec` dependency, no
`runspec.toml`, and no entry points**, so it surfaces zero runnables and stays
invisible to `runspec local` / `runspec serve` discovery.

Public API (every function returns plain data and raises on failure — see
`runspec_webops_core.errors`):

- **TLS** — `connect_peer(host, port, timeout)` opens an inspection connection
  (verification disabled, so it still reports on expired/self-signed endpoints)
  and a second verifying connection for the trust verdict; `leaf_report`,
  `chain_report`, `days_until`, `cert_status` are the pure reporting helpers.
  The certificate chain is read via `SSLSocket.get_unverified_chain()` (Python
  3.13+), the underlying `_sslobj` method on 3.10–3.12, or the leaf-only
  fallback when neither is available.
- **HTTP** — `probe` (single request, no redirect following), `classify`,
  `fetch_headers` (with a security-header report), `trace_redirects` (walks the
  redirect chain with loop detection).
- **DNS** — `resolve(host, want_reverse=...)` for forward (A/AAAA + canonical
  name) and reverse (PTR) lookups via `socket`.
- **OpenAPI/Swagger** — `load_document` (fetch a URL or read a file),
  `parse_document`, `summarize_spec` (bounded overview: info, servers,
  operations) and `describe_operation` (one operation's params, request body and
  responses, with `$ref`s resolved to bounded schema summaries). Understands
  OpenAPI 3.x and Swagger 2.0. JSON parses with the stdlib; YAML needs the
  optional `yaml` extra (`pip install runspec-webops-core[yaml]`). `fetch_body`
  is the redirect-following, size-capped GET behind it.
