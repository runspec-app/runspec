import json
import os
import platform
import sys

import runspec as rs


def main_system_info() -> None:
    rs.parse("system-info")

    try:
        load1, load5, load15 = os.getloadavg()
        with open("/proc/uptime") as f:
            uptime_seconds = float(f.read().split()[0])

        print(
            json.dumps(
                {
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
            )
        )
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_disk_usage() -> None:
    rs.parse("disk-usage")

    try:
        import subprocess

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
        print(json.dumps(rows))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_check_memory() -> None:
    rs.parse("check-memory")

    try:
        mem: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, val_kb = line.split(":")
                kb = int(val_kb.strip().split()[0])
                mem[key.strip()] = kb

        print(
            json.dumps(
                {
                    "total_mb": round(mem.get("MemTotal", 0) / 1024, 1),
                    "used_mb": round((mem.get("MemTotal", 0) - mem.get("MemAvailable", 0)) / 1024, 1),
                    "free_mb": round(mem.get("MemFree", 0) / 1024, 1),
                    "available_mb": round(mem.get("MemAvailable", 0) / 1024, 1),
                    "cached_mb": round(mem.get("Cached", 0) / 1024, 1),
                    "swap_total_mb": round(mem.get("SwapTotal", 0) / 1024, 1),
                    "swap_used_mb": round((mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) / 1024, 1),
                }
            )
        )
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_list_processes() -> None:
    spec = rs.parse("list-processes")
    name_filter = str(spec.filter) if spec.filter is not None else None
    limit = int(spec.limit)

    try:
        import subprocess

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
        rows.sort(key=lambda r: r["cpu_pct"], reverse=True)
        print(json.dumps(rows[:limit]))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
