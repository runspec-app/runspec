# runspec-registry

A lightweight HTTP registry for [runspec](https://runspec.app) agents.

When `runspec serve` starts on a host it registers itself here. The registry tracks which tools are available, on which hosts, and with what execution metadata. Clients (Chainlit apps, orchestrators, CI pipelines) query the registry to discover agents and route tool calls.

## Install

```bash
pip install runspec-registry
```

## Start the server

```bash
runspec-registry
```

Options:

```
--host TEXT            Bind host (default: 0.0.0.0)
--port INT             Bind port (default: 8765)
--api-key TEXT         API key required for write endpoints
--ssl-keyfile PATH     TLS private key
--ssl-certfile PATH    TLS certificate
--purge-interval INT   Seconds between stale-instance sweeps (default: 60)
--reload               Auto-reload for development
```

## API

### Instances

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/instances` | write | Register an agent |
| `POST` | `/instances/{id}/heartbeat` | write | Send heartbeat |
| `POST` | `/instances/{id}/tools` | write | Update tool list |
| `DELETE` | `/instances/{id}` | write | Deregister an agent |

### Discovery

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/tools` | read | List all tools with their hosts |
| `GET` | `/tools/{name}` | read | Get a tool with per-host execution metadata |
| `GET` | `/instances` | read | List all live instances |
| `GET` | `/health` | none | Health check |

Write endpoints require `X-API-Key` header when `--api-key` is set. Read endpoints are unauthenticated by default.

## Authentication

Start with an API key to protect write endpoints:

```bash
runspec-registry --api-key mysecretkey
```

Agents register with the matching key:

```bash
runspec serve --registry http://myserver:8765 --registry-key mysecretkey
```

## TLS

Provide a certificate and key to enable HTTPS:

```bash
runspec-registry --ssl-certfile cert.pem --ssl-keyfile key.pem
```

Agents connecting to a registry with a self-signed cert pass the CA cert:

```bash
runspec serve --registry https://myserver:8765 --registry-cert ca.pem
```

## How it works

1. `runspec serve` starts on each host, reads `runspec.toml`, and registers with the registry
2. It sends a heartbeat every 30 seconds (configurable via `heartbeat` in `runspec.toml`)
3. The registry purges instances that have missed heartbeats
4. Clients query `/tools` to see what's available and on which hosts
5. `runspec jump <tool> --host <host> --registry <url>` looks up the tool, gets the execution metadata for that host, and SSHes to run it

## Links

- [Documentation](https://runspec.app)
- [runspec on PyPI](https://pypi.org/project/runspec/)
- [Source](https://github.com/runspec-app/runspec)
