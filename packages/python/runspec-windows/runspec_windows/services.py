"""Service runnables: list-services, check-service, restart-service, stop-service."""

from __future__ import annotations

import json

import runspec as rs

from runspec_windows import _platform

_SELECT = "Select-Object Name,DisplayName,@{N='status';E={$_.Status.ToString()}},@{N='start_type';E={$_.StartType.ToString()}}"


def normalize_services(data: object, status_filter: str = "all") -> list[dict]:
    """Reshape parsed ``Get-Service | ConvertTo-Json`` output, applying a status filter.

    ``data`` is a single dict (one service) or a list of dicts. ``status_filter``
    is one of ``all`` / ``running`` / ``stopped``.
    """
    items: list[dict]
    if isinstance(data, dict):
        items = [data]
    elif isinstance(data, list):
        items = [d for d in data if isinstance(d, dict)]
    else:
        items = []

    rows = []
    for item in items:
        status = str(item.get("status", "")).lower()
        if status_filter == "running" and status != "running":
            continue
        if status_filter == "stopped" and status != "stopped":
            continue
        rows.append(
            {
                "name": item.get("Name"),
                "display_name": item.get("DisplayName"),
                "status": item.get("status"),
                "start_type": item.get("start_type"),
            }
        )
    return rows


def main_list_services() -> None:
    spec = rs.parse("list-services")
    _platform.ensure_windows()
    status = str(spec.status)
    try:
        out = _platform.run_powershell(f"Get-Service | {_SELECT} | ConvertTo-Json -Compress").strip()
        data = json.loads(out) if out else []
        print(json.dumps(normalize_services(data, status)))
    except Exception as e:
        _platform.fail(str(e))


def main_check_service() -> None:
    spec = rs.parse("check-service")
    _platform.ensure_windows()
    service = str(spec.service)
    try:
        out = _platform.run_powershell(f"Get-Service -Name '{service}' -ErrorAction Stop | {_SELECT} | ConvertTo-Json -Compress").strip()
        rows = normalize_services(json.loads(out)) if out else []
        if not rows:
            _platform.fail(f"Service not found: {service}", service=service)
        print(json.dumps(rows[0]))
    except RuntimeError:
        _platform.fail(f"Service not found: {service}", service=service)
    except Exception as e:
        _platform.fail(str(e), service=service)


def main_restart_service() -> None:
    spec = rs.parse("restart-service")
    _platform.ensure_windows()
    service = str(spec.service)
    try:
        _platform.run_powershell(f"Restart-Service -Name '{service}' -Force -ErrorAction Stop")
        print(json.dumps({"service": service, "restarted": True}))
    except RuntimeError as e:
        _platform.fail(str(e), service=service, restarted=False)
    except Exception as e:
        _platform.fail(str(e), service=service)


def main_stop_service() -> None:
    spec = rs.parse("stop-service")
    _platform.ensure_windows()
    service = str(spec.service)
    try:
        _platform.run_powershell(f"Stop-Service -Name '{service}' -Force -ErrorAction Stop")
        print(json.dumps({"service": service, "stopped": True}))
    except RuntimeError as e:
        _platform.fail(str(e), service=service, stopped=False)
    except Exception as e:
        _platform.fail(str(e), service=service)
