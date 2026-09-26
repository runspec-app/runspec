"""System monitoring helpers: host info, disk, block devices, memory, processes."""

import json
import os
import platform
import subprocess
from typing import cast

from runspec_linux_core._paths import which
from runspec_linux_core.errors import CommandError, ToolNotFoundError


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


# lsblk columns; the mount column is appended separately (MOUNTPOINTS lists every
# mount point of a device and needs util-linux >= 2.37; MOUNTPOINT is the fallback).
_LSBLK_COLUMNS = "NAME,KNAME,TYPE,SIZE,ROTA,RM,TRAN,MODEL,SERIAL,FSTYPE,LABEL,UUID"


def block_devices(include_loop: bool = False) -> dict:
    """List block devices as a tree (``lsblk``): each disk with its partitions,
    LVM volumes and crypt/RAID layers nested under ``children``.

    Each device has ``name``, ``kname``, ``type``, ``size_bytes``, ``size_gib``,
    ``rotational`` and ``removable``, plus — where known — ``transport``
    (sata/nvme/usb …), ``model``, ``serial``, ``fstype``, ``label``, ``uuid``
    and ``mountpoints``. ``rotational`` is the kernel's flag: ``false`` means an
    SSD or NVMe drive (virtual disks often report ``true``). Loop devices (snap
    and squashfs images) are left out unless ``include_loop``.

    Raises ToolNotFoundError if ``lsblk`` is not installed, CommandError if it fails.
    """
    lsblk = which("lsblk")
    if not lsblk:
        raise ToolNotFoundError("lsblk not available (util-linux)")

    def run(mount_column: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([lsblk, "--json", "--bytes", "--output", f"{_LSBLK_COLUMNS},{mount_column}"], capture_output=True, text=True)

    result = run("MOUNTPOINTS")
    if result.returncode != 0 and "unknown column" in result.stderr:
        result = run("MOUNTPOINT")  # util-linux < 2.37
    if result.returncode != 0:
        raise CommandError(result.stderr.strip() or f"lsblk exited {result.returncode}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CommandError(f"lsblk printed output that isn't JSON: {exc}") from exc
    return {"devices": _lsblk_rows(data.get("blockdevices", []), include_loop)}


def _as_bool(value: object) -> bool | None:
    """lsblk emits JSON booleans since util-linux 2.33, and "0"/"1" strings before."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true")


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None


def _clean(value: object) -> str | None:
    """Strip the padding older lsblk versions leave on model/serial strings."""
    text = str(value).strip() if value is not None else ""
    return text or None


def _lsblk_rows(devices: list[dict], include_loop: bool) -> list[dict]:
    """Normalise ``lsblk --json`` devices (recursively) into compact rows,
    dropping empty fields."""
    rows: list[dict] = []
    for dev in devices:
        if dev.get("type") == "loop" and not include_loop:
            continue
        size = _as_int(dev.get("size"))
        mounts = dev.get("mountpoints", dev.get("mountpoint"))
        row: dict = {
            "name": dev.get("name"),
            "kname": dev.get("kname"),
            "type": dev.get("type"),
            "size_bytes": size,
            "size_gib": round(size / 1_073_741_824, 1) if size is not None else None,
            "rotational": _as_bool(dev.get("rota")),
            "removable": _as_bool(dev.get("rm")),
            "transport": _clean(dev.get("tran")),
            "model": _clean(dev.get("model")),
            "serial": _clean(dev.get("serial")),
            "fstype": _clean(dev.get("fstype")),
            "label": _clean(dev.get("label")),
            "uuid": _clean(dev.get("uuid")),
            "mountpoints": [m for m in (mounts if isinstance(mounts, list) else [mounts]) if m],
            "children": _lsblk_rows(dev.get("children", []), include_loop),
        }
        rows.append({key: value for key, value in row.items() if value is not None and value != []})
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
