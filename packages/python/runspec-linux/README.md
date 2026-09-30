# runspec-linux

Linux system-administration runnables (plus Python-venv management, crontab,
filesystem, and the cross-platform web/TLS/API probes) for
[runspec](https://pypi.org/project/runspec/). `pip install` it into a venv and
the runnables are discoverable by `runspec local`, `runspec serve` (MCP), and
runspec-console.

All runnables emit JSON on stdout. Read-only runnables are `autonomy =
"autonomous"`; state-changing ones are `autonomy = "confirm"`.

**Nothing here runs as root.** A tool runs as whoever owns its venv (runspec
>= 0.68.0): you in a personal venv, or the service account in a shared one.
`--run-as` picks another unprivileged account and refuses `root`. The tools that
need root (package upgrades/installs, `reboot-host`, `set-sysctl`,
`enable-passwordless-sudo`) are in
[`runspec-linux-root`](https://pypi.org/project/runspec-linux-root/), for a
root-owned admin venv.

## Install

```
pip install runspec-linux
```

## Runnables

| Group | Runnables |
|---|---|
| System | `system-info`, `disk-usage`, `block-devices`, `check-memory`, `cpu-info`, `pressure` |
| Processes | `list-processes` |
| Services | `list-services`, `check-service`, `restart-service`, `service-detail`, `failed-units` |
| Logs | `tail-log`, `search-log`, `journalctl` |
| Network | `network-interfaces`, `ping-host`, `check-port`, `show-connections`, `trace-route`, `route-table`, `socket-stats`, `dns-config`, `open-ports` |
| Kernel params | `get-sysctl` |
| Files | `find-large-files`, `backup-files`, `owning-package` |
| Sessions | `last-logins`, `who` |
| Containers | `list-containers`, `container-logs`, `restart-container` |
| Packages (read-only) | `list-packages`, `list-autoremovable` |
| Python venvs | `create-venv`, `install-into-venv`, `configure-pip` |
| TCP | `nc-command` |
| Commands | `which` |
| Cron | `cron` (multi-verb: `list`/`add`/`remove`/`clear`) |
| Filesystem | `filesystem` (multi-verb, backed by `runspec-fs-core`) |
| Web / TLS / API | `cert-check`, `cert-chain`, `tls-info`, `http-check`, `http-headers`, `redirect-trace`, `dns-lookup`, `api-spec` |

The package runnables auto-detect apt / dnf / yum / zypper / pacman.
The web/TLS/API probes are pure HTTP (shared with `runspec-windows` via
`runspec-webops-core`) and run anywhere.

```
system-info
list-packages --filter nginx
check-port --host db-01 --port 5432
cert-check --url https://internal.example.com
```

### Python venvs

`create-venv`, `install-into-venv` and `configure-pip` take a **per-invocation**
`--run-as <user>` (+ `--become-method sudo|su`) rather than a declarative
`run_as`, so the service account that owns a shared venv is chosen per call:

```
create-venv --python /usr/bin/python3.12 --dest /opt/venvs/ops --run-as svc-ops --group ops
install-into-venv --venv /opt/venvs/ops --package "runspec-linux==0.13.2" --run-as svc-ops
```

## Public Python API

The pure logic lives in **[`runspec-linux-core`](../runspec-linux-core)** — no
`runspec` dependency, no `runspec.toml`, no entry points (so it surfaces no
runnables). A corporate wrapper package that only wants a helper should depend
on the core alone:

```python
from runspec_linux_core import nc_send, system_info, install_into_venv
```

The helpers are also re-exported from `runspec_linux` for back-compat. Core
helpers return plain data and raise (`ToolNotFoundError` / `CommandError`) on
failure; the runnable wrappers catch these to render the CLI/agent behaviour.

## Development

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest
```
