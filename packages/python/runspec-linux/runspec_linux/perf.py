"""Wrappers for the performance tuning & analysis runnables."""

import json
import sys

import runspec as rs
from runspec_linux_core import cpu_info, get_sysctl, pressure, set_sysctl
from runspec_linux_core.errors import CommandError

from runspec_linux._escalation import resolve_escalation


def main_get_sysctl() -> None:
    spec = rs.parse("get-sysctl")
    pattern = spec.pattern.value or None
    try:
        print(json.dumps(get_sysctl(pattern)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_set_sysctl() -> None:
    spec = rs.parse("set-sysctl")
    key = spec.key.value
    value = spec.value.value
    persist = spec.persist.value
    run_as, method = resolve_escalation(spec)
    try:
        print(json.dumps(set_sysctl(key, value, persist=persist, run_as=run_as, become_method=method)))
    except CommandError as e:
        print(json.dumps({"key": key, "applied": False, "error": str(e)}))
        sys.exit(1)
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
