"""Wrappers for the performance tuning & analysis runnables."""

import json
import sys

import runspec as rs
from runspec_linux_core import cpu_info, get_sysctl, pressure


def main_get_sysctl() -> None:
    spec = rs.parse("get-sysctl")
    pattern = spec.pattern.value or None
    try:
        print(json.dumps(get_sysctl(pattern)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_cpu_info() -> None:
    rs.parse("cpu-info")
    try:
        print(json.dumps(cpu_info()))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_pressure() -> None:
    rs.parse("pressure")
    try:
        print(json.dumps(pressure()))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
