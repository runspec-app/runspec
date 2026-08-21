# runspec-windows

Windows system-administration runnables (plus the cross-platform web/TLS/API
probes) for [runspec](https://pypi.org/project/runspec/). The Windows counterpart
to [`runspec-linux`](../runspec-linux): `pip install` it into a venv and the
runnables are discoverable by `runspec local`, `runspec serve` (MCP), and
runspec-console.

> **Microsoft 365 (Outlook · Teams · Calendar · OneDrive)** runnables moved to the
> standalone [`runspec-o365`](../runspec-o365) package in 0.5.0 (they used to be
> the `[graph]` extra). `pip install runspec-o365` for those.

All runnables emit JSON on stdout. Read-only runnables are `autonomy =
"autonomous"`; state-changing ones (`kill-process`, `restart-service`,
`stop-service`, `flush-dns`) are `autonomy = "confirm"`.

## Install

```
pip install runspec-windows
```

## Runnables

| Group | Runnables |
|---|---|
| System | `system-info`, `disk-usage`, `check-memory`, `uptime` |
| Processes | `list-processes`, `process-info`, `kill-process` |
| Services | `list-services`, `check-service`, `restart-service`, `stop-service` |
| Network | `ping-host`, `check-port`, `show-connections`, `list-adapters`, `flush-dns` |
| Event log | `query-eventlog`, `recent-errors` |
| Scheduled tasks | `list-scheduled-tasks` |
| Installed software | `installed-software` |
| Sessions | `who`, `current-user` |
| Commands | `which` |
| Net diagnostics | `proxy-settings`, `vpn-status`, `dns-config`, `test-connection`, `trace-route`, `route-table`, `gpresult`, `list-certs`, `collect-diagnostics` |
| Filesystem | `fs` (multi-verb, backed by `runspec-fs-core`) |
| Web / TLS / API | `cert-check`, `cert-chain`, `tls-info`, `http-check`, `http-headers`, `redirect-trace`, `dns-lookup`, `api-spec` |

The system runnables are Windows-only; run one on macOS/Linux and it returns a
clean JSON error rather than a traceback. The web/TLS/API probes are pure HTTP
and run anywhere.

```
system-info
list-processes --filter chrome --limit 10
query-eventlog --log System --level error --count 20
installed-software --filter "Microsoft"
```

## Development

```
python -m venv .venv && . .venv/bin/activate   # or .venv\Scripts\activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest
```

The OS-specific work sits behind thin helpers in `_platform.py`; the pure output
parsers (`tasklist`/`netstat`/`ipconfig`/`schtasks`/`query user`) are unit-tested
on any platform. Real end-to-end runs of the Windows-only runnables happen on a
Windows host / the `windows-latest` CI job.
