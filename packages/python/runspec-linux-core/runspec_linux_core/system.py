"""System monitoring helpers: host info, disk, memory, processes."""

import os
import platform
import subprocess
from typing import cast


def system_info() -> dict:
    """Return hostname, OS/kernel, uptime, load averages, and CPU count."""
    load1, load5, load15 = os.getloadavg()
    with open("/proc/uptime") as f:
        uptime_seconds = float(f.read().split()[0])

    return {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "kernel": platform.version(),
        "machine": platform.machine(),
        "uptime_seconds": round(uptime_seconds),
        "load_1": round(load1, 2),
        "load_5": round(load5, 2),
        "load_15": round(load15, 2),
        "cpu_count": os.cpu_count(),
    }


def disk_usage() -> list[dict]:
    """Return per-filesystem usage (sizes in MB) parsed from ``df``."""
    result = subprocess.run(
        ["df", "-P", "-B1"],
        capture_output=True,
        text=True,
        check=True,
    )
    rows = []
    for line in result.stdout.strip().splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        size_bytes = int(parts[1])
        used_bytes = int(parts[2])
        avail_bytes = int(parts[3])
        rows.append(
            {
                "filesystem": parts[0],
                "size_mb": round(size_bytes / 1_048_576, 1),
                "used_mb": round(used_bytes / 1_048_576, 1),
                "available_mb": round(avail_bytes / 1_048_576, 1),
                "use_pct": parts[4],
                "mounted_on": parts[5],
            }
        )
    return rows


def check_memory() -> dict:
    """Return memory and swap usage (in MB) parsed from ``/proc/meminfo``."""
    mem: dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, val_kb = line.split(":")
            kb = int(val_kb.strip().split()[0])
            mem[key.strip()] = kb

    return {
        "total_mb": round(mem.get("MemTotal", 0) / 1024, 1),
        "used_mb": round((mem.get("MemTotal", 0) - mem.get("MemAvailable", 0)) / 1024, 1),
        "free_mb": round(mem.get("MemFree", 0) / 1024, 1),
        "available_mb": round(mem.get("MemAvailable", 0) / 1024, 1),
        "cached_mb": round(mem.get("Cached", 0) / 1024, 1),
        "swap_total_mb": round(mem.get("SwapTotal", 0) / 1024, 1),
        "swap_used_mb": round((mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) / 1024, 1),
    }


def list_processes(name_filter: str | None = None, limit: int = 20) -> list[dict]:
    """Return running processes (from ``ps aux``) sorted by CPU, capped at ``limit``.

    If ``name_filter`` is given, only processes whose command contains it
    (case-insensitive) are included.
    """
    result = subprocess.run(
        ["ps", "aux", "--no-headers"],
        capture_output=True,
        text=True,
        check=True,
    )
    rows = []
    for line in result.stdout.strip().splitlines():
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        command = parts[10]
        if name_filter and name_filter.lower() not in command.lower():
            continue
        rows.append(
            {
                "user": parts[0],
                "pid": int(parts[1]),
                "cpu_pct": float(parts[2]),
                "mem_pct": float(parts[3]),
                "vsz_kb": int(parts[4]),
                "rss_kb": int(parts[5]),
                "stat": parts[7],
                "command": command,
            }
        )
    rows.sort(key=lambda r: cast(float, r["cpu_pct"]), reverse=True)
    return rows[:limit]
