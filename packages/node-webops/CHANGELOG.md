# Changelog — runspec-webops

All notable changes to this package are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers follow [Semantic Versioning](https://semver.org/).

---

## [0.1.0] — 2026-06-04

Initial release. HTTPS/TLS certificate + web-endpoint runnables, built on Node's
built-in `tls`/`https` (zero runtime deps beyond `runspec-node`):

- **`cert-check`** — leaf TLS certificate inspection: days-to-expiry, issuer,
  SANs, serial, SHA-256 fingerprint, and trust result. `--warn-days` sets the
  expiring threshold; exits non-zero on `expiring`/`expired` for monitors.
- **`cert-chain`** — the full certificate chain (leaf → intermediates → root),
  each link's issuer + expiry, self-signed-root detection.
- **`http-check`** — HTTP(S) status, response time, redirect target; optional
  `--expect-status` to gate the exit code.
- **`tls-info`** — negotiated TLS protocol, cipher, and trust result.

All runnables are read-only (`autonomy = "autonomous"`), support `--json`, and
write per-invocation audit logs (`[config.logging] store = "per-run"`). The
first published Node runnable package — deployed as a venv-shaped folder
(`runspec bin`).
