# runspec-webops

HTTPS/TLS certificate and web-endpoint runnables for [runspec](https://github.com/JasonFinestone/runspec), built on Node's built-in `tls` / `https` — **zero runtime dependencies** beyond `runspec-node`.

| Runnable | What it does |
|---|---|
| `cert-check` | Inspect a host's TLS certificate — days-to-expiry, issuer, SANs, fingerprint. Exits non-zero when expiring/expired. |
| `cert-chain` | Print the full certificate chain (leaf → intermediates → root). |
| `http-check` | Probe an HTTP(S) URL — status, response time, redirect target; optional `--expect-status`. |
| `tls-info`   | Report the negotiated TLS protocol, cipher, and trust result. |

All are read-only network probes (`autonomy = "autonomous"`).

## Use

```bash
cert-check example.com                       # human-readable
cert-check example.com --warn-days 14 --json # JSON, warn under 14 days
cert-chain example.com
http-check https://example.com --expect-status 200
tls-info example.com
```

## Deploy (model #1 — venv-shaped folder)

This package *is* the deployable unit. To stand it up on a host:

```bash
# materialise the package as a folder (clone / degit / npm pack + extract)
npm pack runspec-webops && tar xzf runspec-webops-*.tgz && mv package webops && cd webops

npm install            # pulls runspec-node
npx runspec bin        # generates bin/runspec + bin/<runnable> + logs/
```

The folder is now venv-shaped: a controller (runspec-console) invokes `bin/<runnable>` and discovers/manages it via `bin/runspec`, from any working directory — including over SSH. Per-invocation logs land in `logs/` and are read/compacted/pruned by `runspec logs` (Python or Node).
