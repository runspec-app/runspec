"""System monitoring runnables: system-info, disk-usage, check-memory, uptime."""

from __future__ import annotations

import ctypes
import json
import platform
import shutil
import string
import sys
import time

import runspec as rs

from runspec_windows import _platform


def _uptime_seconds() -> int:
    """Seconds since boot, via the Win32 GetTickCount64 tick counter."""
    return int(ctypes.windll.kernel32.GetTickCount64() // 1000)  # type: ignore[attr-defined]


def main_system_info() -> None:
    rs.parse("system-info")
    _platform.ensure_windows()
    try:
        uptime = _uptime_seconds()
        print(
            json.dumps(
                {
                    "hostname": platform.node(),
                    "os": f"{platform.system()} {platform.release()}",
                    "version": platform.version(),
                    "machine": platform.machine(),
                    "processor": platform.processor(),
                    "uptime_seconds": uptime,
                    "boot_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - uptime)),
                }
            )
        )
    except Exception as e:
        _platform.fail(str(e))


def _drive_usage(drive: str) -> dict | None:
    try:
        total, used, free = shutil.disk_usage(drive)
    except OSError:
        return None
    return {
        "drive": drive,
        "total_mb": round(total / 1024 / 1024),
        "used_mb": round(used / 1024 / 1024),
        "free_mb": round(free / 1024 / 1024),
        "percent_used": round(used / total * 100, 1) if total else 0.0,
    }


def main_disk_usage() -> None:
    spec = rs.parse("disk-usage")
    _platform.ensure_windows()
    path = str(spec.path) if spec.path is not None else None
    try:
        if path:
            row = _drive_usage(path)
            if row is None:
                _platform.fail(f"Path not accessible: {path}", path=path)
            print(json.dumps(row))
            return

        rows = []
        for letter in string.ascii_uppercase:
            row = _drive_usage(f"{letter}:\\")
            if row is not None:
                rows.append(row)
        print(json.dumps(rows))
    except Exception as e:
        _platform.fail(str(e))


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def main_check_memory() -> None:
    rs.parse("check-memory")
    _platform.ensure_windows()
    try:
        stat = _MemoryStatusEx()
        stat.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):  # type: ignore[attr-defined]
            _platform.fail("GlobalMemoryStatusEx failed")
        mb = 1024 * 1024
        print(
            json.dumps(
                {
                    "ram_total_mb": round(stat.ullTotalPhys / mb),
                    "ram_used_mb": round((stat.ullTotalPhys - stat.ullAvailPhys) / mb),
                    "ram_available_mb": round(stat.ullAvailPhys / mb),
                    "percent_used": stat.dwMemoryLoad,
                    "pagefile_total_mb": round(stat.ullTotalPageFile / mb),
                    "pagefile_available_mb": round(stat.ullAvailPageFile / mb),
                }
            )
        )
    except Exception as e:
        _platform.fail(str(e))


def main_uptime() -> None:
    rs.parse("uptime")
    _platform.ensure_windows()
    try:
        uptime = _uptime_seconds()
        print(
            json.dumps(
                {
                    "uptime_seconds": uptime,
                    "boot_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - uptime)),
                }
            )
        )
    except Exception as e:
        _platform.fail(str(e))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(0)
