"""Kernel tuning that needs root."""

import json
import sys

import runspec as rs
from runspec_linux_core import set_sysctl
from runspec_linux_core.errors import CommandError


def main_set_sysctl() -> None:
    spec = rs.parse("set-sysctl")
    key = spec.key.value
    value = spec.value.value
    persist = spec.persist.value
    try:
        print(json.dumps(set_sysctl(key, value, persist=persist)))
    except CommandError as e:
        print(json.dumps({"key": key, "applied": False, "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
